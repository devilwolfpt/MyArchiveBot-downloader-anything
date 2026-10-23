"""Custom Brightcove Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor para o Brightcove Playback API."""

import json
import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

from app.core.exceptions import DownloaderError, ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat, SubtitleTrack
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

PLAYER_URL_RE = re.compile(
    r"^https?://players\.brightcove\.net/(?P<account>\d+)/"
    r"(?P<player>[^/?#]+)_(?P<embed>[^/?#]+)/index(?:\.html)?(?:\?(?P<query>[^#]*))?",
    re.IGNORECASE,
)
POLICY_KEY_RE = re.compile(r"""policyKey\s*:\s*["'](?P<key>[^"']+)["']""")

CONTAINERS: dict[str, Container] = {
    "mp4": Container.MP4,
    "m4v": Container.MP4,
    "webm": Container.WEBM,
    "mov": Container.MOV,
    "mkv": Container.MKV,
    "flv": Container.FLV,
    "ts": Container.TS,
    "mp3": Container.MP3,
    "m4a": Container.M4A,
    "aac": Container.M4A,
    "ogg": Container.OGG,
    "wav": Container.WAV,
}


class BrightcoveExtractor(BaseExtractor):
    name = "brightcove"
    priority = 24
    patterns = [
        r"^https?://players\.brightcove\.net/\d+/[^/?#]+/index(?:\.html)?(?:\?|$)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        match = PLAYER_URL_RE.match(url)
        if not match:
            raise ExtractionError("URL de player Brightcove inválido")

        account_id, player_id, embed = (
            match.group("account"),
            match.group("player"),
            match.group("embed"),
        )
        params = parse_qs(match.group("query") or "")
        video_id = (params.get("videoId") or params.get("video_id") or [None])[0]
        playlist_id = (params.get("playlistId") or [None])[0]
        content_type = "playlists" if playlist_id else "videos"
        content_id = playlist_id or video_id
        if not content_id:
            raise ExtractionError(
                "O URL Brightcove não contém videoId ou playlistId",
                details={"account_id": account_id},
            )

        base_url = f"https://players.brightcove.net/{account_id}/{player_id}_{embed}/"
        policy_key = await self._get_policy_key(base_url, ctx, url)
        api_url = (
            "https://edge.api.brightcove.com/playback/v1/accounts/"
            f"{account_id}/{content_type}/{quote(content_id, safe=':')}"
        )
        headers = ctx.request_headers(referer=url)
        headers["Accept"] = f"application/json;pk={policy_key}"
        result = await ctx.http.get(
            api_url,
            headers=headers,
            max_bytes=ctx.max_page_bytes,
        )
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "O Playback API Brightcove devolveu JSON inválido",
                details={"content_id": content_id},
            ) from exc

        if not isinstance(payload, dict):
            raise ExtractionError("O Playback API Brightcove devolveu metadados inválidos")
        errors = payload.get("errors")
        if errors:
            first_error = errors[0] if isinstance(errors, list) else errors
            message = (
                first_error.get("message")
                if isinstance(first_error, dict)
                else str(first_error)
            )
            raise ExtractionError(
                f"Brightcove: {message or 'não foi possível obter o vídeo'}",
                details={"content_id": content_id},
            )
        if playlist_id:
            videos = payload.get("videos")
            if isinstance(videos, list) and videos:
                payload = videos[0]
                content_id = str(payload.get("id") or content_id)

        formats, subtitles = await self._parse_metadata(payload, ctx, url)
        if not formats:
            raise ExtractionError(
                "Brightcove não devolveu formatos media utilizáveis (o conteúdo pode estar protegido por DRM)",
                details={"content_id": content_id},
            )

        duration_ms = self._number(payload.get("duration"))
        duration = duration_ms / 1000 if duration_ms is not None else None
        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=str(payload.get("name") or content_id),
            description=payload.get("description"),
            thumbnail=payload.get("poster") or payload.get("thumbnail"),
            duration=duration,
            uploader_id=str(payload["account_id"]) if payload.get("account_id") else account_id,
            tags=[tag for tag in payload.get("tags", []) if isinstance(tag, str)]
            if isinstance(payload.get("tags"), list)
            else [],
            formats=formats,
            subtitles=subtitles,
        )

    async def _get_policy_key(
        self, base_url: str, ctx: ExtractContext, referer: str
    ) -> str:
        try:
            config_response = await ctx.http.get(
                f"{base_url}config.json",
                headers=ctx.request_headers(referer=referer),
                max_bytes=ctx.max_page_bytes,
            )
            config = json.loads(config_response.text)
        except DownloaderError:
            config = {}
        except json.JSONDecodeError:
            config = {}

        if isinstance(config, dict):
            video_cloud = config.get("video_cloud")
            if isinstance(video_cloud, dict):
                policy_key = video_cloud.get("policy_key")
                if isinstance(policy_key, str) and policy_key:
                    return policy_key

        try:
            player_response = await ctx.http.get(
                f"{base_url}index.min.js",
                headers=ctx.request_headers(referer=referer),
                max_bytes=ctx.max_page_bytes,
            )
        except DownloaderError as exc:
            raise ExtractionError(
                "Não foi possível obter a policy key do player Brightcove",
                details={"player_url": base_url},
            ) from exc

        catalog_match = re.search(
            r"catalog\(\s*(\{.*?\})\s*\)\s*;",
            player_response.text,
            re.DOTALL,
        )
        if catalog_match:
            try:
                catalog = json.loads(catalog_match.group(1))
            except json.JSONDecodeError:
                catalog = {}
            policy_key = catalog.get("policyKey") if isinstance(catalog, dict) else None
            if isinstance(policy_key, str) and policy_key:
                return policy_key

        policy_match = POLICY_KEY_RE.search(player_response.text)
        if policy_match:
            return policy_match.group("key")
        raise ExtractionError(
            "O player Brightcove não expôs uma policy key válida",
            details={"player_url": base_url},
        )

    async def _parse_metadata(
        self, payload: dict, ctx: ExtractContext, referer: str
    ) -> tuple[list[StreamFormat], dict[str, list[SubtitleTrack]]]:
        formats: list[StreamFormat] = []
        subtitles: dict[str, list[SubtitleTrack]] = {}
        sources = payload.get("sources")

        if isinstance(sources, list):
            for index, source in enumerate(sources):
                if not isinstance(source, dict):
                    continue
                src = source.get("src") or source.get("streaming_src")
                if not isinstance(src, str) or not src:
                    continue
                source_type = str(source.get("type") or "").lower()
                path = urlsplit(src).path.lower()

                if "mpegurl" in source_type or path.endswith(".m3u8") or source.get("container") == "M2TS":
                    protected = self._is_drm_protected(source, path)
                    initial_count = len(formats)
                    try:
                        formats.extend(
                            await self.formats_from_manifest(
                                src, ctx, headers=ctx.request_headers(referer=referer)
                            )
                        )
                    except DownloaderError as exc:
                        log.warning("Falha ao resolver HLS Brightcove %s: %s", src, exc)
                    if len(formats) == initial_count:
                        formats.append(
                            StreamFormat(
                                id=f"bc-hls-{index}",
                                url=src,
                                media_type=MediaType.VIDEO,
                                container=Container.TS,
                                protocol=Protocol.HLS,
                                manifest_url=src,
                                manifest_kind="hls",
                                unavailable=protected,
                                unavailable_reason="DRM não suportado" if protected else None,
                            )
                        )
                    elif protected:
                        for fmt in formats[initial_count:]:
                            fmt.unavailable = True
                            fmt.unavailable_reason = "DRM não suportado"
                    continue
                if "dash+xml" in source_type or path.endswith(".mpd"):
                    protected = self._is_drm_protected(source, path)
                    initial_count = len(formats)
                    try:
                        formats.extend(
                            await self.formats_from_manifest(
                                src, ctx, headers=ctx.request_headers(referer=referer)
                            )
                        )
                    except DownloaderError as exc:
                        log.warning("Falha ao resolver DASH Brightcove %s: %s", src, exc)
                    if len(formats) == initial_count:
                        formats.append(
                            StreamFormat(
                                id=f"bc-dash-{index}",
                                url=src,
                                media_type=MediaType.VIDEO,
                                container=Container.MP4,
                                protocol=Protocol.DASH,
                                manifest_url=src,
                                manifest_kind="dash",
                                unavailable=protected,
                                unavailable_reason="DRM não suportado" if protected else None,
                            )
                        )
                    elif protected:
                        for fmt in formats[initial_count:]:
                            fmt.unavailable = True
                            fmt.unavailable_reason = "DRM não suportado"
                    continue

                extension = path.rsplit(".", 1)[-1] if "." in path else ""
                width = self._positive_int(source.get("width"))
                height = self._positive_int(source.get("height"))
                audio_only = width == 0 and height == 0
                bitrate = self._number(source.get("avg_bitrate"))
                protected = self._is_drm_protected(source, path)
                formats.append(
                    StreamFormat(
                        id=f"bc-{source.get('codec') or extension or index}-{height or 'audio'}",
                        url=src,
                        media_type=MediaType.AUDIO if audio_only else MediaType.VIDEO,
                        container=CONTAINERS.get(extension, Container.UNKNOWN),
                        protocol=Protocol.HTTPS if src.startswith("https:") else Protocol.HTTP,
                        width=width if width else None,
                        height=height if height else None,
                        tbr=bitrate / 1000 if bitrate is not None else None,
                        filesize=self._positive_int(source.get("size")),
                        vcodec="none" if audio_only else source.get("codec"),
                        extractor_data={
                            "container": source.get("container"),
                            "drm": protected,
                        },
                        unavailable=protected,
                        unavailable_reason="DRM não suportado" if protected else None,
                    )
                )

        text_tracks = payload.get("text_tracks")
        if isinstance(text_tracks, list):
            for track in text_tracks:
                if not isinstance(track, dict) or track.get("kind") != "captions":
                    continue
                track_url = track.get("src")
                if not isinstance(track_url, str) or not track_url:
                    continue
                language = str(track.get("srclang") or track.get("label") or "en").lower()
                subtitles.setdefault(language, []).append(
                    SubtitleTrack(
                        language=language,
                        name=str(track["label"]) if track.get("label") is not None else None,
                        ext=urlsplit(track_url).path.rsplit(".", 1)[-1]
                        if "." in urlsplit(track_url).path
                        else "vtt",
                        url=track_url,
                    )
                )
        return formats, subtitles

    @staticmethod
    def _is_drm_protected(source: dict, path: str) -> bool:
        return bool(source.get("key_systems")) or source.get("container") == "WVM" or path.endswith(".ism")

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @classmethod
    def _positive_int(cls, value: object) -> int | None:
        number = cls._number(value)
        return int(number) if number is not None and number > 0 else None

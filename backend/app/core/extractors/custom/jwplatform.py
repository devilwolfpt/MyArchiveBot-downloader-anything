"""Custom Jwplatform Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor para media publicados através da API JW Platform."""

import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from app.core.exceptions import DownloaderError, ExtractionError
from app.core.models import (
    Container,
    MediaInfo,
    MediaType,
    Protocol,
    StreamFormat,
    SubtitleTrack,
)
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

JW_MEDIA_ID_RE = re.compile(
    r"(?:/players/|/v2/media/|/feeds/|/previews/|/thumbs/)"
    r"(?P<id>[a-zA-Z0-9]{8})(?:[-./]|$)",
    re.IGNORECASE,
)

_CONTAINERS: dict[str, Container] = {
    ".mp4": Container.MP4,
    ".m4v": Container.MP4,
    ".webm": Container.WEBM,
    ".mov": Container.MOV,
    ".mkv": Container.MKV,
    ".flv": Container.FLV,
    ".ts": Container.TS,
    ".mp3": Container.MP3,
    ".m4a": Container.M4A,
    ".aac": Container.M4A,
    ".opus": Container.OPUS,
    ".ogg": Container.OGG,
    ".wav": Container.WAV,
}


class JWPlatformExtractor(BaseExtractor):
    name = "jwplatform"
    priority = 25
    patterns = [
        r"^https?://(?:content\.jwplatform\.com|cdn\.jwplayer\.com)/"
        r"(?:players|v2/media|feeds|previews|thumbs)/[a-zA-Z0-9]{8}(?:[-./]|$)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        match = JW_MEDIA_ID_RE.search(urlsplit(url).path)
        if not match:
            raise ExtractionError("Não foi possível identificar o media ID JW Platform")

        media_id = match.group("id")
        api_url = f"https://cdn.jwplayer.com/v2/media/{media_id}"
        result = await ctx.http.get(
            api_url,
            headers=ctx.request_headers(referer=url),
            max_bytes=ctx.max_page_bytes,
        )
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "A API JW Platform devolveu JSON inválido",
                details={"media_id": media_id},
            ) from exc

        if not isinstance(payload, dict):
            raise ExtractionError(
                "A API JW Platform devolveu metadados inválidos",
                details={"media_id": media_id},
            )

        playlist = payload.get("playlist")
        items = playlist if isinstance(playlist, list) else [playlist or payload]
        formats: list[StreamFormat] = []
        subtitles: dict[str, list[SubtitleTrack]] = {}
        metadata: dict = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            if not metadata:
                metadata = item

            sources = item.get("sources")
            if not isinstance(sources, list):
                sources = [item] if item.get("file") else []
            for index, source in enumerate(sources):
                if not isinstance(source, dict):
                    continue
                stream_url = source.get("file")
                if not isinstance(stream_url, str) or not stream_url.strip():
                    continue
                stream_url = urljoin(api_url, stream_url.strip())
                source_type = str(source.get("type") or "").lower()
                path = urlsplit(stream_url).path.lower()
                extension = next(
                    (suffix for suffix in _CONTAINERS if path.endswith(suffix)),
                    "",
                )
                is_hls = extension == ".m3u8" or source_type in {
                    "hls",
                    "application/vnd.apple.mpegurl",
                    "application/x-mpegurl",
                }
                is_dash = extension == ".mpd" or source_type in {
                    "dash",
                    "application/dash+xml",
                }

                if is_hls or is_dash:
                    kind = "hls" if is_hls else "dash"
                    try:
                        resolved = await self.formats_from_manifest(
                            stream_url,
                            ctx,
                            headers=ctx.request_headers(referer=url),
                        )
                    except DownloaderError as exc:
                        log.warning("Não foi possível expandir manifesto JW Platform %s: %s", stream_url, exc)
                        resolved = []
                    if resolved:
                        formats.extend(resolved)
                    else:
                        formats.append(
                            StreamFormat(
                                id=f"jw-{kind}-{index}",
                                url=stream_url,
                                media_type=MediaType.VIDEO,
                                container=Container.TS if is_hls else Container.MP4,
                                protocol=Protocol.HLS if is_hls else Protocol.DASH,
                                manifest_url=stream_url,
                                manifest_kind=kind,
                            )
                        )
                    continue

                is_audio = source_type.startswith("audio/") or extension in {
                    ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav",
                }
                height = self._positive_int(source.get("height"))
                if height is None:
                    height = self._height_from_label(source.get("label"))
                formats.append(
                    StreamFormat(
                        id=f"jw-{source.get('label') or index}",
                        url=stream_url,
                        media_type=MediaType.AUDIO if is_audio else MediaType.VIDEO,
                        container=_CONTAINERS.get(extension, Container.UNKNOWN),
                        protocol=Protocol.HTTPS if stream_url.startswith("https:") else Protocol.HTTP,
                        width=self._positive_int(source.get("width")),
                        height=height,
                        tbr=self._bitrate_kbps(source.get("bitrate")),
                        filesize=self._positive_int(source.get("filesize")),
                        vcodec="none" if is_audio else None,
                        extractor_data={"source_type": source_type},
                    )
                )

            tracks = item.get("tracks")
            if isinstance(tracks, list):
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    kind = str(track.get("kind") or "").lower()
                    track_url = track.get("file")
                    if kind not in {"captions", "subtitles"} or not isinstance(track_url, str):
                        continue
                    language = str(
                        track.get("language") or track.get("lang") or track.get("label") or "und"
                    )
                    absolute_track_url = urljoin(api_url, track_url)
                    ext = urlsplit(absolute_track_url).path.rsplit(".", 1)
                    subtitles.setdefault(language, []).append(
                        SubtitleTrack(
                            language=language,
                            name=(
                                str(track["label"])
                                if track.get("label") is not None
                                else None
                            ),
                            ext=ext[-1].lower() if len(ext) == 2 else "vtt",
                            url=absolute_track_url,
                        )
                    )

        if not formats:
            raise ExtractionError(
                "JW Platform não devolveu formatos media",
                details={"media_id": media_id},
            )

        duration = payload.get("duration") or metadata.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None

        title = metadata.get("title") or payload.get("title") or media_id
        return MediaInfo(
            extractor=self.name,
            webpage_url=result.url_final or url,
            original_url=url,
            title=str(title),
            description=metadata.get("description") or payload.get("description"),
            thumbnail=metadata.get("image") or payload.get("image"),
            duration=duration,
            formats=formats,
            subtitles=subtitles,
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value) if value > 0 else None
        if not isinstance(value, str):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _bitrate_kbps(cls, value: object) -> float | None:
        bitrate = cls._positive_int(value)
        return bitrate / 1000 if bitrate is not None else None

    @classmethod
    def _height_from_label(cls, value: object) -> int | None:
        if not isinstance(value, str):
            return None
        match = re.search(r"(?<![\w.])(\d{3,4})(?:\s*p)?(?![\w.])", value, re.IGNORECASE)
        return cls._positive_int(match.group(1)) if match else None

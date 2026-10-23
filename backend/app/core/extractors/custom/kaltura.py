"""Custom Kaltura Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor para vídeos alojados no Kaltura."""

import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

from app.core.exceptions import DownloaderError, ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat, SubtitleTrack
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

KALTURA_PATH_RE = re.compile(
    r"/(?:index\.php/(?:kwidget|extwidget/preview)|html5/html5lib/[^/]+/mwEmbedFrame\.php)"
    r"(?:/(?P<path>[^?#]*))?",
    re.IGNORECASE,
)
CONTAINERS: dict[str, Container] = {
    "mp4": Container.MP4,
    "m4v": Container.MP4,
    "webm": Container.WEBM,
    "mov": Container.MOV,
    "mkv": Container.MKV,
    "flv": Container.FLV,
    "mp3": Container.MP3,
    "m4a": Container.M4A,
    "aac": Container.M4A,
    "ogg": Container.OGG,
    "wav": Container.WAV,
}


class KalturaExtractor(BaseExtractor):
    name = "kaltura"
    priority = 23
    patterns = [
        r"^https?://(?:(?:www|cdnapi(?:sec)?)\.)?kaltura\.com(?::\d+)?/"
        r"(?:index\.php/(?:kwidget|extwidget/preview)|html5/html5lib/[^/]+/mwEmbedFrame\.php)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        partner_id, entry_id, session_key = self._parse_url(url)
        if not partner_id or not entry_id:
            raise ExtractionError(
                "Não foi possível identificar o partner ID e entry ID Kaltura",
                details={"url": url},
            )

        api_url = "https://cdnapisec.kaltura.com/api_v3/service/multirequest"
        payload = self._build_multirequest(partner_id, entry_id)
        result = await ctx.http.post_json(
            api_url,
            payload,
            headers={
                **ctx.request_headers(referer=url),
                "Content-Type": "application/json",
            },
        )
        info, flavor_assets, captions = self._unpack_response(result)
        if not isinstance(info, dict):
            raise ExtractionError(
                "A API Kaltura não devolveu metadados do vídeo",
                details={"entry_id": entry_id},
            )

        formats = await self._build_formats(
            info, flavor_assets, session_key, ctx, url, entry_id
        )
        subtitles = self._build_subtitles(captions)
        if not formats:
            raise ExtractionError(
                "Kaltura não devolveu formatos media disponíveis",
                details={"entry_id": entry_id},
            )
        duration = self._number(info.get("duration"))
        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=str(info.get("name") or entry_id),
            description=info.get("description"),
            thumbnail=info.get("thumbnailUrl"),
            duration=duration,
            uploader_id=str(info["userId"]) if info.get("userId") else None,
            view_count=self._positive_int(info.get("plays")),
            formats=formats,
            subtitles=subtitles,
        )

    @staticmethod
    def _parse_url(url: str) -> tuple[str | None, str | None, str | None]:
        parts = urlsplit(url)
        path_match = KALTURA_PATH_RE.search(parts.path)
        if not path_match:
            return None, None, None
        params = parse_qs(parts.query, keep_blank_values=True)
        path_values: dict[str, str] = {}
        path = path_match.group("path") or ""
        segments = path.strip("/").split("/") if path else []
        for key, value in zip(segments[::2], segments[1::2]):
            path_values[key] = value

        partner_id = (
            (params.get("wid") or [None])[0]
            or (params.get("partner_id") or [None])[0]
            or (params.get("p") or [None])[0]
            or path_values.get("partner_id")
            or path_values.get("p")
        )
        if partner_id:
            partner_id = partner_id.removeprefix("_")
        entry_id = (params.get("entry_id") or [None])[0] or path_values.get("entry_id")
        session_key = (params.get("flashvars[ks]") or [None])[0]
        return partner_id, entry_id, session_key

    @staticmethod
    def _build_multirequest(partner_id: str, entry_id: str) -> dict[str, object]:
        widget_id = partner_id if "_" in partner_id else f"_{partner_id}"
        actions: list[dict[str, object]] = [
            {
                "apiVersion": "3.3.0",
                "clientTag": "html5:v3.1.0",
                "format": 1,
                "ks": "",
                "partnerId": partner_id,
            },
            {
                "expiry": 86400,
                "service": "session",
                "action": "startWidgetSession",
                "widgetId": widget_id,
            },
            {
                "action": "list",
                "filter": {"redirectFromEntryId": entry_id},
                "service": "baseentry",
                "ks": "{1:result:ks}",
                "responseProfile": {
                    "type": 1,
                    "fields": "createdAt,dataUrl,duration,name,plays,thumbnailUrl,userId,description",
                },
            },
            {
                "action": "getbyentryid",
                "entryId": entry_id,
                "service": "flavorAsset",
                "ks": "{1:result:ks}",
            },
            {
                "action": "list",
                "filter:entryIdEqual": entry_id,
                "service": "caption_captionasset",
                "ks": "{1:result:ks}",
            },
        ]
        request: dict[str, object] = actions[0].copy()
        request.update({str(index): action for index, action in enumerate(actions[1:], 1)})
        return request

    @staticmethod
    def _unpack_response(response: object) -> tuple[dict | None, list, dict | None]:
        if not isinstance(response, list) or len(response) < 4:
            raise ExtractionError("A API Kaltura devolveu uma resposta incompleta")
        for item in response:
            if isinstance(item, dict) and item.get("objectType") == "KalturaAPIException":
                raise ExtractionError(
                    f"Kaltura: {item.get('message') or 'erro na API'}",
                    details={"code": item.get("code")},
                )
        raw_info = response[1]
        if isinstance(raw_info, dict) and isinstance(raw_info.get("objects"), list):
            info = next((obj for obj in raw_info["objects"] if isinstance(obj, dict)), None)
        else:
            info = raw_info if isinstance(raw_info, dict) else None
        raw_assets = response[2]
        assets = raw_assets if isinstance(raw_assets, list) else []
        raw_captions = response[3]
        captions = raw_captions if isinstance(raw_captions, dict) else None
        return info, assets, captions

    async def _build_formats(
        self,
        info: dict,
        assets: list,
        session_key: str | None,
        ctx: ExtractContext,
        referer: str,
        entry_id: str,
    ) -> list[StreamFormat]:
        data_url = info.get("dataUrl")
        if not isinstance(data_url, str) or not data_url:
            return []
        if "/flvclipper/" in data_url:
            data_url = re.sub(r"/flvclipper/.*", "/serveFlavor", data_url)
        data_url = data_url.replace("http://", "https://", 1)
        base_url = f"{data_url}/ks/{quote(session_key, safe='')}" if session_key else data_url
        formats: list[StreamFormat] = []
        saw_drm = False

        for asset in assets:
            if not isinstance(asset, dict) or asset.get("status") != 2:
                continue
            extension = str(asset.get("fileExt") or "").lower()
            if extension == "wvm":
                saw_drm = True
                continue
            if extension == "chun":
                continue
            extension = extension or ("mov" if asset.get("containerFormat") == "qt" else "mp4")
            asset_id = asset.get("id")
            if not asset_id:
                continue
            stream_url = f"{base_url}/flavorId/{quote(str(asset_id), safe='')}"
            is_audio = (
                "videoCodecId" not in asset
                and self._number(asset.get("frameRate")) == 0
            )
            bitrate = self._number(asset.get("bitrate"))
            size_kib = self._number(asset.get("size"))
            formats.append(
                StreamFormat(
                    id=f"kaltura-{extension}-{asset.get('bitrate') or asset_id}",
                    url=stream_url,
                    media_type=MediaType.AUDIO if is_audio else MediaType.VIDEO,
                    container=CONTAINERS.get(extension, Container.UNKNOWN),
                    protocol=Protocol.HTTPS,
                    width=self._positive_int(asset.get("width")),
                    height=self._positive_int(asset.get("height")),
                    fps=self._number(asset.get("frameRate")),
                    tbr=bitrate,
                    filesize_approx=int(size_kib * 1024) if size_kib else None,
                    vcodec="none" if is_audio else (
                        str(asset["videoCodecId"]) if asset.get("videoCodecId") else None
                    ),
                    extractor_data={"asset_id": str(asset_id)},
                )
            )

        if "/playManifest/" in data_url:
            manifest_url = data_url.replace("format/url", "format/applehttp")
            if session_key:
                manifest_url = f"{manifest_url}/ks/{quote(session_key, safe='')}"
            try:
                resolved = await self.formats_from_manifest(
                    manifest_url,
                    ctx,
                    headers=ctx.request_headers(referer=referer),
                )
                formats.extend(resolved)
                if not resolved:
                    formats.append(
                        StreamFormat(
                            id="kaltura-hls",
                            url=manifest_url,
                            media_type=MediaType.VIDEO,
                            container=Container.TS,
                            protocol=Protocol.HLS,
                            manifest_url=manifest_url,
                            manifest_kind="hls",
                        )
                    )
            except DownloaderError as exc:
                log.warning("Não foi possível resolver HLS Kaltura para %s: %s", entry_id, exc)
                formats.append(
                    StreamFormat(
                        id="kaltura-hls",
                        url=manifest_url,
                        media_type=MediaType.VIDEO,
                        container=Container.TS,
                        protocol=Protocol.HLS,
                        manifest_url=manifest_url,
                        manifest_kind="hls",
                    )
                )
        if not formats and saw_drm:
            raise ExtractionError(
                "O vídeo Kaltura só disponibiliza assets protegidos por DRM, que não é suportado",
                details={"entry_id": entry_id},
            )
        return formats

    @staticmethod
    def _build_subtitles(captions: dict | None) -> dict[str, list[SubtitleTrack]]:
        tracks: dict[str, list[SubtitleTrack]] = {}
        objects = captions.get("objects", []) if captions else []
        if not isinstance(objects, list):
            return tracks
        extensions = {1: "srt", 2: "ttml", 3: "vtt"}
        for caption in objects:
            if not isinstance(caption, dict) or caption.get("status") != 2 or not caption.get("id"):
                continue
            language = str(caption.get("languageCode") or caption.get("language") or "und")
            extension = caption.get("fileExt") or extensions.get(
                KalturaExtractor._positive_int(caption.get("format")) or 0,
                "ttml",
            )
            tracks.setdefault(language, []).append(
                SubtitleTrack(
                    language=language,
                    ext=str(extension),
                    url=(
                        "https://cdnapisec.kaltura.com/api_v3/service/"
                        "caption_captionasset/action/serve/captionAssetId/"
                        f"{quote(str(caption['id']), safe='')}"
                    ),
                )
            )
        return tracks

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

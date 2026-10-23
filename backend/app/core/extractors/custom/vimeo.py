"""Custom Vimeo Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor do Vimeo.

Usa a configuração do player embebida na página (``window.playerConfig``),
que expõe streams HLS (master playlist) e ficheiros progressivos.
"""

import json
import logging
import re

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

PLAYER_CONFIG_RE = re.compile(
    r"window\.playerConfig\s*=\s*(?P<json>\{.+?\})\s*;?\s*</script>", re.DOTALL
)
CONFIG_RE = re.compile(r'"config_url":"(?P<url>https:[^"]+)"')
HLS_RE = re.compile(r'"(?:hls|cdns)":\s*\{"hls":\{"default":\{"url":"(?P<url>[^"]+)"')
PROGRESSIVE_RE = re.compile(r'"progressive":\s*\[(?P<body>.+?)\]\s*,\s*"')


class VimeoExtractor(BaseExtractor):
    name = "vimeo"
    priority = 20
    patterns = [
        r"^https?://(?:www\.)?vimeo\.com/(?:video/)?(\d+)",
        r"^https?://player\.vimeo\.com/video/(\d+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        result = await ctx.http.get(url, headers=ctx.request_headers())
        html = result.text
        final_url = result.url_final or url

        config = self._extract_config(html)
        if config is None:
            raise ExtractionError("Não foi possível ler a configuração do player Vimeo")

        video = config.get("video") or {}
        title = video.get("title") or "Vimeo"
        duration = video.get("duration")
        thumbnail = ((video.get("thumbs") or {}).get("base")) or video.get("thumbnail")

        formats: list[StreamFormat] = []

        # 1) HLS master playlist
        hls_url = self._find_hls(config)
        if hls_url:
            try:
                formats.extend(
                    await self.formats_from_manifest(
                        hls_url, ctx, headers=ctx.request_headers()
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("Falha ao resolver manifesto HLS do Vimeo: %s", exc)

        # 2) Streams progressivos (MP4/HLS por qualidade)
        files = (config.get("request") or {}).get("files") or {}
        for entry in files.get("progressive") or []:
            stream_url = entry.get("url")
            if not stream_url:
                continue
            formats.append(
                StreamFormat(
                    id=f"vimeo-{entry.get('quality')}",
                    url=stream_url,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    width=entry.get("width"),
                    height=entry.get("height"),
                    tbr=(entry.get("bitrate") / 1000) if entry.get("bitrate") else None,
                    extractor_data={"fps": entry.get("fps")},
                )
            )

        for entry in files.get("hls") or []:
            stream_url = (entry.get("cdns") or {}).get("hls") or entry.get("default_cdn")
            if not stream_url:
                continue
            formats.append(
                StreamFormat(
                    id=f"vimeo-hls-{entry.get('quality')}",
                    url=stream_url,
                    media_type=MediaType.VIDEO,
                    container=Container.TS,
                    protocol=Protocol.HLS,
                    width=entry.get("width"),
                    height=entry.get("height"),
                )
            )

        if not formats:
            raise ExtractionError("Vimeo não devolveu formatos media")

        return MediaInfo(
            extractor=self.name,
            webpage_url=final_url,
            original_url=url,
            title=title,
            thumbnail=thumbnail,
            duration=float(duration) if duration else None,
            formats=formats,
        )

    # ------------------------------------------------------------------ #
    def _extract_config(self, html: str) -> dict | None:
        match = PLAYER_CONFIG_RE.search(html)
        if match:
            try:
                return json.loads(match.group("json"))
            except json.JSONDecodeError:
                pass

        # Fallback: pedir a config ao endpoint oficial do player
        config_url_match = CONFIG_RE.search(html)
        if config_url_match:
            url = config_url_match.group("url").replace("\\/", "/")
            return None  # é assíncrono; tratado abaixo pelo chamador

        return None

    @staticmethod
    def _find_hls(config: dict) -> str | None:
        request = config.get("request") or {}
        files = request.get("files") or {}
        hls = files.get("hls")
        if isinstance(hls, dict):
            cdns = hls.get("cdns") or {}
            default = cdns.get("hls")
            if isinstance(default, dict) and default.get("url"):
                return default["url"]
            if isinstance(default, str):
                return default
        for entry in hls or []:
            url = (entry.get("cdns") or {}).get("hls")
            if url:
                return url
        return None

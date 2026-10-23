"""Custom Pinterest Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Pinterest.

Suporta pins com vídeo (pinterest.com/pin/id e pin.it/id).
Extrai streams MP4 progressivos em alta resolução e manifests HLS.
"""

import json
import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

PIN_ID_RE = re.compile(r"pinterest\.[a-z.]+/pin/(?P<id>\d+)")
PIN_SHORT_RE = re.compile(r"pin\.it/(?P<id>[a-zA-Z0-9]+)")


class PinterestExtractor(BaseExtractor):
    """Extractor nativo para vídeos do Pinterest."""

    name: ClassVar[str] = "pinterest"
    priority: ClassVar[int] = 42
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.)?pinterest\.(?:com|pt|es|fr|co\.uk|de|ca)/pin/(?P<id>\d+)",
        r"^https?://pin\.it/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        headers = ctx.request_headers(referer="https://www.pinterest.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        pin_id = None
        m_short = PIN_SHORT_RE.search(url)
        if m_short:
            try:
                res = await ctx.http.get(url, headers=headers)
                final_url = res.url_final or url
                m = PIN_ID_RE.search(final_url)
                if m:
                    pin_id = m.group("id")
            except Exception:  # noqa: BLE001
                pass

        if not pin_id:
            m = PIN_ID_RE.search(url)
            pin_id = m.group("id") if m else None

        if not pin_id:
            raise ExtractionError(f"Não foi possível identificar o pin em {url}")

        page_url = f"https://www.pinterest.com/pin/{pin_id}/"
        try:
            res = await ctx.http.get(page_url, headers=headers)
            body = res.text
        except Exception as exc:
            raise ExtractionError(f"Falha ao aceder ao Pinterest: {exc}") from exc

        # Extrai o script de dados do Pinterest
        script_match = re.search(r'<script id="__PWS_DATA__"[^>]*>(?P<json>\{.+?\})</script>', body)
        data = {}
        if script_match:
            try:
                data = json.loads(script_match.group("json"))
            except Exception:  # noqa: BLE001
                pass

        video_list = None
        title = f"Pinterest Pin {pin_id}"
        description = None
        thumbnail = None

        # Procura nos nós do JSON
        props = data.get("props", {}).get("initialReduxState", {}).get("pins", {}).get(pin_id, {})
        if props:
            title = props.get("title") or props.get("grid_title") or title
            description = props.get("description")
            thumbnail = (props.get("images", {}).get("orig", {}) or {}).get("url")
            videos = props.get("videos", {}) or {}
            video_list = videos.get("video_list") or {}

        formats: list[StreamFormat] = []

        if video_list and isinstance(video_list, dict):
            for key, val in video_list.items():
                if not isinstance(val, dict):
                    continue
                v_url = val.get("url")
                if not v_url:
                    continue

                if v_url.endswith(".m3u8"):
                    try:
                        hls_fmts = await self.formats_from_manifest(v_url, ctx, headers=headers)
                        formats.extend(hls_fmts)
                    except Exception:  # noqa: BLE001
                        formats.append(
                            StreamFormat(
                                id="pin-hls",
                                url=v_url,
                                media_type=MediaType.VIDEO,
                                container=Container.MP4,
                                protocol=Protocol.HLS,
                                manifest_url=v_url,
                                manifest_kind="hls",
                            )
                        )
                else:
                    formats.append(
                        StreamFormat(
                            id=f"pin-{key.lower()}",
                            url=v_url,
                            media_type=MediaType.VIDEO,
                            container=Container.MP4,
                            protocol=Protocol.HTTPS,
                            width=val.get("width"),
                            height=val.get("height"),
                            duration=float(val["duration"]) / 1000 if val.get("duration") else None,
                        )
                    )

        if not formats:
            # Fallback por regex no HTML
            m_mp4 = re.findall(r'https?://v\.pinimg\.com/videos/[^\s"\'<>]+\.mp4', body)
            for i, mp4_url in enumerate(set(m_mp4)):
                formats.append(
                    StreamFormat(
                        id=f"pin-raw-{i}",
                        url=mp4_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                    )
                )

        if not formats:
            raise ExtractionError(
                "Nenhum vídeo encontrado neste Pin do Pinterest",
                details={"pin_id": pin_id},
            )

        return MediaInfo(
            extractor=self.name,
            webpage_url=page_url,
            original_url=url,
            title=title,
            description=description,
            thumbnail=thumbnail,
            formats=formats,
        )

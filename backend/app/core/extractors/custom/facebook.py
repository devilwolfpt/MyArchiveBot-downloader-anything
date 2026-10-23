"""Custom Facebook Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Facebook.

Suporta vídeos e Reels do Facebook (facebook.com/watch, fb.watch, /reel/).
Extrai variantes em alta definição (HD) e definição padrão (SD).
"""

import html
import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

FB_HD_RE = re.compile(r'["\']playable_url_quality_hd["\']:\s*["\'](?P<url>https:[^"\']+)["\']')
FB_SD_RE = re.compile(r'["\']playable_url["\']:\s*["\'](?P<url>https:[^"\']+)["\']')


class FacebookExtractor(BaseExtractor):
    """Extractor nativo para vídeos do Facebook."""

    name: ClassVar[str] = "facebook"
    priority: ClassVar[int] = 41
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:watch/?\?v=|video\.php\?v=|[^/?#&]+/videos/|reel/)(?P<id>\d+)",
        r"^https?://fb\.watch/(?P<id>[a-zA-Z0-9_-]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        headers = ctx.request_headers(referer="https://www.facebook.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        try:
            res = await ctx.http.get(url, headers=headers)
            body = res.text
        except Exception as exc:
            raise ExtractionError(f"Falha ao aceder ao Facebook: {exc}") from exc

        formats: list[StreamFormat] = []

        hd_match = FB_HD_RE.search(body)
        if hd_match:
            formats.append(
                StreamFormat(
                    id="fb-hd",
                    url=self._clean_url(hd_match.group("url")),
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    height=720,
                )
            )

        sd_match = FB_SD_RE.search(body)
        if sd_match:
            formats.append(
                StreamFormat(
                    id="fb-sd",
                    url=self._clean_url(sd_match.group("url")),
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    height=360,
                )
            )

        if not formats:
            # Fallback para meta tags OpenGraph
            og_match = re.search(r'<meta property="og:video" content="(?P<url>[^"]+)"', body)
            if og_match:
                formats.append(
                    StreamFormat(
                        id="fb-og",
                        url=html.unescape(og_match.group("url")),
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                    )
                )

        if not formats:
            raise ExtractionError(
                "Não foi possível extrair o vídeo do Facebook (pode ser privado ou requerer sessão)",
                details={"url": url},
            )

        title_match = re.search(r'<meta property="og:title" content="(?P<title>[^"]+)"', body)
        title = html.unescape(title_match.group("title")) if title_match else "Vídeo do Facebook"

        thumb_match = re.search(r'<meta property="og:image" content="(?P<thumb>[^"]+)"', body)
        thumbnail = html.unescape(thumb_match.group("thumb")) if thumb_match else None

        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=title,
            thumbnail=thumbnail,
            formats=formats,
        )

    @staticmethod
    def _clean_url(raw: str) -> str:
        clean = raw.replace(r"\/", "/").replace(r"\u0026", "&")
        return html.unescape(clean)

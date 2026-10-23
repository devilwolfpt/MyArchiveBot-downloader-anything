"""Custom Instagram Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Instagram.

Suporta Reels, vídeos e IGTV (instagram.com/p/, /reel/, /tv/).
Extrai o stream MP4 progressivo original e a capa.
"""

import html
import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

INSTA_ID_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]+)")
VIDEO_URL_RE = re.compile(r'\\?"video_url\\?":\s*\\?"(?P<url>https:[^"\\]+(?:\\.[^"\\]*)*)\\?"')
DISPLAY_URL_RE = re.compile(r'\\?"display_url\\?":\s*\\?"(?P<url>https:[^"\\]+(?:\\.[^"\\]*)*)\\?"')
USERNAME_RE = re.compile(r'\\?"username\\?":\s*\\?"(?P<user>[^"\\]+)\\?"')


class InstagramExtractor(BaseExtractor):
    """Extractor para vídeos e Reels do Instagram."""

    name: ClassVar[str] = "instagram"
    priority: ClassVar[int] = 38
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        match = INSTA_ID_RE.search(url)
        if not match:
            raise ExtractionError(f"URL de Instagram inválido: {url}")

        media_id = match.group("id")
        embed_url = f"https://www.instagram.com/p/{media_id}/embed/captioned/"
        headers = ctx.request_headers(referer="https://www.instagram.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        try:
            res = await ctx.http.get(embed_url, headers=headers)
            body = res.text
        except Exception as exc:
            raise ExtractionError(f"Falha ao aceder ao Instagram: {exc}") from exc

        video_match = VIDEO_URL_RE.search(body)
        if not video_match:
            # Tenta verificar se o link não é um vídeo
            if "display_url" in body:
                raise ExtractionError(
                    "Esta publicação do Instagram é uma imagem, não um vídeo",
                    details={"media_id": media_id},
                )
            raise ExtractionError(
                "Não foi possível obter o vídeo do Instagram (pode ser privado ou requerer login)",
                details={"media_id": media_id},
            )

        video_url = self._clean_url(video_match.group("url"))
        display_match = DISPLAY_URL_RE.search(body)
        thumbnail = self._clean_url(display_match.group("url")) if display_match else None

        user_match = USERNAME_RE.search(body)
        uploader = user_match.group("user") if user_match else None

        formats = [
            StreamFormat(
                id="insta-mp4",
                url=video_url,
                media_type=MediaType.VIDEO,
                container=Container.MP4,
                protocol=Protocol.HTTPS,
                extractor_data={"http_headers": {"Referer": "https://www.instagram.com/"}},
            )
        ]

        title = f"Instagram de @{uploader}" if uploader else f"Instagram {media_id}"

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.instagram.com/p/{media_id}/",
            original_url=url,
            title=title,
            thumbnail=thumbnail,
            uploader=uploader,
            formats=formats,
        )

    @staticmethod
    def _clean_url(raw: str) -> str:
        clean = raw.replace(r"\/", "/").replace(r"\u0026", "&")
        return html.unescape(clean)

"""Extractor para URLs de media directa (mp4, mp3, m3u8, mpd...)."""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

from ..models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from .base import BaseExtractor, ExtractContext

EXTENSION_MAP: dict[str, tuple[MediaType, Container]] = {
    ".mp4": (MediaType.VIDEO, Container.MP4),
    ".m4v": (MediaType.VIDEO, Container.MP4),
    ".mov": (MediaType.VIDEO, Container.MOV),
    ".webm": (MediaType.VIDEO, Container.WEBM),
    ".mkv": (MediaType.VIDEO, Container.MKV),
    ".flv": (MediaType.VIDEO, Container.FLV),
    ".ts": (MediaType.VIDEO, Container.TS),
    ".avi": (MediaType.VIDEO, Container.UNKNOWN),
    ".mp3": (MediaType.AUDIO, Container.MP3),
    ".m4a": (MediaType.AUDIO, Container.M4A),
    ".aac": (MediaType.AUDIO, Container.M4A),
    ".opus": (MediaType.AUDIO, Container.OPUS),
    ".ogg": (MediaType.AUDIO, Container.OGG),
    ".wav": (MediaType.AUDIO, Container.WAV),
    ".flac": (MediaType.AUDIO, Container.UNKNOWN),
}


class DirectMediaExtractor(BaseExtractor):
    """Trata URLs que apontam directamente para um ficheiro de media."""

    name = "direct"
    priority = 10
    patterns = [
        r"\.(m3u8|mpd|mp4|m4v|mov|webm|mkv|flv|ts|avi|mp3|m4a|aac|opus|ogg|wav|flac)(\?.*)?$",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        path = urlsplit(url).path.lower()
        ext = ""
        for candidate in EXTENSION_MAP:
            if path.endswith(candidate):
                ext = candidate
                break

        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "media"

        if path.endswith(".m3u8"):
            formats = await self.formats_from_manifest(url, ctx)
            return MediaInfo(
                extractor=self.name,
                webpage_url=url,
                original_url=url,
                title=filename.rsplit(".", 1)[0],
                formats=formats,
                is_live=any(f.protocol is Protocol.HLS for f in formats),
            )

        if path.endswith(".mpd"):
            formats = await self.formats_from_manifest(url, ctx)
            return MediaInfo(
                extractor=self.name,
                webpage_url=url,
                original_url=url,
                title=filename.rsplit(".", 1)[0],
                formats=formats,
            )

        media_type, container = EXTENSION_MAP.get(ext, (MediaType.UNKNOWN, Container.UNKNOWN))
        formats = await self.formats_from_direct_url(
            url,
            ctx,
            media_type=media_type,
            container=container,
            headers=ctx.request_headers(),
        )
        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=filename.rsplit(".", 1)[0],
            formats=formats,
        )


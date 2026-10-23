"""Custom Tiktok Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do TikTok.

Suporta links padrão (tiktok.com/@user/video/id), mobile e encurtados (vm.tiktok.com/id).
Extrai streams MP4 progressivos de alta qualidade sem marca de água.
"""

import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"tiktok\.com/(?:@[^/?#&]+/video/|v/)(?P<id>\d+)")
SHORT_RE = re.compile(r"(?:vm|vt|www)\.tiktok\.com/(?:t/)?(?P<id>[a-zA-Z0-9]+)")


class TikTokExtractor(BaseExtractor):
    """Extractor para vídeos do TikTok."""

    name: ClassVar[str] = "tiktok"
    priority: ClassVar[int] = 35
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.|m\.)?tiktok\.com/(?:@[^/?#&]+/video/|v/)(?P<id>\d+)",
        r"^https?://(?:vm|vt)\.tiktok\.com/(?P<id>[a-zA-Z0-9]+)",
        r"^https?://(?:www\.)?tiktok\.com/t/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        video_id = await self._resolve_video_id(url, ctx)
        if not video_id:
            raise ExtractionError(f"Não foi possível obter o ID de vídeo do TikTok para {url}")

        api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}"
        headers = ctx.request_headers(referer="https://www.tiktok.com/")
        headers["User-Agent"] = (
            "com.ss.android.ugc.trill/260103 (Linux; U; Android 13; en_US; Pixel 7; Build/TD1A.220804.031; Cronet/58.0.2991.0)"
        )

        try:
            payload = await ctx.http.get_json(api_url, headers=headers)
        except Exception:
            # Fallback para embed HTML
            return await self._extract_embed_fallback(video_id, url, ctx)

        aweme_list = payload.get("aweme_list") if isinstance(payload, dict) else []
        if not aweme_list:
            return await self._extract_embed_fallback(video_id, url, ctx)

        aweme = aweme_list[0]
        video = aweme.get("video") or {}
        author = aweme.get("author") or {}

        formats: list[StreamFormat] = []

        # Streams de vídeo sem marca de água
        play_urls = (video.get("play_addr") or {}).get("url_list") or []
        download_urls = (video.get("download_addr") or {}).get("url_list") or []
        bitrate_list = video.get("bit_rate") or []

        for i, stream_url in enumerate(play_urls):
            formats.append(
                StreamFormat(
                    id=f"tiktok-play-{i}",
                    url=stream_url,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    width=video.get("width"),
                    height=video.get("height"),
                    duration=float(video["duration"]) / 1000 if video.get("duration") else None,
                    extractor_data={"http_headers": {"Referer": "https://www.tiktok.com/"}},
                )
            )

        for i, stream_url in enumerate(download_urls):
            formats.append(
                StreamFormat(
                    id=f"tiktok-download-{i}",
                    url=stream_url,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    width=video.get("width"),
                    height=video.get("height"),
                    duration=float(video["duration"]) / 1000 if video.get("duration") else None,
                    extractor_data={"http_headers": {"Referer": "https://www.tiktok.com/"}},
                )
            )

        if not formats:
            return await self._extract_embed_fallback(video_id, url, ctx)

        title = aweme.get("desc") or f"TikTok de @{author.get('unique_id', video_id)}"
        cover = (video.get("cover") or {}).get("url_list", [None])[0]

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.tiktok.com/@{author.get('unique_id', '_')}/video/{video_id}",
            original_url=url,
            title=title,
            description=aweme.get("desc"),
            thumbnail=cover,
            duration=float(video["duration"]) / 1000 if video.get("duration") else None,
            uploader=author.get("nickname") or author.get("unique_id"),
            uploader_id=author.get("unique_id"),
            formats=formats,
        )

    async def _resolve_video_id(self, url: str, ctx: ExtractContext) -> str | None:
        m = VIDEO_ID_RE.search(url)
        if m:
            return m.group("id")

        if SHORT_RE.search(url):
            try:
                res = await ctx.http.get(url, headers=ctx.request_headers())
                final_url = res.url_final or url
                m2 = VIDEO_ID_RE.search(final_url)
                if m2:
                    return m2.group("id")
            except Exception as exc:  # noqa: BLE001
                log.debug("Erro ao seguir redirect do TikTok: %s", exc)
        return None

    async def _extract_embed_fallback(self, video_id: str, original_url: str, ctx: ExtractContext) -> MediaInfo:
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        headers = ctx.request_headers(referer="https://www.tiktok.com/")
        res = await ctx.http.get(embed_url, headers=headers)
        html = res.text

        match = re.search(r'"playAddr":\s*"(?P<url>https:[^"]+)"', html)
        if not match:
            raise ExtractionError("Não foi possível extrair o vídeo do TikTok", details={"video_id": video_id})

        play_url = match.group("url").encode("utf-8").decode("unicode_escape")
        formats = [
            StreamFormat(
                id="tiktok-embed",
                url=play_url,
                media_type=MediaType.VIDEO,
                container=Container.MP4,
                protocol=Protocol.HTTPS,
                extractor_data={"http_headers": {"Referer": "https://www.tiktok.com/"}},
            )
        ]

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.tiktok.com/video/{video_id}",
            original_url=original_url,
            title=f"TikTok {video_id}",
            formats=formats,
        )

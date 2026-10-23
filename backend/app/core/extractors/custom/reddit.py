"""Custom Reddit Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Reddit.

Suporta links do reddit.com, old.reddit.com e v.redd.it.
Extrai vídeo DASH e HLS hospedados pelo Reddit com áudio e legendas.
"""

import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

POST_RE = re.compile(
    r"^https?://(?:\w+\.)?reddit(?:media)?\.com/(?:(?:r|user)/[^/]+/)?comments/(?P<id>[^/?#&]+)"
)
VREDDIT_RE = re.compile(r"^https?://(?:v\.|www\.)?redd\.it/(?P<id>[a-zA-Z0-9]+)")


class RedditExtractor(BaseExtractor):
    """Extractor nativo para publicações e vídeos do Reddit."""

    name: ClassVar[str] = "reddit"
    priority: ClassVar[int] = 37
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:\w+\.)?reddit(?:media)?\.com/(?:(?:r|user)/[^/]+/)?comments/(?P<id>[^/?#&]+)",
        r"^https?://v\.redd\.it/(?P<id>[a-zA-Z0-9]+)",
        r"^https?://(?:\w+\.)?reddit\.com/video/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        post_id = self._extract_id(url)
        if not post_id:
            raise ExtractionError(f"Não foi possível extrair o ID do Reddit em {url}")

        api_url = f"https://www.reddit.com/comments/{post_id}.json?raw_json=1"
        headers = ctx.request_headers(referer="https://www.reddit.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        try:
            payload = await ctx.http.get_json(api_url, headers=headers)
        except Exception as exc:
            # Tenta fallback via HTML
            return await self._extract_html_fallback(url, post_id, ctx)

        if not payload or not isinstance(payload, list):
            return await self._extract_html_fallback(url, post_id, ctx)

        try:
            data = payload[0]["data"]["children"][0]["data"]
        except (IndexError, KeyError, TypeError):
            return await self._extract_html_fallback(url, post_id, ctx)

        # Trata crossposts
        crosspost = data.get("crosspost_parent_list")
        if crosspost and isinstance(crosspost, list) and crosspost[0]:
            data = crosspost[0]

        media = data.get("secure_media") or data.get("media") or {}
        reddit_video = media.get("reddit_video") or {}

        formats: list[StreamFormat] = []

        # 1) HLS master
        hls_url = reddit_video.get("hls_url")
        if hls_url:
            try:
                formats.extend(await self.formats_from_manifest(hls_url, ctx, headers=headers))
            except Exception as exc:  # noqa: BLE001
                log.debug("Falha ao resolver HLS Reddit: %s", exc)

        # 2) DASH manifest
        dash_url = reddit_video.get("dash_url")
        if dash_url:
            try:
                formats.extend(await self.formats_from_manifest(dash_url, ctx, headers=headers))
            except Exception as exc:  # noqa: BLE001
                log.debug("Falha ao resolver DASH Reddit: %s", exc)

        # 3) Fallback MP4 progressivo
        fallback_url = reddit_video.get("fallback_url")
        if fallback_url:
            formats.append(
                StreamFormat(
                    id="reddit-fallback",
                    url=fallback_url,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    width=reddit_video.get("width"),
                    height=reddit_video.get("height"),
                    duration=float(reddit_video["duration"]) if reddit_video.get("duration") else None,
                )
            )

        if not formats:
            # Se for link para ficheiro directo ou outro player
            video_url = data.get("url")
            if video_url and (video_url.endswith(".mp4") or "v.redd.it" in video_url):
                return await self.formats_from_direct_url(video_url, ctx)
            raise ExtractionError(
                "O post do Reddit não contém um vídeo nativo compatível",
                details={"post_id": post_id},
            )

        title = data.get("title") or f"Reddit {post_id}"
        uploader = data.get("author")
        thumbnail = data.get("thumbnail") if (data.get("thumbnail") or "").startswith("http") else None

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.reddit.com/comments/{post_id}",
            original_url=url,
            title=title,
            description=data.get("selftext"),
            thumbnail=thumbnail,
            duration=float(reddit_video["duration"]) if reddit_video.get("duration") else None,
            uploader=uploader,
            view_count=data.get("score"),
            age_limit=18 if data.get("over_18") else None,
            formats=formats,
        )

    async def _extract_html_fallback(self, url: str, post_id: str, ctx: ExtractContext) -> MediaInfo:
        """Fallback via scraping simples da página do Reddit."""
        headers = ctx.request_headers(referer="https://www.reddit.com/")
        result = await ctx.http.get(url, headers=headers)
        html = result.text

        m3u8_match = re.search(r'https?://v\.redd\.it/[^"/]+/HLSPlaylist\.m3u8[^\s"\'<>]*', html)
        mp4_match = re.search(r'https?://v\.redd\.it/[^"/]+/DASH_[^\s"\'<>]*\.mp4', html)

        formats: list[StreamFormat] = []
        if m3u8_match:
            try:
                formats.extend(await self.formats_from_manifest(m3u8_match.group(0), ctx))
            except Exception:  # noqa: BLE001
                pass
        if mp4_match:
            formats.append(
                StreamFormat(
                    id="reddit-html-mp4",
                    url=mp4_match.group(0),
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                )
            )

        if not formats:
            raise ExtractionError("Não foi possível extrair vídeo do Reddit", details={"id": post_id})

        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=f"Reddit {post_id}",
            formats=formats,
        )

    @staticmethod
    def _extract_id(url: str) -> str | None:
        m = POST_RE.search(url)
        if m:
            return m.group("id")
        m = VREDDIT_RE.search(url)
        if m:
            return m.group("id")
        return None

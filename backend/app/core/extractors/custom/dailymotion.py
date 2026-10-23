"""Custom Dailymotion Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor do Dailymotion.

Usa a API pública de metadados do player:
``https://www.dailymotion.com/player/metadata/video/{id}``
que devolve as qualidades disponíveis (HLS) e thumbnails.
"""

import re

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

VIDEO_ID_RE = re.compile(r"dailymotion\.com/(?:video|embed/video)/(?P<id>[a-zA-Z0-9]+)")
QUALITY_MAP: dict[str, int] = {
    "144": 144,
    "240": 240,
    "380": 380,
    "480": 480,
    "720": 720,
    "1080": 1080,
    "2160": 2160,
}


class DailymotionExtractor(BaseExtractor):
    name = "dailymotion"
    priority = 30
    patterns = [
        r"^https?://(?:www\.)?dailymotion\.com/(?:video|embed/video)/[a-zA-Z0-9]+",
        r"^https?://(?:www\.)?dai\.ly/[a-zA-Z0-9]+",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        video_id = self._video_id(url)
        if not video_id:
            raise ExtractionError(f"Não foi possível extrair o videoId de {url}")

        metadata_url = f"https://www.dailymotion.com/player/metadata/video/{video_id}"
        payload = await ctx.http.post_json(
            metadata_url,
            {"country": "PT", "locale": "pt_PT"},
            headers=ctx.request_headers(referer="https://www.dailymotion.com/"),
        )

        qualities = payload.get("qualities") or {}
        formats: list[StreamFormat] = []
        for quality, entries in qualities.items():
            height = QUALITY_MAP.get(quality)
            for entry in entries or []:
                stream_url = entry.get("url")
                if not stream_url:
                    continue
                formats.append(
                    StreamFormat(
                        id=f"dm-{quality}",
                        url=stream_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                        height=height,
                        width=(height * 16 // 9) if height else None,
                        tbr=(entry.get("bitrate") / 1000) if entry.get("bitrate") else None,
                    )
                )

        if not formats:
            raise ExtractionError(
                "Dailymotion não devolveu formatos media", details={"video_id": video_id}
            )

        title = payload.get("title") or payload.get("owner", {}).get("screenname") or video_id
        poster = payload.get("poster_url") or payload.get("poster2_url")

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.dailymotion.com/video/{video_id}",
            original_url=url,
            title=title,
            description=payload.get("description"),
            thumbnail=poster,
            duration=float(payload["duration"]) if payload.get("duration") else None,
            uploader=payload.get("owner", {}).get("screenname"),
            formats=formats,
        )

    @staticmethod
    def _video_id(url: str) -> str | None:
        match = VIDEO_ID_RE.search(url)
        if match:
            return match.group("id")
        if "dai.ly" in url:
            return url.rstrip("/").rsplit("/", 1)[-1]
        return None

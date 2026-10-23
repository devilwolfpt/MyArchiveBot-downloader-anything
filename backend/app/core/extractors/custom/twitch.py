"""Custom Twitch Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo da Twitch.

Suporta Clips (clips.twitch.tv e twitch.tv/user/clip/id) e VODs (twitch.tv/videos/id).
Usa a API GraphQL oficial da Twitch para resolver tokens de playback e manifests HLS.
"""

import json
import logging
import re
from typing import ClassVar
from urllib.parse import quote

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

VOD_RE = re.compile(r"twitch\.tv/videos/(?P<id>\d+)")
CLIP_RE = re.compile(
    r"(?:clips\.twitch\.tv/|twitch\.tv/[^/?#&]+/clip/)(?P<id>[A-Za-z0-9_-]+)"
)


class TwitchExtractor(BaseExtractor):
    """Extractor nativo para Clips e Vídeos (VODs) da Twitch."""

    name: ClassVar[str] = "twitch"
    priority: ClassVar[int] = 39
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:(?:www|go|m)\.)?twitch\.tv/videos/(?P<id>\d+)",
        r"^https?://(?:clips\.)?twitch\.tv/(?:[^/?#&]+/clip/)?(?P<id>[A-Za-z0-9_-]+)",
        r"^https?://(?:(?:www|go|m)\.)?twitch\.tv/[^/?#&]+/clip/(?P<id>[A-Za-z0-9_-]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        vod_match = VOD_RE.search(url)
        if vod_match:
            return await self._extract_vod(vod_match.group("id"), url, ctx)

        clip_match = CLIP_RE.search(url)
        if clip_match:
            return await self._extract_clip(clip_match.group("id"), url, ctx)

        raise ExtractionError(f"URL de Twitch desconhecido: {url}")

    async def _extract_vod(self, vod_id: str, original_url: str, ctx: ExtractContext) -> MediaInfo:
        gql_url = "https://gql.twitch.tv/gql"
        headers = ctx.request_headers(referer="https://www.twitch.tv/")
        headers["Client-ID"] = CLIENT_ID

        query = [
            {
                "operationName": "PlaybackAccessToken",
                "variables": {
                    "isLive": False,
                    "login": "",
                    "isVod": True,
                    "vodID": vod_id,
                    "playerType": "embed",
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "0828119ded1c13477966434e15800ff57ddace7ba0ff14ac6ddfa5e54b3b649e",
                    }
                },
            },
            {
                "operationName": "VideoMetadata",
                "variables": {"channelLogin": "", "videoID": vod_id},
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "45111672eea2e507f8ba44d101a61862f9c56b11dee09a15634cb75cb9b9084d",
                    }
                },
            },
        ]

        try:
            res = await ctx.http.post_json(gql_url, query, headers=headers)
        except Exception as exc:
            raise ExtractionError(f"Falha ao consultar API da Twitch para o VOD {vod_id}: {exc}") from exc

        if not res or not isinstance(res, list) or len(res) < 1:
            raise ExtractionError(f"Resposta inválida da Twitch para o VOD {vod_id}")

        token_data = (res[0].get("data") or {}).get("videoPlaybackAccessToken") or {}
        token = token_data.get("value")
        sig = token_data.get("signature")

        if not token or not sig:
            raise ExtractionError(f"Não foi possível obter token de playback para o VOD {vod_id}")

        video_meta = {}
        if len(res) > 1:
            video_meta = (res[1].get("data") or {}).get("video") or {}

        usher_url = (
            f"https://usher.ttvnw.net/vod/{vod_id}.m3u8"
            f"?nauth={quote(token)}&nauthsig={sig}&allow_source=true&allow_audio_only=true"
        )

        formats = await self.formats_from_manifest(usher_url, ctx, headers=headers)
        if not formats:
            raise ExtractionError(f"Nenhum formato HLS encontrado para o VOD {vod_id}")

        title = video_meta.get("title") or f"Twitch VOD {vod_id}"
        owner = video_meta.get("owner") or {}

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.twitch.tv/videos/{vod_id}",
            original_url=original_url,
            title=title,
            description=video_meta.get("description"),
            thumbnail=video_meta.get("previewThumbnailURL"),
            duration=float(video_meta["lengthSeconds"]) if video_meta.get("lengthSeconds") else None,
            uploader=owner.get("displayName") or owner.get("login"),
            uploader_id=owner.get("login"),
            formats=formats,
        )

    async def _extract_clip(self, slug: str, original_url: str, ctx: ExtractContext) -> MediaInfo:
        gql_url = "https://gql.twitch.tv/gql"
        headers = ctx.request_headers(referer="https://clips.twitch.tv/")
        headers["Client-ID"] = CLIENT_ID

        query = [
            {
                "operationName": "VideoAccessToken_Clip",
                "variables": {"slug": slug},
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4cff39db3e0",
                    }
                },
            }
        ]

        try:
            res = await ctx.http.post_json(gql_url, query, headers=headers)
        except Exception:
            res = None

        formats: list[StreamFormat] = []
        title = f"Twitch Clip {slug}"
        broadcaster = None

        if res and isinstance(res, list) and len(res) > 0:
            clip_data = (res[0].get("data") or {}).get("clip") or {}
            title = clip_data.get("title") or title
            broadcaster = (clip_data.get("broadcaster") or {}).get("displayName")
            playback_token = clip_data.get("playbackAccessToken") or {}
            sig = playback_token.get("signature")
            token = playback_token.get("value")

            for entry in clip_data.get("videoQualities") or []:
                src_url = entry.get("sourceURL")
                if not src_url:
                    continue
                if sig and token:
                    src_url = f"{src_url}?sig={sig}&token={quote(token)}"
                quality = entry.get("quality")
                height = int(quality) if quality and str(quality).isdigit() else None
                formats.append(
                    StreamFormat(
                        id=f"clip-{quality}",
                        url=src_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                        height=height,
                        fps=float(entry["frameRate"]) if entry.get("frameRate") else None,
                    )
                )

        if not formats:
            # Fallback para extração HTML
            page = await ctx.http.get(f"https://clips.twitch.tv/{slug}", headers=headers)
            match = re.search(r'https?://[^\s"\'<>]*twitchcdn\.net[^\s"\'<>]*\.mp4', page.text)
            if match:
                formats.append(
                    StreamFormat(
                        id="clip-fallback",
                        url=match.group(0),
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                    )
                )

        if not formats:
            raise ExtractionError(f"Não foi possível obter streams para o clip da Twitch {slug}")

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://clips.twitch.tv/{slug}",
            original_url=original_url,
            title=title,
            uploader=broadcaster,
            formats=formats,
        )

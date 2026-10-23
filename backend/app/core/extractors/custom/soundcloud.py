"""Custom Soundcloud Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do SoundCloud.

Extrai faixas de áudio, metadados, capas em alta resolução e streams
progressivos e HLS (MP3 e Opus) usando a API pública v2 do SoundCloud.
"""

import logging
import re
from typing import ClassVar
from urllib.parse import quote

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

CLIENT_ID_RE = re.compile(r'client_id[:=]["\']?([a-zA-Z0-9]{32})["\']?')
DEFAULT_CLIENT_ID = "a3e059563d7fd3372b49b37f00a00bcf"


class SoundCloudExtractor(BaseExtractor):
    """Extractor para faixas de áudio do SoundCloud."""

    name: ClassVar[str] = "soundcloud"
    priority: ClassVar[int] = 40
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.|m\.)?soundcloud\.com/[^/?#&]+/(?!(?:tracks|albums|sets|reposts|likes)/?$)[^/?#&]+",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        headers = ctx.request_headers(referer="https://soundcloud.com/")

        # 1) Obtém a página para tentar extrair um client_id fresco
        client_id = DEFAULT_CLIENT_ID
        try:
            page = await ctx.http.get(url, headers=headers)
            cid_match = CLIENT_ID_RE.search(page.text)
            if cid_match:
                client_id = cid_match.group(1)
        except Exception:  # noqa: BLE001
            pass

        # 2) Consulta o endpoint /resolve da API v2
        api_url = f"https://api-v2.soundcloud.com/resolve?url={quote(url, safe='')}&client_id={client_id}"
        try:
            track = await ctx.http.get_json(api_url, headers=headers)
        except Exception as exc:
            raise ExtractionError(f"Falha ao consultar API do SoundCloud: {exc}") from exc

        if not track or not isinstance(track, dict):
            raise ExtractionError(f"Resposta inválida do SoundCloud para {url}")

        transcodings = ((track.get("media") or {}).get("transcodings")) or []
        formats: list[StreamFormat] = []

        for i, entry in enumerate(transcodings):
            stream_auth_url = entry.get("url")
            if not stream_auth_url:
                continue

            format_info = entry.get("format") or {}
            protocol_kind = format_info.get("protocol", "progressive")
            mime_type = format_info.get("mime_type", "")

            # Resolve a URL final assinada
            try:
                auth_res = await ctx.http.get_json(
                    f"{stream_auth_url}?client_id={client_id}", headers=headers
                )
                media_url = auth_res.get("url") if isinstance(auth_res, dict) else None
            except Exception:  # noqa: BLE001
                media_url = None

            if not media_url:
                continue

            is_hls = protocol_kind == "hls" or ".m3u8" in media_url
            container = Container.OPUS if "opus" in mime_type else Container.MP3

            if is_hls:
                try:
                    hls_fmts = await self.formats_from_manifest(media_url, ctx, headers=headers)
                    formats.extend(hls_fmts)
                except Exception:  # noqa: BLE001
                    formats.append(
                        StreamFormat(
                            id=f"sc-hls-{i}",
                            url=media_url,
                            media_type=MediaType.AUDIO,
                            container=container,
                            protocol=Protocol.HLS,
                            manifest_url=media_url,
                            manifest_kind="hls",
                        )
                    )
            else:
                formats.append(
                    StreamFormat(
                        id=f"sc-prog-{i}",
                        url=media_url,
                        media_type=MediaType.AUDIO,
                        container=container,
                        protocol=Protocol.HTTPS,
                    )
                )

        if not formats:
            raise ExtractionError(f"Nenhum stream de áudio disponível para {url}")

        title = track.get("title") or "SoundCloud Track"
        user = track.get("user") or {}
        duration_ms = track.get("duration")
        artwork = track.get("artwork_url")
        if artwork and "large" in artwork:
            artwork = artwork.replace("large", "t500x500")

        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=title,
            description=track.get("description"),
            thumbnail=artwork,
            duration=float(duration_ms) / 1000 if duration_ms else None,
            uploader=user.get("username"),
            uploader_id=str(user.get("id") or ""),
            formats=formats,
        )

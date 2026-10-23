"""Custom Twitter Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Twitter / X.

Utiliza a API oficial de Syndication do Twitter:
``https://cdn.syndication.twimg.com/tweet-result?id={id}&token={token}``
para obter os metadados e streams sem requerer autenticação ou chaves de API.
"""

import logging
import math
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

TWEET_ID_RE = re.compile(
    r"^https?://(?:(?:www|m(?:obile)?)\.)?(?:twitter|x)\.com/(?:#!/)?(?P<user>[^/]+)/status/(?P<id>\d+)"
)


def _js_number_to_string(number: float, radix: int = 10) -> str:
    """Converte um número em string com uma base arbitrária (2-36), imitando o JS."""
    if number < 0:
        return "-" + _js_number_to_string(-number, radix)
    if number == 0:
        return "0"
    int_part = int(number)
    frac_part = number - int_part
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    res = ""
    while int_part > 0:
        res = digits[int_part % radix] + res
        int_part //= radix
    if not res:
        res = "0"
    if frac_part > 0:
        res += "."
        for _ in range(16):
            frac_part *= radix
            digit = int(frac_part)
            res += digits[digit]
            frac_part -= digit
            if frac_part == 0:
                break
    return res


def _generate_syndication_token(twid: str) -> str:
    """Gera o token de autenticação syndication exigido pela CDN do Twitter."""
    val = (int(twid) / 1e15) * math.pi
    translation = str.maketrans(dict.fromkeys("0."))
    return _js_number_to_string(val, 36).translate(translation)


class TwitterExtractor(BaseExtractor):
    """Extractor para publicações com vídeo do Twitter / X."""

    name: ClassVar[str] = "twitter"
    priority: ClassVar[int] = 36
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:(?:www|m(?:obile)?)\.)?(?:twitter|x)\.com/(?:#!/)?(?P<user>[^/]+)/status/(?P<id>\d+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        match = TWEET_ID_RE.search(url)
        if not match:
            raise ExtractionError(f"URL de tweet inválido: {url}")

        twid = match.group("id")
        user = match.group("user")
        token = _generate_syndication_token(twid)

        api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={twid}&token={token}"
        headers = ctx.request_headers(referer="https://twitter.com/")
        headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        try:
            payload = await ctx.http.get_json(api_url, headers=headers)
        except Exception as exc:
            raise ExtractionError(f"Não foi possível obter dados do tweet {twid}: {exc}") from exc

        if not payload or not isinstance(payload, dict):
            raise ExtractionError(f"Resposta vazia do Twitter para o tweet {twid}")

        media_details = payload.get("mediaDetails") or []
        video_items = [
            m for m in media_details
            if m.get("type") in ("video", "animated_gif") and m.get("video_info")
        ]

        if not video_items:
            raise ExtractionError(
                "O tweet não contém nenhum vídeo ou media compatível",
                details={"tweet_id": twid},
            )

        video = video_items[0]
        video_info = video.get("video_info") or {}
        variants = video_info.get("variants") or []

        formats: list[StreamFormat] = []
        for i, variant in enumerate(variants):
            stream_url = variant.get("url")
            if not stream_url:
                continue

            content_type = variant.get("content_type", "")
            bitrate = variant.get("bitrate")

            if "mpegURL" in content_type or stream_url.endswith(".m3u8"):
                try:
                    hls_formats = await self.formats_from_manifest(
                        stream_url, ctx, headers=headers
                    )
                    formats.extend(hls_formats)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Falha ao resolver HLS Twitter: %s", exc)
                    formats.append(
                        StreamFormat(
                            id=f"hls-{i}",
                            url=stream_url,
                            media_type=MediaType.VIDEO,
                            container=Container.MP4,
                            protocol=Protocol.HLS,
                            manifest_url=stream_url,
                            manifest_kind="hls",
                        )
                    )
            else:
                formats.append(
                    StreamFormat(
                        id=f"http-{bitrate}" if bitrate else f"http-{i}",
                        url=stream_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                        tbr=(bitrate / 1000) if bitrate else None,
                    )
                )

        if not formats:
            raise ExtractionError(f"Nenhum formato de stream extraído para o tweet {twid}")

        title = payload.get("text") or f"Tweet de @{user}"
        user_info = payload.get("user") or {}
        duration_ms = video_info.get("duration_millis")
        thumbnail = video.get("media_url_https") or payload.get("media_url_https")

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://x.com/{user}/status/{twid}",
            original_url=url,
            title=title,
            description=payload.get("text"),
            thumbnail=thumbnail,
            duration=float(duration_ms) / 1000 if duration_ms else None,
            uploader=user_info.get("name") or user,
            uploader_id=user_info.get("screen_name") or user,
            formats=formats,
        )

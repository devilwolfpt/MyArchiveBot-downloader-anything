"""Custom Bilibili Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor nativo do Bilibili.

Suporta identificadores BV e av (bilibili.com/video/BV... e b23.tv/...).
Utiliza a API pública do Bilibili para obter metadados, vídeo e áudio.
"""

import logging
import re
from typing import ClassVar

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

BVID_RE = re.compile(r"bilibili\.com/video/(?P<id>(?:av\d+|BV[a-zA-Z0-9]+))")
B23_RE = re.compile(r"b23\.tv/(?P<id>[a-zA-Z0-9]+)")


class BilibiliExtractor(BaseExtractor):
    """Extractor nativo para vídeos do Bilibili."""

    name: ClassVar[str] = "bilibili"
    priority: ClassVar[int] = 43
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.)?bilibili\.com/video/(?P<id>(?:av\d+|BV[a-zA-Z0-9]+))",
        r"^https?://b23\.tv/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        headers = ctx.request_headers(referer="https://www.bilibili.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        bvid = None
        m_short = B23_RE.search(url)
        if m_short:
            try:
                res = await ctx.http.get(url, headers=headers)
                final_url = res.url_final or url
                m = BVID_RE.search(final_url)
                if m:
                    bvid = m.group("id")
            except Exception:  # noqa: BLE001
                pass

        if not bvid:
            m = BVID_RE.search(url)
            bvid = m.group("id") if m else None

        if not bvid:
            raise ExtractionError(f"Não foi possível extrair o identificador Bilibili de {url}")

        # 1) Obtém dados da view (cid, título, uploader)
        view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            view_res = await ctx.http.get_json(view_url, headers=headers)
        except Exception as exc:
            raise ExtractionError(f"Falha ao aceder à API do Bilibili: {exc}") from exc

        data = (view_res or {}).get("data") or {}
        cid = data.get("cid")
        if not cid:
            raise ExtractionError("Vídeo Bilibili não encontrado ou inacessível", details={"bvid": bvid})

        title = data.get("title") or f"Bilibili {bvid}"
        owner = (data.get("owner") or {}).get("name")
        pic = data.get("pic")
        duration = data.get("duration")

        # 2) Obtém stream URLs
        play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=16"
        formats: list[StreamFormat] = []

        try:
            play_res = await ctx.http.get_json(play_url, headers=headers)
            play_data = (play_res or {}).get("data") or {}

            # Vídeo em durl
            for i, item in enumerate(play_data.get("durl") or []):
                v_url = item.get("url")
                if not v_url:
                    continue
                formats.append(
                    StreamFormat(
                        id=f"bili-mp4-{i}",
                        url=v_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                        filesize=item.get("size"),
                        extractor_data={"http_headers": {"Referer": "https://www.bilibili.com/"}},
                    )
                )

            # Dash streams se presentes
            dash = play_data.get("dash") or {}
            for item in dash.get("video") or []:
                v_url = item.get("baseUrl") or item.get("base_url")
                if not v_url:
                    continue
                formats.append(
                    StreamFormat(
                        id=f"bili-dash-v-{item.get('id', 'video')}",
                        url=v_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                        width=item.get("width"),
                        height=item.get("height"),
                        extractor_data={"http_headers": {"Referer": "https://www.bilibili.com/"}},
                    )
                )

            for item in dash.get("audio") or []:
                a_url = item.get("baseUrl") or item.get("base_url")
                if not a_url:
                    continue
                formats.append(
                    StreamFormat(
                        id=f"bili-dash-a-{item.get('id', 'audio')}",
                        url=a_url,
                        media_type=MediaType.AUDIO,
                        container=Container.M4A,
                        protocol=Protocol.HTTPS,
                        extractor_data={"http_headers": {"Referer": "https://www.bilibili.com/"}},
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("Erro ao consultar playurl do Bilibili: %s", exc)

        if not formats:
            raise ExtractionError("Não foi possível obter streams do Bilibili", details={"bvid": bvid})

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.bilibili.com/video/{bvid}",
            original_url=url,
            title=title,
            description=data.get("desc"),
            thumbnail=pic,
            duration=float(duration) if duration else None,
            uploader=owner,
            formats=formats,
        )

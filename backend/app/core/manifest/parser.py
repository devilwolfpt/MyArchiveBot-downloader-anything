"""Dispatcher de manifests: normaliza HLS e DASH para uma estrutura comum."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from ..http.client import HttpClient
from ..models import Container, MediaType, Protocol, VariantInfo
from .dash import DashParser, container_for
from .hls import HlsParser, protocol_for

log = logging.getLogger(__name__)


class ManifestResolver:
    """Resolve URLs de manifesto para variantes normalizadas."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.hls = HlsParser()
        self.dash = DashParser()

    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_kind(url: str) -> str | None:
        """Detecta o tipo de manifesto a partir do URL/content-type."""
        lowered = url.lower().split("?")[0]
        if lowered.endswith(".m3u8") or ".m3u8/" in lowered:
            return "hls"
        if lowered.endswith(".mpd") or ".mpd/" in lowered:
            return "dash"
        return None

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[str, str]:
        """Devolve ``(conteúdo, content_type)``."""
        result = await self.http.get(url, headers=headers)
        content_type = result.headers.get("content-type", "")
        return result.text, content_type

    # ------------------------------------------------------------------ #
    async def resolve(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        depth: int = 0,
    ) -> list[VariantInfo]:
        """Devolve a lista de variantes de um manifesto.

        Para um master playlist HLS resolve cada variant para o seu
        media playlist (para obter contagem/duração de segmentos).
        """
        if depth > 3:
            log.warning("Profundidade de resolução de manifesto excedida em %s", url)
            return []

        content, content_type = await self.fetch(url, headers=headers)
        kind = self.detect_kind(url)
        if kind is None:
            if "mpegurl" in content_type.lower() or "#EXTM3U" in content[:200]:
                kind = "hls"
            elif "dash+xml" in content_type.lower() or "<MPD" in content[:500]:
                kind = "dash"

        if kind == "hls":
            return await self._resolve_hls(url, content, headers=headers, depth=depth)
        if kind == "dash":
            return await self._resolve_dash(url, content, headers=headers)
        log.warning("Tipo de manifesto desconhecido para %s (content-type=%s)", url, content_type)
        return []

    # ------------------------------------------------------------------ #
    async def _resolve_hls(
        self,
        url: str,
        content: str,
        *,
        headers: dict[str, str] | None,
        depth: int,
    ) -> list[VariantInfo]:
        if self.hls.is_master(content):
            master = self.hls.parse_master(content, url)
            variants: list[VariantInfo] = []
            for variant in master.variants:
                if variant.is_iframe:
                    continue
                # Resolución da variant -> media playlist (para segmentos).
                media_url = variant.uri
                duration = None
                try:
                    sub_content, _ = await self.fetch(media_url, headers=headers)
                    media = self.hls.parse_media(sub_content, media_url)
                    duration = media.duration or None
                except Exception as exc:  # noqa: BLE001 - degradacao graciosa
                    log.debug("Não foi possível ler media playlist %s: %s", media_url, exc)

                variants.append(
                    VariantInfo(
                        id=variant.id,
                        url=media_url,
                        media_type=variant.media_type,
                        container=variant.container,
                        width=variant.width,
                        height=variant.height,
                        bandwidth=variant.average_bandwidth or variant.bandwidth,
                        codecs=variant.codecs,
                        frame_rate=variant.frame_rate,
                        audio_group=variant.audio_group,
                        manifest_url=url,
                        manifest_kind="hls",
                        media_playlist_url=media_url,
                    )
                )
                _ = duration
            return variants

        # Media playlist directo -> variante única
        media = self.hls.parse_media(content, url)
        return [
            VariantInfo(
                id="hls-0",
                url=url,
                media_type=MediaType.VIDEO,
                container=Container.TS,
                bandwidth=None,
                manifest_url=url,
                manifest_kind="hls",
                media_playlist_url=url,
            )
        ]

    async def _resolve_dash(
        self, url: str, content: str, *, headers: dict[str, str] | None
    ) -> list[VariantInfo]:
        manifest = self.dash.parse(content, url)
        variants: list[VariantInfo] = []
        for rep in manifest.representations:
            variants.append(
                VariantInfo(
                    id=rep.id,
                    url=rep.url,
                    media_type=rep.media_type,
                    container=container_for(rep.mime_type),
                    width=rep.width,
                    height=rep.height,
                    bandwidth=rep.bandwidth,
                    codecs=rep.codecs,
                    frame_rate=rep.frame_rate,
                    manifest_url=url,
                    manifest_kind="dash",
                    media_playlist_url=rep.url,
                )
            )
        return variants


def protocol_of(url: str) -> Protocol:
    return protocol_for(url)


def is_media_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(
        (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".ts", ".mp3", ".m4a", ".aac", ".opus", ".ogg")
    )

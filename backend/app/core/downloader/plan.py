"""Planos de download — a representação intermédia entre formatos e o disco.

Um :class:`DownloadPlan` descreve *exactamente* o que tem de ser transferido:
um recurso progressivo (um único URL) ou uma lista de segmentos (HLS/DASH).
É o contrato entre a camada de extracção e o chunk downloader.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin

from ..exceptions import DownloadError, HttpForbiddenError, MediaUrlExpiredError, NoFormatFoundError
from ..http.client import HttpClient
from ..manifest.dash import DashParser
from ..manifest.hls import HlsParser
from ..models import StreamFormat

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SegmentPlan:
    """Um segmento a descarregar."""

    index: int
    url: str
    duration: float = 0.0
    byte_range: tuple[int, int] | None = None
    #: Material AES (hex) + IV (hex) — apenas para HLS cifrado.
    aes_key_hex: str | None = None
    aes_iv_hex: str | None = None
    #: Segmento de inicialização (pré-pendente a este).
    init_url: str | None = None
    init_byte_range: tuple[int, int] | None = None
    init_aes_key_hex: str | None = None
    init_aes_iv_hex: str | None = None

    @property
    def is_encrypted(self) -> bool:
        return self.aes_key_hex is not None


@dataclass(slots=True)
class DownloadPlan:
    """Plano completo de transferência."""

    kind: Literal["progressive", "segmented"]
    url: str
    filename: str
    headers: dict[str, str] = field(default_factory=dict)
    container: str = "mp4"
    total_bytes: int | None = None
    segments: list[SegmentPlan] = field(default_factory=list)
    format_id: str | None = None
    width: int | None = None
    height: int | None = None
    title: str | None = None

    @property
    def is_segmented(self) -> bool:
        return self.kind == "segmented"


class PlanBuilder:
    """Constrói :class:`DownloadPlan` a partir de um :class:`StreamFormat`."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.hls = HlsParser()
        self.dash = DashParser()

    async def build(
        self,
        fmt: StreamFormat,
        *,
        filename: str,
        headers: dict[str, str] | None = None,
    ) -> DownloadPlan:
        headers = dict(headers or {})
        fmt_headers = fmt.extractor_data.get("http_headers")
        if isinstance(fmt_headers, dict):
            for k, v in fmt_headers.items():
                if v and k not in headers:
                    headers[k] = v

        if fmt.manifest_kind == "hls":
            return await self._build_hls(fmt, filename=filename, headers=headers)
        if fmt.manifest_kind == "dash":
            return await self._build_dash(fmt, filename=filename, headers=headers)

        # Progressivo
        total = fmt.filesize or fmt.filesize_approx
        if total is None:
            try:
                total = await self.http.content_length(fmt.url, headers=headers)
            except Exception as exc:  # noqa: BLE001
                log.debug("content-length indisponível para %s: %s", fmt.url, exc)
        return DownloadPlan(
            kind="progressive",
            url=fmt.url,
            filename=filename,
            headers=headers,
            container=fmt.container.value if fmt.container.value != "unknown" else "mp4",
            total_bytes=total,
            format_id=fmt.id,
            width=fmt.width,
            height=fmt.height,
        )

    # ------------------------------------------------------------------ #
    async def _build_hls(
        self, fmt: StreamFormat, *, filename: str, headers: dict[str, str]
    ) -> DownloadPlan:
        media_url = fmt.manifest_url or fmt.url
        result = await self.http.get(media_url, headers=headers)
        playlist = self.hls.parse_media(result.text, media_url)

        if len(playlist.segments) > 100_000:
            raise DownloadError("Playlist HLS com demasiados segmentos")

        segments: list[SegmentPlan] = []
        sequence = playlist.media_sequence
        for index, seg in enumerate(playlist.segments):
            key_hex = None
            iv_hex = None
            if seg.key is not None and seg.key.method == "AES-128":
                if not seg.key.uri:
                    raise DownloadError("Segmento cifrado sem URI de chave")
                key_hex = await self._fetch_key_hex(seg.key.uri, headers)
                iv_hex = seg.key.iv
            elif seg.key is not None and seg.key.method not in ("NONE", ""):
                raise DownloadError(
                    f"Método de cifra HLS não suportado: {seg.key.method}",
                    details={"segment": index},
                )

            segments.append(
                SegmentPlan(
                    index=index,
                    url=seg.uri,
                    duration=seg.duration,
                    byte_range=seg.byte_range,
                    aes_key_hex=key_hex,
                    aes_iv_hex=iv_hex,
                    init_url=seg.map_uri,
                    init_byte_range=seg.map_byte_range,
                )
            )
            sequence += 1

        return DownloadPlan(
            kind="segmented",
            url=media_url,
            filename=filename,
            headers=headers,
            container="ts",
            segments=segments,
            format_id=fmt.id,
            width=fmt.width,
            height=fmt.height,
        )

    async def _build_dash(
        self, fmt: StreamFormat, *, filename: str, headers: dict[str, str]
    ) -> DownloadPlan:
        manifest_url = fmt.manifest_url or fmt.url
        result = await self.http.get(manifest_url, headers=headers)
        manifest = self.dash.parse(result.text, manifest_url)

        target = None
        for rep in manifest.representations:
            if rep.id == fmt.id:
                target = rep
                break
        if target is None:
            if not manifest.representations:
                raise NoFormatFoundError("MPD sem representações")
            target = manifest.representations[0]

        segments: list[SegmentPlan] = []
        if target.initialization is not None:
            segments.append(
                SegmentPlan(
                    index=-1,
                    url=target.initialization.url,
                    byte_range=target.initialization.byte_range,
                )
            )
        for seg in target.segments:
            segments.append(
                SegmentPlan(
                    index=seg.index,
                    url=seg.url,
                    duration=seg.duration,
                    byte_range=seg.byte_range,
                )
            )

        return DownloadPlan(
            kind="segmented",
            url=manifest_url,
            filename=filename,
            headers=headers,
            container="mp4",
            segments=segments,
            format_id=fmt.id,
            width=target.width,
            height=target.height,
        )

    async def _fetch_key_hex(self, key_url: str, headers: dict[str, str]) -> str:
        result = await self.http.get(key_url, headers=headers)
        return result.content[:16].hex()


_SAFE_NAME_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)


def sanitize_filename(name: str, *, max_length: int = 120, default: str = "media") -> str:
    """Normaliza um nome de ficheiro para uso seguro em disco.

    Remove separadores de caminho, barras invertidas e sequências ``..``
    (evita path traversal), preservando a extensão do ficheiro.
    """
    cleaned = _SAFE_NAME_RE.sub("_", name).strip(" ._")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)  # neutraliza ".." e variantes
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        cleaned = default
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" ._")
    return cleaned or default


def extension_for(fmt: StreamFormat, fallback: str = "mp4") -> str:
    container = fmt.container.value
    if container in ("unknown", "ts"):
        return fallback
    return container

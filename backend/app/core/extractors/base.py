"""Contrato base dos extractores.

Um extractor é responsável por transformar um URL num :class:`MediaInfo`
(lista de :class:`StreamFormat`). A resolução é feita por *routing* baseado
em regex — adicionar uma nova plataforma é uma classe nova + um ``register()``.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...config import Settings
from ..http.client import HttpClient
from ..manifest.parser import ManifestResolver
from ..models import Container, MediaType, Protocol, StreamFormat

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExtractContext:
    """Contexto partilhado por todos os extractores durante uma extracção."""

    http: HttpClient
    settings: Settings
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_file: str | None = None
    cookie_jar: Any = None
    referer: str | None = None
    max_page_bytes: int = 5 * 1024 * 1024

    @property
    def manifest_resolver(self) -> ManifestResolver:
        return ManifestResolver(self.http)

    def request_headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = dict(self.headers)
        referer_url = referer or self.referer
        if referer_url:
            headers.setdefault("Referer", referer_url)
            origin = _origin_from(referer_url)
            if origin:
                headers.setdefault("Origin", origin)
        else:
            headers.pop("Referer", None)
            headers.pop("Origin", None)
        return headers


def _origin_from(url: str) -> str | None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class BaseExtractor(ABC):
    """Base de todos os extractores."""

    #: Identificador único (usado na API e nos logs).
    name: ClassVar[str] = "base"
    #: Menor valor = maior prioridade no routing.
    priority: ClassVar[int] = 100
    #: Regexes que identificam URLs desta plataforma.
    patterns: ClassVar[list[str]] = []
    #: Esquemas suportados.
    url_schemes: ClassVar[set[str]] = {"http", "https"}
    #: Se True, o extractor é consultado mesmo sem corresponder a um regex.
    catch_all: ClassVar[bool] = False
    #: Se False, os metadados não são guardados em cache.
    #: Usado por plataformas cujas URLs de media estão vinculadas ao IP de
    #: origem (YouTube): uma URL em cache fica inválida assim que o IP de
    #: saída muda e provoca HTTP 403 no download.
    cacheable: ClassVar[bool] = True

    @classmethod
    def compiled_patterns(cls) -> list[re.Pattern[str]]:
        cached = cls.__dict__.get("_compiled_patterns")
        if cached is None:
            cached = [re.compile(p, re.IGNORECASE) for p in cls.patterns]
            cls._compiled_patterns = cached  # type: ignore[attr-defined]
        return cached

    # ------------------------------------------------------------------ #
    def matches(self, url: str) -> bool:
        """Verifica se este extractor suporta o URL."""
        if self.catch_all:
            return True
        return any(pattern.search(url) for pattern in self.compiled_patterns())

    @abstractmethod
    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        """Produz os metadados e formatos do URL."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Helpers partilhados
    # ------------------------------------------------------------------ #
    async def formats_from_manifest(
        self,
        url: str,
        ctx: ExtractContext,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[StreamFormat]:
        """Converte um manifesto HLS/DASH em formatos."""
        resolver = ctx.manifest_resolver
        variants = await resolver.resolve(url, headers=headers or ctx.request_headers())
        formats: list[StreamFormat] = []
        for variant in variants:
            formats.append(
                StreamFormat(
                    id=variant.id,
                    url=variant.url,
                    media_type=variant.media_type,
                    container=variant.container,
                    protocol=Protocol.HLS if variant.manifest_kind == "hls" else Protocol.DASH,
                    width=variant.width,
                    height=variant.height,
                    fps=variant.frame_rate,
                    tbr=(variant.bandwidth / 1000) if variant.bandwidth else None,
                    vcodec=(variant.codecs or "").split(",")[0] or None,
                    manifest_url=variant.manifest_url,
                    manifest_kind=variant.manifest_kind,
                    extractor_data={"bandwidth": variant.bandwidth},
                )
            )
        return formats

    async def formats_from_direct_url(
        self,
        url: str,
        ctx: ExtractContext,
        *,
        media_type: MediaType = MediaType.UNKNOWN,
        container: Container = Container.UNKNOWN,
        headers: dict[str, str] | None = None,
    ) -> list[StreamFormat]:
        """Cria um formato para um URL de media directo, com tamanho estimado."""
        size: int | None = None
        try:
            size = await ctx.http.content_length(url, headers=headers or ctx.request_headers())
        except Exception as exc:  # noqa: BLE001 - degradacao graciosa
            log.debug("Não foi possível obter content-length de %s: %s", url, exc)
        return [
            StreamFormat(
                id="direct",
                url=url,
                media_type=media_type,
                container=container,
                protocol=Protocol.HTTPS if url.startswith("https") else Protocol.HTTP,
                filesize=size,
                filesize_approx=size,
            )
        ]

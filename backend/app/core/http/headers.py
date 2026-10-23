"""Construção e rotação de headers HTTP."""
from __future__ import annotations

import random

from ...config import DEFAULT_USER_AGENTS, Settings


def _supported_encodings() -> str:
    """Devolve apenas as compressões que o httpx consegue descodificar.

    Se o servidor responder com ``Content-Encoding: br`` e o ``brotli`` não
    estiver instalado, o httpx devolve os bytes *comprimidos* em vez do
    conteúdo — o que produz erros crípticos do tipo "resposta JSON
    inválida". Por isso só anunciamos o que temos decoder para.
    """
    encodings = ["gzip", "deflate"]
    try:  # pragma: no cover - depende do ambiente
        import brotli  # noqa: F401

        encodings.append("br")
    except ImportError:  # pragma: no cover
        pass
    try:  # pragma: no cover - depende do ambiente
        import zstandard  # noqa: F401

        encodings.append("zstd")
    except ImportError:  # pragma: no cover
        pass
    return ", ".join(encodings)


#: Cabeçalho ``Accept-Encoding`` calculado em tempo de importacao.
ACCEPT_ENCODING = _supported_encodings()

# Headers de um browser "realista". A ordem importa em alguns CDNs.
BASE_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": ACCEPT_ENCODING,
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

BINARY_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Accept-Encoding": ACCEPT_ENCODING,
}


class HeaderFactory:
    """Gera headers por request, com rotação de User-Agent e Referer."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._agents = list(settings.user_agents or DEFAULT_USER_AGENTS)
        self._rng = random.Random()

    def pick_user_agent(self) -> str:
        if not self._agents:
            return DEFAULT_USER_AGENTS[0]
        if not self._settings.rotate_user_agents:
            return self._agents[0]
        return self._rng.choice(self._agents)

    def build(
        self,
        *,
        referer: str | None = None,
        origin: str | None = None,
        binary: bool = False,
        extra: dict[str, str] | None = None,
        user_agent: str | None = None,
    ) -> dict[str, str]:
        headers = dict(BINARY_HEADERS if binary else BASE_HEADERS)
        headers["User-Agent"] = user_agent or self.pick_user_agent()
        headers.update(self._settings.default_headers)
        effective_referer = referer or (extra or {}).get("Referer")
        if effective_referer:
            headers["Referer"] = effective_referer
            headers["Origin"] = origin or (extra or {}).get("Origin") or _origin_from(effective_referer)
        if extra:
            headers.update(extra)
            if "Referer" in extra and "Origin" not in headers:
                headers["Origin"] = _origin_from(extra["Referer"])
        return headers


def _origin_from(url: str) -> str | None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def default_headers_for(settings: Settings, *, referer: str | None = None) -> dict[str, str]:
    """Atalho funcional para usar handlers do FastAPI."""
    return HeaderFactory(settings).build(referer=referer)

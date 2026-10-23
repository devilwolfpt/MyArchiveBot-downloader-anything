"""Dependências FastAPI (injecção de serviços, auth e rate limiting)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import Settings, get_settings
from ..core.downloader.manager import JobManager
from ..services.extraction import ExtractionService
from ..services.pipeline import DownloadService


def settings_dependency() -> Settings:
    return get_settings()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """Valida a API key, se configurada."""
    settings = get_settings()
    if not settings.api_key:
        return

    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = request.query_params.get("api_key")

    if token != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "API key inválida ou em falta"},
        )


# --------------------------------------------------------------------------- #
# Rate limiting (janela deslizante, in-memory)
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Limitador simples por IP — suficiente para uma instância única."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int, int]:
        """Devolve ``(permitido, restantes, reset_em_segundos)``."""
        now = time.time()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            reset = int(self.window - (now - hits[0])) + 1
            return False, 0, reset
        hits.append(now)
        remaining = self.limit - len(hits)
        return True, remaining, int(self.window)


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        settings = get_settings()
        _limiter = RateLimiter(settings.rate_limit_per_minute)
    return _limiter


async def rate_limit(request: Request) -> None:
    """Aplica rate limiting por IP."""
    limiter = get_rate_limiter()
    client_ip = request.client.host if request.client else "unknown"
    allowed, remaining, reset = limiter.check(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": "Demasiados pedidos. Tente novamente mais tarde.",
                "retry_after": reset,
            },
            headers={"Retry-After": str(reset)},
        )


# --------------------------------------------------------------------------- #
# Serviços
# --------------------------------------------------------------------------- #
def get_job_manager() -> JobManager:
    from ..main import app_state

    return app_state.job_manager


def get_extraction_service() -> ExtractionService:
    from ..main import app_state

    return app_state.extraction


def get_download_service() -> DownloadService:
    from ..main import app_state

    return app_state.downloads

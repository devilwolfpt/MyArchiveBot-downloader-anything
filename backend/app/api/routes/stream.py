"""Proxy de streaming — permite tocar media remota sem problemas de CORS.

O frontend (motor de busca / extensão) pode apontar um ``<video>`` ou um
``hls.js`` directamente para ``/api/stream?url=...``. O proxy suporta
``Range`` requests, o que é essencial para seeking.
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ...config import get_settings
from ...core.http.client import HttpClient
from ..deps import rate_limit, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

CHUNK = 1024 * 256

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}


@router.get("/stream")
async def stream(
    request: Request,
    url: str = Query(..., description="URL do media a reproduzir"),
    _: None = Depends(require_api_key),
    __: None = Depends(rate_limit),
):
    """Faz proxy de um recurso de media com suporte a Range requests."""
    settings = get_settings()
    target = unquote(url)

    async with HttpClient(settings) as http:
        try:
            response, iterator = await http.stream(
                "GET",
                target,
                headers=dict(request.headers.get("x-upstream-headers") and {} or {}),
                range_start=_range_start(request.headers.get("range")),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_BAD_GATEWAY,
                detail={"code": "http_request_failed", "message": str(exc)},
            ) from exc

        content_type = response.headers.get("content-type")
        if not content_type:
            for ext, ctype in CONTENT_TYPES.items():
                if target.lower().split("?")[0].endswith(ext):
                    content_type = ctype
                    break
        content_type = content_type or "application/octet-stream"

        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
        for key in ("content-length", "content-range", "etag", "last-modified"):
            if key in response.headers:
                headers[key] = response.headers[key]

        async def body() -> object:
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(body(), status_code=response.status_code, media_type=content_type, headers=headers)


def _range_start(value: str | None) -> int | None:
    if not value or not value.startswith("bytes="):
        return None
    raw = value[6:].split("-")[0]
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


@router.get("/probe", summary="Cabeçalhos de um recurso remoto (CORS-friendly)")
async def probe(
    url: str = Query(...),
    _: None = Depends(require_api_key),
) -> dict:
    """Devolve os cabeçalhos HTTP de um recurso (sem CORS no browser)."""
    settings = get_settings()
    async with HttpClient(settings) as http:
        try:
            result = await http.head(url)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_BAD_GATEWAY,
                detail={"code": "http_request_failed", "message": str(exc)},
            ) from exc
    return {
        "status": result.status_code,
        "headers": result.headers,
        "final_url": result.url_final,
    }


def _unused_httpx() -> None:  # pragma: no cover
    httpx.__version__

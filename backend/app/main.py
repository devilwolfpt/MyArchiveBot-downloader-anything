"""Ponto de entrada da aplicação FastAPI.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import cookies, download, extract, health, stream, updater
from .config import get_settings
from .core.downloader.manager import JobManager
from .core.exceptions import DownloaderError
from .services.extraction import ExtractionService
from .services.pipeline import DownloadService

logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO, including signed YouTube media tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")


@dataclass
class AppState:
    """Estado partilhado da aplicação (iniciado no lifespan)."""

    settings: object
    job_manager: JobManager
    extraction: ExtractionService
    downloads: DownloadService


app_state: AppState | None = None  # type: ignore[assignment]


async def _cleanup_loop(interval: int = 300) -> None:
    """Remove jobs expirados e ficheiros temporários antigos."""
    while True:
        try:
            await asyncio.sleep(interval)
            state = app_state
            if state is None:
                continue
            removed = state.job_manager.purge_expired()
            state.downloads.storage.purge_expired(state.settings.job_ttl_seconds)
            if removed:
                log.info("Purga: %d job(s) expirado(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Erro na rotina de limpeza: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_state

    settings = get_settings()
    settings.ensure_dirs()

    job_manager = JobManager(
        ttl_seconds=settings.job_ttl_seconds,
        max_jobs=settings.job_history_limit,
        max_concurrent=settings.max_concurrent_jobs,
    )
    extraction = ExtractionService(settings)
    downloads = DownloadService(settings, job_manager, extraction)

    app_state = AppState(
        settings=settings,
        job_manager=job_manager,
        extraction=extraction,
        downloads=downloads,
    )

    cleanup_task = asyncio.create_task(_cleanup_loop(), name="cleanup")
    log.info("%s iniciado (debug=%s)", settings.app_name, settings.debug)

    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await job_manager.shutdown()
        log.info("Aplicação encerrada")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Motor de download de media universal MyArchive Downloader, "
            "com API REST + WebSocket."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        root_path=settings.root_path,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "Content-Disposition"],
    )

    prefix = settings.api_prefix
    app.include_router(extract.router, prefix=prefix)
    app.include_router(download.router, prefix=prefix)
    app.include_router(stream.router, prefix=prefix)
    app.include_router(health.router, prefix=prefix)
    app.include_router(cookies.router, prefix=prefix)
    app.include_router(updater.router, prefix=prefix)

    # ---- Frontend estático (demo web) ---------------------------------
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if not frontend_dir.exists():
        frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
    if frontend_dir.exists():
        app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    # ---- Exception handlers -------------------------------------------
    @app.exception_handler(DownloaderError)
    async def downloader_error_handler(request: Request, exc: DownloaderError) -> JSONResponse:
        log.warning("Erro de negócio em %s: %s", request.url.path, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> Any:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and frontend_dir.exists():
            return RedirectResponse(url="/app", status_code=307)
        return {
            "name": settings.app_name,
            "version": "1.0.0",
            "docs": "/docs",
            "frontend": "/app",
            "endpoints": {
                "extract": f"{prefix}/extract-info",
                "download": f"{prefix}/download",
                "jobs": f"{prefix}/jobs/{{job_id}}",
                "websocket": f"{prefix}/ws/jobs/{{job_id}}",
                "stream": f"{prefix}/stream?url=...",
                "health": f"{prefix}/health",
                "cookies": f"{prefix}/cookies",
                "engine": f"{prefix}/engine/status",
            },
        }

    return app


app = create_app()

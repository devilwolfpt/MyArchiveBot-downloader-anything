"""Rotas de download: criação de jobs, estado, streaming e entrega do ficheiro."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, StreamingResponse

from ...config import get_settings
from ...core.downloader.manager import Job, JobManager
from ...core.models import DownloadRequest, JobResult
from ...services.pipeline import DownloadService
from ..deps import get_download_service, get_job_manager, rate_limit, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["download"])

CHUNK = 1024 * 512


# --------------------------------------------------------------------------- #
# Criação e consulta de jobs
# --------------------------------------------------------------------------- #
@router.post("/download", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
async def create_download(
    payload: DownloadRequest,
    service: DownloadService = Depends(get_download_service),
    _: None = Depends(require_api_key),
    __: None = Depends(rate_limit),
) -> dict:
    """Cria um job de download e devolve imediatamente o ``job_id``.

    O progresso é consumido via WebSocket (``/ws/jobs/{job_id}``) ou por
    polling em ``GET /jobs/{job_id}``.
    """
    job = await service.jobs.create(payload.url)
    job.task = asyncio.create_task(
        service.jobs.run(job, lambda j: service.execute(j, payload)),
        name=f"download-{job.id}",
    )
    return {
        "job_id": job.id,
        "status": job.status.value,
        "websocket": f"/api/ws/jobs/{job.id}",
        "poll": f"/api/jobs/{job.id}",
    }


@router.get("/jobs/{job_id}", response_model=dict)
async def get_job(
    job_id: str,
    jobs: JobManager = Depends(get_job_manager),
    _: None = Depends(require_api_key),
) -> dict:
    """Estado completo do job (inclui último evento de progresso)."""
    job = jobs.find(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Job não encontrado"})
    return job.snapshot()


@router.delete("/jobs/{job_id}", response_model=dict)
async def cancel_job(
    job_id: str,
    jobs: JobManager = Depends(get_job_manager),
    _: None = Depends(require_api_key),
) -> dict:
    """Cancela um job em execução."""
    try:
        job = await jobs.cancel(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": str(exc)}) from exc
    return {"job_id": job.id, "status": job.status.value}


@router.get("/jobs", response_model=list)
async def list_jobs(
    jobs: JobManager = Depends(get_job_manager),
    _: None = Depends(require_api_key),
) -> list[dict]:
    """Lista os jobs recentes (uso administrativo)."""
    return [job.snapshot() for job in jobs.list()[:100]]


# --------------------------------------------------------------------------- #
# WebSocket de progresso
# --------------------------------------------------------------------------- #
@router.websocket("/ws/jobs/{job_id}")
async def job_progress_ws(websocket: WebSocket, job_id: str) -> None:
    """Stream de eventos de progresso (JSON) para um job.

    Eventos típicos::

        {"job_id": "...", "status": "running", "phase": "downloading",
         "percent": 42.7, "downloaded_bytes": 12345, "total_bytes": 28900,
         "speed": 1048576.0, "eta": 16.2, "message": "..."}
    """
    settings = get_settings()
    token = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    if settings.api_key and token != settings.api_key:
        await websocket.close(code=4401, reason="API key inválida")
        return

    await websocket.accept()

    from ...main import app_state

    jobs: JobManager = app_state.job_manager
    if jobs.find(job_id) is None:
        await websocket.send_json({"error": {"code": "job_not_found", "message": "Job não encontrado"}})
        await websocket.close(code=4404)
        return

    try:
        async for event in jobs.subscribe(job_id):
            await websocket.send_json(event.model_dump())
    except WebSocketDisconnect:
        log.debug("WebSocket de progresso desligado (%s)", job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Erro no WebSocket de progresso %s: %s", job_id, exc)
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- #
# Entrega do ficheiro
# --------------------------------------------------------------------------- #
@router.get("/download/{job_id}")
async def download_file(
    job_id: str,
    request: Request,
    jobs: JobManager = Depends(get_job_manager),
    _: None = Depends(require_api_key),
):
    """Entrega o ficheiro final, com suporte a ``Range`` requests."""
    job = jobs.find(job_id)
    if job is None or job.result is None or not job.result.path:
        raise HTTPException(
            status_code=404,
            detail={"code": "job_not_found", "message": "Job sem resultado disponível"},
        )

    path = Path(job.result.path)
    if not path.exists():
        raise HTTPException(
            status_code=410,
            detail={"code": "file_gone", "message": "Ficheiro já removido do servidor"},
        )

    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    media_type = job.result.mime_type or "application/octet-stream"

    if range_header and range_header.startswith("bytes="):
        start, _, end = range_header[6:].partition("-")
        start_i = int(start) if start else 0
        end_i = int(end) if end else file_size - 1
        end_i = min(end_i, file_size - 1)
        if start_i > end_i or start_i >= file_size:
            raise HTTPException(status_code=416, detail="Intervalo inválido")

        async def iter_range() -> object:
            with path.open("rb") as handle:
                handle.seek(start_i)
                remaining = end_i - start_i + 1
                while remaining > 0:
                    buffer = handle.read(min(CHUNK, remaining))
                    if not buffer:
                        break
                    remaining -= len(buffer)
                    yield buffer

        return StreamingResponse(
            iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start_i}-{end_i}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end_i - start_i + 1),
                "Content-Disposition": f'attachment; filename="{path.name}"',
            },
        )

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
    )


@router.head("/download/{job_id}")
async def head_download(
    job_id: str,
    jobs: JobManager = Depends(get_job_manager),
    _: None = Depends(require_api_key),
) -> dict:
    """Metadados do ficheiro (tamanho, MIME) sem o transferring."""
    job = jobs.find(job_id)
    if job is None or job.result is None or not job.result.path:
        raise HTTPException(status_code=404, detail={"code": "job_not_found"})
    path = Path(job.result.path)
    return {
        "filename": path.name,
        "size": path.stat().st_size if path.exists() else None,
        "mime_type": job.result.mime_type,
    }

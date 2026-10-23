"""Endpoints de saúde e diagnóstico."""
from __future__ import annotations

import platform
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from ...config import get_settings
from ...core.extractors import registry
from ...services.extraction import ExtractionService
from ..deps import get_extraction_service, get_job_manager

router = APIRouter(tags=["system"])

STARTED_AT = time.time()


@router.get("/health", summary="Estado do serviço")
async def health(
    extraction: ExtractionService = Depends(get_extraction_service),
) -> dict:
    """Verificação de saúde: usada por balanceadores e pelo frontend."""
    settings = get_settings()
    ffmpeg = shutil.which(settings.ffmpeg_path)
    return {
        "status": "ok",
        "app": settings.app_name,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "version": "1.0.0",
        "python": platform.python_version(),
        "platform": platform.system(),
        "ffmpeg": ffmpeg or "not_installed",
        "extractors": [e.name for e in registry.all()],
        "supported_sites_count": 1754,
        "cache_entries": extraction.stats()["cached_entries"],
    }


@router.get("/storage", summary="Estado do armazenamento")
async def storage_info() -> dict:
    """Uso de disco e directórios de trabalho."""
    settings = get_settings()
    downloads = Path(settings.downloads_dir)
    try:
        usage = shutil.disk_usage(downloads)
        disk = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
        }
    except OSError:
        disk = {}
    return {
        "downloads_dir": str(downloads),
        "temp_dir": str(settings.temp_dir),
        "files": len(list(downloads.glob("*"))) if downloads.exists() else 0,
        "disk": disk,
    }

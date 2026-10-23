"""Endpoints de atualização automática e sincronização do motor MyArchive Downloader."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks

from ...core.updater import sync_bot

router = APIRouter(prefix="/engine", tags=["engine-updater"])


@router.get("/status", summary="Estado e versão do motor")
async def get_engine_status() -> dict[str, Any]:
    """Retorna a versão atual instalada, a versão mais recente upstream e o estado do bot."""
    return sync_bot._to_dict()


@router.post("/check", summary="Verificar novas versões")
async def check_updates() -> dict[str, Any]:
    """Consulta a comunidade para verificar se existem novas melhorias e correções para sites de streaming."""
    return await sync_bot.check_for_updates()


@router.post("/update", summary="Atualizar motor de streaming")
async def update_engine(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Inicia a sincronização, adaptação automática e validação do motor sem parar a aplicação."""
    if sync_bot.status.is_updating:
        return {"success": False, "message": "A atualização já está em curso.", "status": sync_bot._to_dict()}

    # Dispara a atualização em background
    background_tasks.add_task(sync_bot.sync_now)

    return {
        "success": True,
        "message": "Atualização iniciada em segundo plano. Podes acompanhar o progresso em tempo real.",
        "status": sync_bot._to_dict(),
    }


@router.post("/rollback", summary="Reverter para versão anterior")
async def rollback_engine() -> dict[str, Any]:
    """Restaura a cópia de segurança imediatamente anterior se necessário."""
    return sync_bot.rollback()

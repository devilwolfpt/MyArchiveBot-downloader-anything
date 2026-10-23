"""Rotas para gestão de cookies (cookies.txt / Netscape / JSON).

Permite carregar ficheiros de cookies exportados de extensões de browser
como "Get cookies.txt LOCALLY", "Cookie-Editor", etc., permitindo ao utilizador
contornar verificações Cloudflare, logins e restrições de idade em qualquer site.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ...config import get_settings
from ...core.http.cookies import (
    get_cookie_file_stats,
    parse_cookies_text,
    save_cookie_jar,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/cookies", tags=["cookies"])


class CookiePastePayload(BaseModel):
    content: str


@router.get("", summary="Estado do ficheiro de cookies atual")
async def get_cookies_status() -> dict[str, Any]:
    """Devolve informações sobre os cookies atualmente carregados na app."""
    settings = get_settings()
    cookie_path = settings.data_dir / "cookies.txt"
    return get_cookie_file_stats(cookie_path)


@router.post("/upload", summary="Carregar ficheiro ou texto cookies.txt")
async def upload_cookies(
    request: Request,
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Recebe um ficheiro cookies.txt (UploadFile) ou texto de cookies (JSON ou multipart)."""
    settings = get_settings()
    settings.ensure_dirs()
    target_path = settings.data_dir / "cookies.txt"

    content = ""
    if file is not None:
        raw_bytes = await file.read()
        content = raw_bytes.decode("utf-8", errors="replace")
    else:
        # Tenta ler como JSON
        try:
            body = await request.json()
            if isinstance(body, dict):
                content = body.get("content", "")
        except Exception:
            pass

    content = content.strip()
    if not content:
        raise HTTPException(
            status_code=400,
            detail="Nenhum conteúdo de cookies fornecido. Envia um ficheiro ou o texto do cookies.txt.",
        )

    try:
        jar, domains = parse_cookies_text(content)
        cookie_count = len(list(jar))
        if cookie_count == 0:
            raise HTTPException(
                status_code=400,
                detail="Nenhum cookie válido encontrado no ficheiro. Verifica se o formato é Netscape (cookies.txt) ou JSON.",
            )

        save_cookie_jar(jar, target_path)
        log.info(
            "Ficheiro cookies.txt guardado com sucesso: %d cookies, domínios: %s",
            cookie_count,
            domains[:10],
        )

        return {
            "ok": True,
            "count": cookie_count,
            "domains": domains,
            "message": f"{cookie_count} cookies carregados com sucesso para {len(domains)} domínio(s).",
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Falha ao processar cookies carregados: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Não foi possível processar o ficheiro de cookies: {exc}",
        ) from exc


@router.delete("", summary="Remover ficheiro de cookies atual")
async def delete_cookies() -> dict[str, Any]:
    """Remove o ficheiro cookies.txt carregado."""
    settings = get_settings()
    target_path = settings.data_dir / "cookies.txt"
    if target_path.exists():
        target_path.unlink(missing_ok=True)
        log.info("Ficheiro cookies.txt removido pelo utilizador.")
        return {"ok": True, "message": "Ficheiro de cookies removido com sucesso."}
    return {"ok": True, "message": "Nenhum ficheiro de cookies estava ativo."}

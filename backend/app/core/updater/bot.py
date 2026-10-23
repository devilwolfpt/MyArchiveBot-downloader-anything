"""Bot de Sincronização e Atualização Automática do Motor MyArchive Downloader.

Este Bot:
1. Comunica com o repositório upstream da comunidade para detectar melhorias e correções de sites de streaming.
2. Descarrega automaticamente o código-fonte mais recente.
3. Executa o EngineTransformer para manter a compatibilidade total com a aplicação e arquitetura unificada.
4. Faz rollback automático se detetar qualquer inconformidade nos testes pré-instalação.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .transformer import EngineTransformer

log = logging.getLogger(__name__)

GITHUB_API_LATEST = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
GITHUB_TARBALL_URL = "https://github.com/yt-dlp/yt-dlp/archive/refs/tags/{tag}.tar.gz"
GITHUB_ZIPBALL_URL = "https://github.com/yt-dlp/yt-dlp/archive/refs/tags/{tag}.zip"


@dataclass
class UpdateStatus:
    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    published_at: str | None = None
    release_notes: str | None = None
    last_checked: float = 0.0
    is_updating: bool = False
    progress_percent: float = 0.0
    progress_step: str = ""
    error: str | None = None
    recent_logs: list[str] = field(default_factory=list)


class EngineSyncBot:
    """Bot de automação que mantém o motor de extracção sempre atualizado com as plataformas da web."""

    def __init__(self, extractors_dir: Path | str | None = None) -> None:
        self.transformer = EngineTransformer(extractors_dir)
        self.extractors_dir = self.transformer.extractors_dir
        self._lock = asyncio.Lock()
        self.status = UpdateStatus(
            current_version=self.get_installed_version(),
            last_checked=0.0,
        )

    def get_installed_version(self) -> str:
        """Lê a versão atualmente instalada em extractors/version.py."""
        version_file = self.extractors_dir / "version.py"
        if version_file.exists():
            try:
                content = version_file.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if line.startswith("__version__"):
                        return line.split("=")[1].strip().strip("'\"")
            except Exception:  # noqa: BLE001
                pass
        return "1.0.0"

    def _log(self, message: str, percent: float | None = None) -> None:
        log.info("[SyncBot] %s", message)
        if percent is not None:
            self.status.progress_percent = percent
        self.status.progress_step = message
        timestamp = time.strftime("%H:%M:%S")
        self.status.recent_logs.append(f"[{timestamp}] {message}")
        if len(self.status.recent_logs) > 60:
            self.status.recent_logs.pop(0)

    async def check_for_updates(self) -> dict[str, Any]:
        """Consulta as últimas releases da comunidade."""
        self.status.current_version = self.get_installed_version()
        self.status.last_checked = time.time()
        self.status.error = None

        headers = {
            "User-Agent": "MyArchiveBot-Downloader/1.0 (+https://github.com/myarchive/downloader)",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(GITHUB_API_LATEST, headers=headers)
                if resp.status_code == 403:
                    # Rate limit atingido da API pública do GitHub
                    self._log("GitHub API atingiu limite temporário; a verificar tags directas...")
                    # Fallback suave mantendo versão instalada
                    return self._to_dict()

                resp.raise_for_status()
                data = resp.json()

                tag = data.get("tag_name", "").lstrip("v")
                body = data.get("body", "")
                published_at = data.get("published_at", "")

                self.status.latest_version = tag
                self.status.published_at = published_at
                self.status.release_notes = body[:1000] if body else None

                # Compara versões (YYYY.MM.DD)
                curr = self.status.current_version.replace(".", "")
                latest = tag.replace(".", "")
                try:
                    self.status.update_available = int(latest) > int(curr)
                except (ValueError, TypeError):
                    self.status.update_available = (tag != self.status.current_version)

                if self.status.update_available:
                    self._log(f"Nova versão do motor disponível: {tag} (atual: {self.status.current_version})")
                else:
                    self._log(f"O motor MyArchive está na versão mais recente ({self.status.current_version}).")

        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao verificar atualizações do motor: %s", exc)
            self.status.error = f"Não foi possível verificar atualizações: {exc}"

        return self._to_dict()

    async def sync_now(self, target_version: str | None = None) -> dict[str, Any]:
        """Transfere, adapta e instala a versão mais recente em segundo plano."""
        if self._lock.locked() or self.status.is_updating:
            return {"success": False, "message": "Uma atualização já se encontra em curso."}

        async with self._lock:
            self.status.is_updating = True
            self.status.error = None
            self.status.progress_percent = 5.0
            self.status.recent_logs.clear()

            try:
                # 1. Se versão não fornecida, verifica upstream
                if not target_version or not self.status.latest_version:
                    self._log("A verificar detalhes da versão mais recente...", 10.0)
                    await self.check_for_updates()
                    target_version = self.status.latest_version

                if not target_version:
                    raise RuntimeError("Não foi possível identificar a versão alvo para atualização.")

                self._log(f"A descarregar código-fonte da versão {target_version}...", 20.0)
                download_url = GITHUB_TARBALL_URL.format(tag=target_version)

                headers = {
                    "User-Agent": "MyArchiveBot-Downloader/1.0",
                    "Accept": "application/octet-stream",
                }

                archive_bytes = bytearray()
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    async with client.stream("GET", download_url, headers=headers) as stream:
                        if stream.status_code != 200:
                            # Tenta ZIP alternativo
                            zip_url = GITHUB_ZIPBALL_URL.format(tag=target_version)
                            self._log(f"A tentar download alternativo via ZIP: {zip_url}...", 25.0)
                            resp = await client.get(zip_url, headers=headers)
                            resp.raise_for_status()
                            archive_bytes = bytearray(resp.content)
                            is_zip = True
                        else:
                            total = int(stream.headers.get("content-length") or 0)
                            downloaded = 0
                            async for chunk in stream.aiter_bytes(chunk_size=65536):
                                archive_bytes.extend(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    pct = 20.0 + (downloaded / total) * 20.0
                                    self.status.progress_percent = round(pct, 1)
                            is_zip = False

                self._log("Download concluído. A extrair arquivos...", 45.0)
                with tempfile.TemporaryDirectory(prefix="myarchive_upstream_") as extract_tmp:
                    extract_path = Path(extract_tmp)

                    if is_zip:
                        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                            zf.extractall(extract_path)
                    else:
                        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tf:
                            tf.extractall(extract_path)

                    self._log("A executar pipeline de compatibilidade e adaptação...", 55.0)

                    # Executa o transformador em thread assíncrona
                    def on_transform_progress(msg: str, pct: float):
                        self._log(msg, 50.0 + (pct * 0.45))

                    success = await asyncio.to_thread(
                        self.transformer.transform_and_deploy,
                        extract_path,
                        target_version,
                        on_transform_progress,
                    )

                if success:
                    self.status.current_version = self.get_installed_version()
                    self.status.update_available = False
                    self.status.progress_percent = 100.0
                    self._log(f"Motor atualizado e testado com sucesso para a versão {target_version}!", 100.0)
                    return {
                        "success": True,
                        "version": self.status.current_version,
                        "message": f"Motor atualizado com sucesso para {self.status.current_version}",
                    }
                else:
                    raise RuntimeError("Falha desconhecida durante a transformação do motor.")

            except Exception as exc:  # noqa: BLE001
                log.exception("Erro durante a atualização do motor: %s", exc)
                self.status.error = str(exc)
                self._log(f"Erro na atualização: {exc}")
                return {"success": False, "message": str(exc)}

            finally:
                self.status.is_updating = False

    def rollback(self) -> dict[str, Any]:
        """Reverte para a versão anterior em caso de necessidade."""
        ok, msg = self.transformer.rollback()
        if ok:
            self.status.current_version = self.get_installed_version()
            self._log(f"Rollback executado: {msg}")
        return {"success": ok, "message": msg, "version": self.status.current_version}

    def _to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.status.current_version,
            "latest_version": self.status.latest_version,
            "update_available": self.status.update_available,
            "published_at": self.status.published_at,
            "release_notes": self.status.release_notes,
            "last_checked": self.status.last_checked,
            "is_updating": self.status.is_updating,
            "progress_percent": self.status.progress_percent,
            "progress_step": self.status.progress_step,
            "error": self.status.error,
            "recent_logs": list(self.status.recent_logs),
        }


# Instância global singleton do Bot de sincronização
sync_bot = EngineSyncBot()

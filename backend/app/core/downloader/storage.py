"""Armazenamento em disco: caminhos seguros, ficheiros parciais e limpeza."""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Storage:
    """Abstrai o sistema de ficheiros usado pelo downloader."""

    def __init__(self, downloads_dir: Path, temp_dir: Path) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.temp_dir = Path(temp_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def final_path(self, filename: str) -> Path:
        """Caminho final, garantindo unicidade (sufixo ``-1``, ``-2``...)."""
        base = Path(filename).name
        candidate = self.downloads_dir / base
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        index = 1
        while True:
            candidate = self.downloads_dir / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def part_path(self, target: Path) -> Path:
        return target.with_suffix(target.suffix + ".part")

    def job_parts_dir(self, job_id: str) -> Path:
        path = self.temp_dir / job_id / "parts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_dir(self, job_id: str) -> Path:
        path = self.temp_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ #
    @staticmethod
    def file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def cleanup_job(self, job_id: str) -> None:
        path = self.temp_dir / job_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def purge_expired(self, ttl_seconds: int) -> int:
        """Remove ficheiros parciais mais antigos que ``ttl_seconds``."""
        cutoff = time.time() - ttl_seconds
        removed = 0
        for child in self.temp_dir.iterdir() if self.temp_dir.exists() else []:
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def delete(self, path: Path) -> None:
        try:
            if path.exists():
                os.remove(path)
        except OSError as exc:
            log.warning("Não foi possível apagar %s: %s", path, exc)

    def disk_free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.downloads_dir).free
        except OSError:
            return 0

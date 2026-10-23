"""Fixtures partilhadas."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from app.config import Settings

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Executa uma corrotina sem depender de pytest-asyncio."""
    return asyncio.run(coro)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolados por teste (sem tocar no sistema de ficheiros real)."""
    return Settings(
        data_dir=tmp_path,
        downloads_dir=tmp_path / "downloads",
        cache_dir=tmp_path / "cache",
        temp_dir=tmp_path / "tmp",
        debug=True,
        api_key=None,
        rate_limit_per_minute=10_000,
        download_concurrency=4,
        max_retries=3,
        retry_backoff_base=1.0,
        retry_backoff_max=0.1,
        allow_private_urls=True,
        cache_enabled=False,
    )

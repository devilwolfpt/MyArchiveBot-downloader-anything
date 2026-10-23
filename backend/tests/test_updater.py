"""Testes do Bot de Sincronização e Atualização Automática do Motor."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.updater import EngineSyncBot, EngineTransformer, sync_bot
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_engine_status_endpoint(client: TestClient) -> None:
    resp = client.get("/api/engine/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_version" in data
    assert "is_updating" in data
    assert isinstance(data["recent_logs"], list)


def test_engine_check_endpoint(client: TestClient) -> None:
    fake_status = {
        "current_version": "2026.08.19",
        "latest_version": "2026.09.01",
        "update_available": True,
        "published_at": "2026-09-01T12:00:00Z",
        "release_notes": "Upstream improvements",
        "last_checked": 1000.0,
        "is_updating": False,
        "progress_percent": 0.0,
        "progress_step": "",
        "error": None,
        "recent_logs": [],
    }
    with patch.object(sync_bot, "check_for_updates", new_callable=AsyncMock) as mock_chk:
        mock_chk.return_value = fake_status
        resp = client.post("/api/engine/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest_version"] == "2026.09.01"
        assert data["update_available"] is True


def test_engine_transformer_pipeline() -> None:
    """Verifica que o pipeline achata extractors e adapta imports relativos correctamente."""
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dest_dir:
        src_root = Path(src_dir)
        dest_root = Path(dest_dir)

        # Cria estrutura de mock upstream:
        # upstream/yt_dlp/YoutubeDL.py
        # upstream/yt_dlp/extractor/dummy_site.py
        # upstream/yt_dlp/utils/__init__.py
        core = src_root / "yt_dlp"
        core.mkdir()
        (core / "YoutubeDL.py").write_text("# ydl\n", encoding="utf-8")
        (core / "version.py").write_text("__version__ = '2026.09.01'\n", encoding="utf-8")

        extractor_dir = core / "extractor"
        extractor_dir.mkdir()
        (extractor_dir / "dummy.py").write_text(
            "from __future__ import annotations\nfrom ..utils import clean_html\nfrom .common import InfoExtractor\n",
            encoding="utf-8",
        )

        utils_dir = core / "utils"
        utils_dir.mkdir()
        (utils_dir / "__init__.py").write_text("def clean_html(x): return x\n", encoding="utf-8")

        transformer = EngineTransformer(extractors_dir=dest_root)

        # 1. Testa localização do núcleo
        found = transformer._find_upstream_core(src_root)
        assert found == core

        # 2. Testa achatamento para staging
        staging = dest_root / "staging"
        staging.mkdir()
        transformer._flatten_and_copy(core, staging)
        assert (staging / "dummy.py").exists()
        assert (staging / "YoutubeDL.py").exists()
        assert not (staging / "extractor").exists()

        # 3. Testa adaptação de imports relativos
        transformer._rewrite_imports(staging)
        dummy_content = (staging / "dummy.py").read_text(encoding="utf-8")
        assert "from .utils import clean_html" in dummy_content
        assert "from ..utils" not in dummy_content


def test_engine_rollback(client: TestClient) -> None:
    resp = client.post("/api/engine/rollback")
    assert resp.status_code == 200
    data = resp.json()
    assert "success" in data
    assert "message" in data

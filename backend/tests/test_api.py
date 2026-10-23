"""Testes da camada HTTP (FastAPI TestClient)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.downloader.manager import JobManager
from app.core.exceptions import ExtractionError
from app.core.models import JobPhase, JobStatus, MediaInfo, MediaType, StreamFormat
from app.main import app, app_state


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "youtube" in body["extractors"]
        assert "generic" in body["extractors"]

    def test_root_advertises_endpoints(self, client: TestClient) -> None:
        body = client.get("/").json()
        assert body["endpoints"]["extract"].endswith("/extract-info")


class TestAuth:
    def test_rejects_missing_api_key(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("MAD_API_KEY", "segredo")
        get_settings.cache_clear()
        response = client.post("/api/extract-info", json={"url": "https://exemplo.test/v"})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "unauthorized"

    def test_accepts_valid_api_key(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("MAD_API_KEY", "segredo")
        get_settings.cache_clear()
        response = client.get("/api/formats", headers={"X-API-Key": "segredo"})
        assert response.status_code == 200


class TestExtract:
    def test_validates_payload(self, client: TestClient) -> None:
        response = client.post("/api/extract-info", json={"url": "x"})
        assert response.status_code == 422

    def test_returns_media_info(self, client: TestClient, monkeypatch) -> None:
        async def fake_extract(self, url, **kwargs):
            return MediaInfo(
                extractor="fake",
                webpage_url=url,
                original_url=url,
                title="Vídeo de Teste",
                duration=120.0,
                formats=[
                    StreamFormat(
                        id="720",
                        url="https://cdn.test/720.mp4",
                        media_type=MediaType.VIDEO,
                        height=720,
                    )
                ],
            )

        from app.services.extraction import ExtractionService

        monkeypatch.setattr(ExtractionService, "extract", fake_extract)
        response = client.post("/api/extract-info", json={"url": "https://exemplo.test/video"})
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Vídeo de Teste"
        assert body["formats"][0]["height"] == 720

    def test_maps_business_errors(self, client: TestClient, monkeypatch) -> None:
        async def fake_extract(self, url, **kwargs):
            raise ExtractionError("Vídeo indisponível", details={"code": "x"})

        from app.services.extraction import ExtractionService

        monkeypatch.setattr(ExtractionService, "extract", fake_extract)
        response = client.post("/api/extract-info", json={"url": "https://exemplo.test/video"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "extraction_failed"


class TestJobLifecycle:
    def test_create_job_and_stream_progress(self, client: TestClient, monkeypatch) -> None:
        from app.core.models import ProgressEvent

        async def fake_execute(self, job, request):
            for pct in (0.0, 25.0, 50.0, 100.0):
                await self.jobs.publish(
                    job,
                    ProgressEvent(
                        job_id=job.id,
                        status=JobStatus.RUNNING,
                        phase=JobPhase.DOWNLOADING,
                        percent=pct,
                        downloaded_bytes=int(pct * 10),
                        total_bytes=100,
                        message=f"{pct}%",
                    ),
                )
            from app.core.models import JobResult

            return JobResult(
                job_id=job.id,
                status=JobStatus.COMPLETED,
                filename="video.mp4",
                path="/tmp/video.mp4",
                size=100,
            )

        from app.services.pipeline import DownloadService

        monkeypatch.setattr(DownloadService, "execute", fake_execute)

        response = client.post("/api/download", json={"url": "https://exemplo.test/video"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        # O job precisa de existir no registo para o WS subscrever.
        with client.websocket_connect(f"/api/ws/jobs/{job_id}") as ws:
            received = []
            for _ in range(6):
                message = ws.receive_json()
                if "error" in message:
                    break
                received.append(message)
                if message.get("status") in ("completed", "failed", "cancelled"):
                    break

        assert received, "não recebeu eventos de progresso"
        percents = [m["percent"] for m in received]
        assert percents == sorted(percents)

    def test_job_not_found(self, client: TestClient) -> None:
        response = client.get("/api/jobs/inexistente")
        assert response.status_code == 404

    def test_ws_unknown_job(self, client: TestClient) -> None:
        with client.websocket_connect("/api/ws/jobs/inexistente") as ws:
            message = ws.receive_json()
            assert "error" in message

    def test_job_snapshot_exposes_warnings(self, client: TestClient, monkeypatch) -> None:
        """Os avisos do pós-processamento têm de chegar ao cliente."""
        from app.core.models import JobResult

        async def fake_execute(self, job, request):
            return JobResult(
                job_id=job.id,
                status=JobStatus.COMPLETED,
                filename="video.mp4",
                path="/tmp/video.mp4",
                size=100,
                warnings=["FFmpeg indisponível: ficheiro original entregue"],
            )

        from app.services.pipeline import DownloadService

        monkeypatch.setattr(DownloadService, "execute", fake_execute)

        response = client.post("/api/download", json={"url": "https://exemplo.test/video"})
        job_id = response.json()["job_id"]

        # Aguarda o job terminar.
        for _ in range(50):
            snapshot = client.get(f"/api/jobs/{job_id}").json()
            if snapshot["status"] in ("completed", "failed", "cancelled"):
                break
        assert snapshot["status"] == "completed"
        assert snapshot["result"]["warnings"] == [
            "FFmpeg indisponível: ficheiro original entregue"
        ]


class TestJobManager:
    def test_publish_and_subscribe(self) -> None:
        async def scenario():
            manager = JobManager()
            job = await manager.create("https://exemplo.test/v")

            events: list = []

            async def consume():
                async for event in manager.subscribe(job.id):
                    events.append(event)

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.05)
            await manager.update_status(job, JobStatus.RUNNING, message="a começar")
            await asyncio.sleep(0.05)
            await manager.update_status(job, JobStatus.COMPLETED)
            await asyncio.sleep(0.05)
            await asyncio.wait_for(task, timeout=2)
            return events

        events = asyncio.run(scenario())
        assert len(events) >= 2
        assert events[-1].status is JobStatus.COMPLETED

    def test_cancel(self) -> None:
        async def scenario():
            manager = JobManager()
            job = await manager.create("https://exemplo.test/v")
            await manager.cancel(job.id)
            return job

        job = asyncio.run(scenario())
        assert job.status is JobStatus.CANCELLED
        assert job.cancel_event.is_set()

    def test_concurrency_limit_queues_extra_jobs(self) -> None:
        """Com ``max_concurrent=1`` o segundo job fica em ``queued``."""

        async def scenario():
            manager = JobManager(max_concurrent=1)
            first = await manager.create("https://exemplo.test/1")
            second = await manager.create("https://exemplo.test/2")

            release = asyncio.Event()
            started = asyncio.Event()

            async def blocking(_job):
                started.set()
                await release.wait()
                return None

            task1 = asyncio.create_task(manager.run(first, blocking))
            await asyncio.wait_for(started.wait(), timeout=2)

            task2 = asyncio.create_task(manager.run(second, blocking))
            await asyncio.sleep(0.05)
            # O segundo job ainda não pode executar: está à espera de vaga.
            assert second.status is JobStatus.QUEUED
            assert second.phase is JobPhase.QUEUED
            assert not task2.done()

            release.set()
            await asyncio.wait_for(task1, timeout=2)
            await asyncio.wait_for(task2, timeout=2)
            return second

        second = asyncio.run(scenario())
        assert second.status is JobStatus.COMPLETED

    def test_queued_job_cancellation_publishes_terminal_event(self) -> None:
        """Cancelar um job em fila deve emitir sempre um evento terminal."""

        async def scenario():
            manager = JobManager(max_concurrent=1)
            first = await manager.create("https://exemplo.test/1")
            second = await manager.create("https://exemplo.test/2")

            release = asyncio.Event()
            started = asyncio.Event()

            async def blocking(_job):
                started.set()
                await release.wait()
                return None

            task1 = asyncio.create_task(manager.run(first, blocking))
            await asyncio.wait_for(started.wait(), timeout=2)
            task2 = asyncio.create_task(manager.run(second, blocking))
            await asyncio.sleep(0.05)

            await manager.cancel(second.id)
            await asyncio.gather(task2, return_exceptions=True)
            release.set()
            await asyncio.wait_for(task1, timeout=2)
            return second

        second = asyncio.run(scenario())
        assert second.status is JobStatus.CANCELLED
        assert second.progress is not None
        assert second.progress.status is JobStatus.CANCELLED

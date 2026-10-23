"""Testes da renovação de URL em caso de 403 (media URL expirada).

As URLs de media do YouTube estão vinculadas ao IP de origem. Se o IP de
saída mudar entre a extracção e o download, o CDN responde 403 — o serviço
tem de re-extrair os metadados e tentar de novo com uma URL fresca.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.core.downloader.chunk import ChunkDownloader
from app.core.downloader.manager import JobManager
from app.core.downloader.storage import Storage
from app.core.exceptions import MediaUrlExpiredError
from app.core.http.client import HttpClient
from app.core.models import (
    Container,
    DownloadMode,
    DownloadRequest,
    MediaInfo,
    MediaType,
    StreamFormat,
)
from app.services.extraction import ExtractionService
from app.services.pipeline import DownloadService
from tests.conftest import run


# --------------------------------------------------------------------------- #
# Chunk downloader: 403 -> erro de URL expirada (sem retentativas inuteis)
# --------------------------------------------------------------------------- #
def test_forbidden_raises_media_url_expired(settings) -> None:
    """Um 403 não deve ser retentado 5 vezes: a URL é que está má."""
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(403, json={"error": "expired"})

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            from app.core.downloader.plan import DownloadPlan

            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/video.mp4",
                filename="video.mp4",
                total_bytes=1000,
            )
            await downloader.download(plan, job_id="job-403")

    with pytest.raises(MediaUrlExpiredError):
        run(scenario())

    # Uma única tentativa: repetir a mesma URL não resolve.
    assert len(attempts) == 1


def test_server_error_is_still_retried(settings) -> None:
    """Um 500 é transitório: continua a ser retentado."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            content=b"X" * 1000,
            headers={"Content-Length": "1000"},
        )

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            from app.core.downloader.plan import DownloadPlan

            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/video.mp4",
                filename="video.mp4",
                total_bytes=1000,
            )
            return await downloader.download(plan, job_id="job-503")

    path = run(scenario())
    assert path.exists()
    assert len(calls) == 3


# --------------------------------------------------------------------------- #
# Downloader: auto-repara quando o provider devolve um plano novo
# --------------------------------------------------------------------------- #
def test_downloader_renews_plan_on_403(settings) -> None:
    """Com plan_provider, um 403 é reparado e o download prossegue."""
    from app.core.downloader.plan import DownloadPlan

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(403)
        return httpx.Response(
            206,
            content=b"X" * 500,
            headers={"Content-Range": "bytes 0-499/500", "Content-Length": "500"},
        )

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/velha.mp4",
                filename="video.mp4",
                total_bytes=500,
            )
            renewed = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/nova.mp4",
                filename="video.mp4",
                total_bytes=500,
            )

            async def provider():
                return renewed

            return await downloader.download(plan, job_id="job-renew", plan_provider=provider)

    path = run(scenario())
    assert path.exists()
    assert len(requests) == 2
    assert "nova.mp4" in requests[1], "a segunda tentativa usa a URL renovada"


def test_downloader_without_provider_raises(settings) -> None:
    """Sem provider, o 403 dá origem a MediaUrlExpiredError."""
    from app.core.downloader.plan import DownloadPlan

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/velha.mp4",
                filename="video.mp4",
                total_bytes=500,
            )
            await downloader.download(plan, job_id="job-norenew")

    with pytest.raises(MediaUrlExpiredError):
        run(scenario())


# --------------------------------------------------------------------------- #
# Pipeline: re-extrai e repete
# --------------------------------------------------------------------------- #
def _format(url: str, fmt_id: str = "137") -> StreamFormat:
    return StreamFormat(
        id=fmt_id,
        url=url,
        media_type=MediaType.VIDEO,
        container=Container.MP4,
        width=1920,
        height=1080,
        tbr=4000,
        acodec=None,
        vcodec="avc1.640028",
        filesize=1000,
    )


def _info(url: str) -> MediaInfo:
    return MediaInfo(
        webpage_url="https://www.youtube.com/watch?v=abc",
        original_url="https://www.youtube.com/watch?v=abc",
        title="Vídeo Teste",
        duration=100.0,
        formats=[_format(url)],
    )


class _FakeExtraction:
    """Devolve URLs diferentes em cada extracção (como o YouTube faz)."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.calls = 0
        self.use_cache_flags: list[bool] = []

    async def extract(self, url, *, headers=None, cookies=None, use_cache: bool = True) -> MediaInfo:
        index = min(self.calls, len(self.urls) - 1)
        self.calls += 1
        self.use_cache_flags.append(use_cache)
        return _info(self.urls[index])


class _FakeDownloader:
    """Finge um 403 e usa o ``plan_provider`` para se auto-reparar.

    ``fail_times=None`` significa "falha sempre"; um número significa
    quantas tentativas falham antes deucceedir.
    """

    def __init__(self, http, settings, storage, *, fail_times: int | None = 1) -> None:
        self.http = http
        self.settings = settings
        self.storage = storage
        self.fail_times = fail_times
        self.urls: list[str] = []

    async def download(
        self,
        plan,
        *,
        on_progress=None,
        cancel_event=None,
        job_id="job",
        plan_provider=None,
    ):
        for _ in range(10):
            self.urls.append(plan.url)
            failing = self.fail_times is None or self.fail_times > 0
            if not failing:
                break
            if self.fail_times is not None:
                self.fail_times -= 1

            renewed = await plan_provider() if plan_provider is not None else None
            if renewed is None:
                raise MediaUrlExpiredError("A URL de media expirou (403)")
            plan = renewed
        else:  # pragma: no cover - apenas se exceder o limite
            raise MediaUrlExpiredError("Demasiadas renovações")

        path = self.storage.final_path(plan.filename)
        path.write_bytes(b"X" * 1000)
        return path


def test_pipeline_refreshes_url_after_403(settings, monkeypatch) -> None:
    """O 403 obriga a re-extrair: a segunda tentativa usa a URL nova."""
    settings.enable_postprocess = False
    settings.max_url_refreshes = 2

    extraction = _FakeExtraction(
        [
            "https://cdn.example.com/old.mp4?sig=antiga",
            "https://cdn.example.com/nova.mp4?sig=nova",
        ]
    )
    downloader = _FakeDownloader(None, settings, Storage(settings.downloads_dir, settings.temp_dir))

    monkeypatch.setattr("app.services.pipeline.ChunkDownloader", lambda *a, **k: downloader)

    jobs = JobManager()
    service = DownloadService(settings, jobs, extraction)
    request = DownloadRequest(url="https://www.youtube.com/watch?v=abc", mode=DownloadMode.VIDEO)

    async def scenario():
        job = await jobs.create(request.url)
        return await jobs.run(job, lambda j: service.execute(j, request))

    result = run(scenario())

    assert result is not None, "o job devia concluir após a renovação"
    assert extraction.calls == 2, "a extracção tem de ser repetida"
    assert downloader.urls == [
        "https://cdn.example.com/old.mp4?sig=antiga",
        "https://cdn.example.com/nova.mp4?sig=nova",
    ]
    # A renovação tem de ignorar o cache: uma URL em cache pode estar
    # presa ao IP antigo — que é a causa do 403.
    assert extraction.use_cache_flags == [True, False]


def test_pipeline_gives_up_after_max_refreshes(settings, monkeypatch) -> None:
    """Sem renovações infinitas: acaba por falhar com um erro claro."""
    settings.enable_postprocess = False
    settings.max_url_refreshes = 1

    extraction = _FakeExtraction(["https://cdn.example.com/velha.mp4"])
    downloader = _FakeDownloader(
        None, settings, Storage(settings.downloads_dir, settings.temp_dir), fail_times=None
    )

    monkeypatch.setattr("app.services.pipeline.ChunkDownloader", lambda *a, **k: downloader)

    jobs = JobManager()
    service = DownloadService(settings, jobs, extraction)
    request = DownloadRequest(url="https://www.youtube.com/watch?v=abc", mode=DownloadMode.VIDEO)

    async def scenario():
        job = await jobs.create(request.url)
        return await jobs.run(job, lambda j: service.execute(j, request))

    result = run(scenario())

    assert result is None, "o job devia falhar"
    # 1 tentativa inicial + 1 renovação
    assert len(downloader.urls) == 2
    assert extraction.calls == 2


def test_refresh_keeps_the_same_format(settings) -> None:
    """A renovação mantém o itag escolhido pelo utilizador."""
    from app.services.pipeline import DownloadService

    info = _info("https://cdn.example.com/nova.mp4")
    found = DownloadService._find_format(info, "137")
    assert found is not None
    assert found.url == "https://cdn.example.com/nova.mp4"
    assert DownloadService._find_format(info, "999") is None

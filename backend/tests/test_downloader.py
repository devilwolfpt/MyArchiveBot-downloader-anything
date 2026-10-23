"""Testes do chunk downloader com um servidor HTTP simulado."""
from __future__ import annotations

from pathlib import Path

import httpx

from app.core.downloader.chunk import ChunkDownloader
from app.core.downloader.plan import DownloadPlan, SegmentPlan
from app.core.downloader.storage import Storage
from app.core.exceptions import DownloadCancelledError
from app.core.http.client import HttpClient
from tests.conftest import run

FILE_BODY = b"A" * 5000 + b"B" * 5000
SEGMENTS = [b"segmento-0" * 10, b"segmento-1" * 10, b"segmento-2" * 10]


class FakeServer:
    """Servidor HTTP em memória com suporte a Range e falhas injectáveis."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_times = 0
        self.forbidden_times = 0
        self.files: dict[str, bytes] = {
            "/video.mp4": FILE_BODY,
            "/seg0.ts": SEGMENTS[0],
            "/seg1.ts": SEGMENTS[1],
            "/seg2.ts": SEGMENTS[2],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if self.forbidden_times > 0:
            self.forbidden_times -= 1
            return httpx.Response(403, text="token expirado")

        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(503, text="indisponivel")

        body = self.files.get(path)
        if body is None:
            return httpx.Response(404, text="nao encontrado")

        range_header = request.headers.get("range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes=") :]
            raw_start, _, raw_end = spec.partition("-")
            start = int(raw_start)
            end = int(raw_end) if raw_end else len(body) - 1
            end = min(end, len(body) - 1)
            chunk = body[start : end + 1]
            return httpx.Response(
                206,
                content=chunk,
                headers={
                    "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(body)}",
                    "Content-Length": str(len(chunk)),
                },
            )

        return httpx.Response(
            200,
            content=body,
            headers={"Content-Length": str(len(body)), "Accept-Ranges": "bytes"},
        )


def build_client(server: FakeServer, settings) -> HttpClient:
    return HttpClient(settings, transport=httpx.MockTransport(server.handler))


class TestProgressiveDownload:
    def test_downloads_full_file(self, settings, tmp_path: Path) -> None:
        server = FakeServer()

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                    total_bytes=len(FILE_BODY),
                )
                events: list[tuple[int, int | None]] = []

                async def on_progress(downloaded, total, seg_idx, seg_total):
                    events.append((downloaded, total))

                path = await downloader.download(plan, on_progress=on_progress)
                return path, events

        path, events = run(scenario())
        assert path.exists()
        assert path.read_bytes() == FILE_BODY
        assert path.parent == settings.downloads_dir
        assert events, "o callback de progresso não foi invocado"
        assert events[-1][0] == len(FILE_BODY)
        # O progresso deve ser monótono
        values = [e[0] for e in events]
        assert values == sorted(values)

    def test_resumes_from_partial_file(self, settings) -> None:
        server = FakeServer()

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)

                target = storage.final_path("video.mp4")
                part = storage.part_path(target)
                part.write_bytes(FILE_BODY[:2000])

                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                    total_bytes=len(FILE_BODY),
                )
                return await downloader.download(plan), server

        path, srv = run(scenario())
        assert path.read_bytes() == FILE_BODY
        # Deve ter usado Range
        ranged = [r for r in srv.requests if r.headers.get("range")]
        assert ranged, "esperado um pedido com Range para retomar"
        assert ranged[0].headers["range"] == "bytes=2000-"

    def test_retries_on_server_error(self, settings) -> None:
        server = FakeServer()
        server.fail_times = 2

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                )
                return await downloader.download(plan)

        path = run(scenario())
        assert path.read_bytes() == FILE_BODY

    def test_respects_max_file_size(self, settings) -> None:
        server = FakeServer()
        settings.max_file_size = 100

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                )
                await downloader.download(plan)

        try:
            run(scenario())
        except Exception as exc:  # noqa: BLE001
            assert "tamanho" in str(exc).lower() or "excede" in str(exc).lower()
        else:
            raise AssertionError("deveria ter falhado por exceder o limite")


class TestParallelDownload:
    """O download paralelo divide o ficheiro em varios pedidos Range."""

    def _settings(self, settings):
        settings.parallel_download = True
        settings.parallel_min_size = 1024
        settings.parallel_min_chunk_size = 512
        settings.download_concurrency = 4
        return settings

    def test_splits_file_in_several_range_requests(self, settings) -> None:
        server = FakeServer()
        settings = self._settings(settings)

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                    total_bytes=len(FILE_BODY),
                )
                events: list[tuple[int, int | None]] = []

                async def on_progress(downloaded, total, seg_idx, seg_total):
                    events.append((downloaded, total))

                path = await downloader.download(
                    plan, on_progress=on_progress, job_id="job-parallel"
                )
                return path, events

        path, events = run(scenario())
        assert path.read_bytes() == FILE_BODY

        ranged = [r.headers["range"] for r in server.requests if r.headers.get("range")]
        assert len(ranged) > 1, f"esperado varios pedidos Range, obtive {ranged}"
        # Os intervalos nao podem sobrepor-se.
        starts = sorted(int(r.split("=")[1].split("-")[0]) for r in ranged)
        assert starts == sorted(set(starts))
        assert events[-1][0] == len(FILE_BODY)
        values = [e[0] for e in events]
        assert values == sorted(values), "o progresso tem de ser monotonico"

    def test_falls_back_when_server_ignores_range(self, settings) -> None:
        """Servidor que responde 200 -> download single-stream continua a funcionar."""
        server = FakeServer()
        settings = self._settings(settings)

        def handler(request: httpx.Request) -> httpx.Response:
            server.requests.append(request)
            body = server.files[request.url.path]
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Length": str(len(body)), "Accept-Ranges": "none"},
            )

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                    total_bytes=len(FILE_BODY),
                )
                return await downloader.download(plan, job_id="job-norange")

        path = run(scenario())
        assert path.read_bytes() == FILE_BODY

    def test_resumes_completed_chunks(self, settings) -> None:
        """Os chunks ja completos em disco nao sao redescarregados."""
        server = FakeServer()
        settings = self._settings(settings)

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="progressive",
                    url="https://cdn.example.com/video.mp4",
                    filename="video.mp4",
                    total_bytes=len(FILE_BODY),
                )
                return await downloader.download(plan, job_id="job-resume-parallel")

        # Prepara um job anterior com um chunk completo.
        storage = Storage(settings.downloads_dir, settings.temp_dir)
        downloader = ChunkDownloader(None, settings, storage)  # type: ignore[arg-type]
        ranges = downloader._split_ranges(len(FILE_BODY))
        prefix = __import__("hashlib").sha1(b"video.mp4").hexdigest()[:12]
        parts_dir = storage.job_parts_dir("job-resume-parallel") / prefix
        parts_dir.mkdir(parents=True, exist_ok=True)
        start, end = ranges[0]
        (parts_dir / "chunk-00000.part").write_bytes(FILE_BODY[start : end + 1])

        before = len(server.requests)
        path = run(scenario())
        assert path.read_bytes() == FILE_BODY
        after = len(server.requests)
        # O primeiro chunk nao foi pedido outra vez.
        assert after - before < len(ranges)

    def test_split_ranges_covers_the_whole_file(self, settings) -> None:
        settings = self._settings(settings)
        storage = Storage(settings.downloads_dir, settings.temp_dir)
        downloader = ChunkDownloader(None, settings, storage)  # type: ignore[arg-type]

        for total in (1, 999, 1000, 10_000, 123_456):
            ranges = downloader._split_ranges(total)
            assert ranges[0][0] == 0
            assert ranges[-1][1] == total - 1
            assert all(end - start + 1 > 0 for start, end in ranges)
            # Sem lacunas nem sobreposições.
            for previous, current in zip(ranges, ranges[1:]):
                assert previous[1] + 1 == current[0]
            assert sum(end - start + 1 for start, end in ranges) == total


class TestSegmentedDownload:
    def test_downloads_and_concatenates(self, settings) -> None:
        server = FakeServer()

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                plan = DownloadPlan(
                    kind="segmented",
                    url="https://cdn.example.com/media.m3u8",
                    filename="media.ts",
                    segments=[
                        SegmentPlan(index=0, url="https://cdn.example.com/seg0.ts"),
                        SegmentPlan(index=1, url="https://cdn.example.com/seg1.ts"),
                        SegmentPlan(index=2, url="https://cdn.example.com/seg2.ts"),
                    ],
                )
                events: list[tuple[int, int, int]] = []

                async def on_progress(downloaded, total, seg_idx, seg_total):
                    events.append((downloaded, seg_idx, seg_total))

                path = await downloader.download(
                    plan, on_progress=on_progress, job_id="job-1"
                )
                return path, events

        path, events = run(scenario())
        assert path.exists()
        expected = b"".join(SEGMENTS)
        assert path.read_bytes() == expected
        assert events[-1][1] == 3  # 3 segmentos concluídos

    def test_cancellation(self, settings) -> None:
        import asyncio

        server = FakeServer()

        async def scenario():
            async with build_client(server, settings) as http:
                storage = Storage(settings.downloads_dir, settings.temp_dir)
                downloader = ChunkDownloader(http, settings, storage)
                cancel = asyncio.Event()
                cancel.set()
                plan = DownloadPlan(
                    kind="segmented",
                    url="https://cdn.example.com/media.m3u8",
                    filename="media.ts",
                    segments=[SegmentPlan(index=0, url="https://cdn.example.com/seg0.ts")],
                )
                await downloader.download(plan, cancel_event=cancel, job_id="job-cancel")

        try:
            run(scenario())
        except DownloadCancelledError:
            pass
        else:
            raise AssertionError("deveria ter launchado DownloadCancelledError")

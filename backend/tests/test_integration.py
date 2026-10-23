"""Teste de integração ponta-a-ponta.

Levanta um servidor HTTP real (media + playlist HLS) e o motor de download
por cima, e percorre o fluxo completo: extracção -> job -> progresso -> ficheiro.
"""
from __future__ import annotations

import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from app.config import get_settings
from app.core.downloader.manager import JobManager
from app.core.http.client import HttpClient
from app.core.models import DownloadRequest, DownloadMode
from app.services.extraction import ExtractionService
from app.services.pipeline import DownloadService

MEDIA = b"X" * 40_000
SEGMENTS = [b"seg-%d-" % i + b"Y" * 500 for i in range(4)]


class _Handler(SimpleHTTPRequestHandler):
    """Servidor estático simples que também serve os segmentos HLS."""

    def log_message(self, *args):  # silence
        pass

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(self._size()))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _size(self) -> int:
        path = self.path.split("?")[0]
        if path.endswith(".mp4"):
            return len(MEDIA)
        if path.endswith(".m3u8"):
            return len(self._playlist().encode())
        for index, segment in enumerate(SEGMENTS):
            if path.endswith(f"seg{index}.ts"):
                return len(segment)
        return 0

    def _playlist(self) -> str:
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
        for index in range(len(SEGMENTS)):
            lines.append("#EXTINF:4.0,")
            lines.append(f"seg{index}.ts")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.endswith(".mp4"):
            self._serve(MEDIA)
            return
        if path.endswith(".m3u8"):
            self._serve(self._playlist().encode())
            return
        for index, segment in enumerate(SEGMENTS):
            if path.endswith(f"seg{index}.ts"):
                self._serve(segment)
                return
        self.send_error(404)

    def _serve(self, body: bytes) -> None:
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header[6:].split("-")[0])
            chunk = body[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class TestEndToEnd:
    def test_direct_file_download(self, settings) -> None:
        with _Server() as server:
            result = asyncio.run(self._run_flow(settings, f"{server.base}/video.mp4"))
        assert result["status"] == "completed"
        assert result["size"] == len(MEDIA)
        assert result["path"] and Path(result["path"]).exists()

    def test_hls_playlist_download(self, settings) -> None:
        with _Server() as server:
            result = asyncio.run(self._run_flow(settings, f"{server.base}/media.m3u8"))
        assert result["status"] == "completed"
        assert result["path"] and Path(result["path"]).exists()
        assert Path(result["path"]).stat().st_size == sum(len(s) for s in SEGMENTS)

    # ------------------------------------------------------------------ #
    async def _run_flow(self, settings, url: str) -> dict:
        jobs = JobManager()
        extraction = ExtractionService(settings)
        service = DownloadService(settings, jobs, extraction)

        request = DownloadRequest(url=url, mode=DownloadMode.VIDEO)

        info = await extraction.extract(url)
        assert info.formats, "a extracção devolveu formatos"

        job = await jobs.create(url)
        # Mesmo caminho usado pela rota POST /api/download
        result = await jobs.run(job, lambda j: service.execute(j, request))
        assert result is not None, f"o job falhou: {job.error}"
        return {
            "status": job.status.value,
            "size": result.size,
            "path": result.path,
            "filename": result.filename,
            "formats": len(info.formats),
        }


class TestHttpClientIntegration:
    def test_client_against_real_server(self, settings) -> None:
        with _Server() as server:
            async def scenario():
                async with HttpClient(settings) as http:
                    result = await http.get(f"{server.base}/video.mp4")
                    size = await http.content_length(f"{server.base}/video.mp4")
                    return len(result.content), size

            length, reported = asyncio.run(scenario())

        assert length == len(MEDIA)
        assert reported == len(MEDIA)

    def test_client_retries_on_500(self, settings) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500)
            return httpx.Response(200, content=b"ok")

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                return await http.get("https://cdn.example.com/x")

        result = asyncio.run(scenario())
        assert result.content == b"ok"
        assert calls["n"] >= 3

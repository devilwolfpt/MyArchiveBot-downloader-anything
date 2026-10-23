"""Testes do header ``Range`` obrigatório (``rqh=1`` do YouTube).

O CDN do YouTube assina os URLs de media com ``rqh=1`` ("Range Header
Required"): um GET sem header ``Range`` é recusado com HTTP 403. O
downloader tem de enviar sempre um Range, mesmo quando começa do início.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.downloader.chunk import ChunkDownloader
from app.core.downloader.plan import DownloadPlan
from app.core.downloader.storage import Storage
from app.core.http.client import HttpClient
from tests.conftest import run


def _range_required(request: httpx.Request, total: int = 101) -> httpx.Response:
    """Servidor que só aceita pedidos com header Range."""
    if "range" not in request.headers:
        return httpx.Response(403, text="Range header required")
    value = request.headers["range"]
    span = value.removeprefix("bytes=")
    start = int(span.split("-")[0] or 0)
    end = int(span.split("-")[1]) if "-" in span and span.split("-")[1] else start + 99
    end = min(end, total - 1)
    body = bytes((start + i) % 256 for i in range(end - start + 1))
    return httpx.Response(
        206,
        content=body,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(body)),
        },
    )


def test_progressive_download_always_sends_range(settings) -> None:
    """O download progressivo tem de enviar Range mesmo a começar do zero."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        return _range_required(request)

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/video.mp4",
                filename="video.mp4",
                total_bytes=100,
            )
            return await downloader.download(plan, job_id="job-rqh")

    path = run(scenario())
    assert path.exists()
    assert seen, "o downloador tem de fazer pelo menos um pedido"
    assert all(value is not None for value in seen), f"pedidos sem Range: {seen}"
    assert seen[0] == "bytes=0-", f"Range inicial inesperado: {seen[0]}"


def test_content_length_falls_back_to_range_probe(settings) -> None:
    """``content_length`` usa a sonda com Range quando o HEAD é recusado."""
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(403)
        return _range_required(request)

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            return await http.content_length("https://cdn.example.com/video.mp4")

    total = run(scenario())
    assert total == 101, f"tamanho inesperado: {total}"
    assert any(method == "GET" and rng is not None for method, rng in calls), calls


def test_content_length_uses_head_when_possible(settings) -> None:
    """Quando o HEAD funciona, não se faz a sonda extra."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "4321"})

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            return await http.content_length("https://cdn.example.com/video.mp4")

    assert run(scenario()) == 4321


def test_server_without_range_support_still_works(settings) -> None:
    """Servidores que ignoram o Range (200) continuam a funcionar."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"X" * 100, headers={"Content-Length": "100"})

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            storage = Storage(settings.downloads_dir, settings.temp_dir)
            downloader = ChunkDownloader(http, settings, storage)
            plan = DownloadPlan(
                kind="progressive",
                url="https://cdn.example.com/video.mp4",
                filename="video.mp4",
                total_bytes=100,
            )
            return await downloader.download(plan, job_id="job-norange")

    path = run(scenario())
    assert path.exists()
    assert path.stat().st_size == 100

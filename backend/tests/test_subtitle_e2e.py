"""Teste de integração: download completo com legendas.

Percorre o pipeline inteiro (extracção simulada -> selecção -> download ->
mux das legendas) contra um servidor HTTP local que serve um MP4 real e
uma faixa WebVTT, e verifica com ``ffprobe`` que o ficheiro final tem
vídeo, áudio **e** a faixa de legendas.

É ignorado se o FFmpeg não estiver disponível.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.downloader.manager import JobManager
from app.core.models import (
    Container,
    DownloadMode,
    DownloadRequest,
    MediaInfo,
    MediaType,
    StreamFormat,
    SubtitleTrack,
)
from app.services.pipeline import DownloadService


VTT = """WEBVTT

00:00:00.000 --> 00:00:01.000
Primeira legenda

00:00:01.000 --> 00:00:02.000
Segunda legenda
"""


def _ffmpeg() -> str | None:
    for candidate in ("ffmpeg", "ffprobe"):
        if shutil.which(candidate) is None:
            return None
    return shutil.which("ffmpeg")


pytestmark = pytest.mark.skipif(_ffmpeg() is None, reason="FFmpeg indisponível")


class _Handler(SimpleHTTPRequestHandler):
    """Serve o media com suporte a ``Range`` (como um CDN real)."""

    media: bytes = b""

    def log_message(self, *args):  # noqa: D102
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.media)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/subs.vtt"):
            body = VTT.encode("utf-8")
            content_type = "text/vtt"
        elif self.path.startswith("/video.mp4"):
            body = self.media
            content_type = "video/mp4"
        else:
            self.send_error(404)
            return

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            chunk = body[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(chunk)
            return

        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def media_file(tmp_path_factory) -> bytes:
    """Gera um MP4 curto com vídeo e áudio usando o FFmpeg."""
    target = tmp_path_factory.mktemp("media") / "video.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(target),
        ],
        check=True,
    )
    return target.read_bytes()


@pytest.fixture
def server(media_file):
    _Handler.media = media_file
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


class _FakeExtraction:
    """Substitui o extractor: devolve sempre a mesma `MediaInfo`."""

    def __init__(self, info: MediaInfo) -> None:
        self.info = info

    async def extract(self, url, headers=None, cookies=None, use_cache=True):
        return self.info


def _info(base: str) -> MediaInfo:
    return MediaInfo(
        extractor="fake",
        webpage_url=f"{base}/video.mp4",
        original_url=f"{base}/video.mp4",
        title="Vídeo de Teste",
        duration=2.0,
        formats=[
            StreamFormat(
                id="v1",
                url=f"{base}/video.mp4",
                media_type=MediaType.VIDEO,
                container=Container.MP4,
                height=240,
                vcodec="avc1",
                acodec="mp4a",
                tbr=800,
                filesize=1000,
            )
        ],
        subtitles={
            "pt": [
                SubtitleTrack(
                    language="pt",
                    name="Português",
                    ext="vtt",
                    url=f"{base}/subs.vtt",
                    automatic=False,
                )
            ]
        },
    )


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            shutil.which("ffprobe"),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    return json.loads(result.stdout)


@pytest.mark.asyncio
async def test_download_with_subtitles_muxes_the_track(settings, server) -> None:
    """O ficheiro final tem vídeo, áudio e a faixa de legenda."""
    jobs = JobManager()
    service = DownloadService(settings, jobs, _FakeExtraction(_info(server)))
    job = await jobs.create(f"{server}/video.mp4")

    request = DownloadRequest(
        url=f"{server}/video.mp4",
        mode=DownloadMode.AUTO,
        subtitle_lang="auto",
    )

    result = await service.execute(job, request)

    output = Path(result.path)
    assert output.exists(), "o ficheiro final tem de existir"
    assert result.warnings == [], f"não deve haver avisos: {result.warnings}"

    streams = _probe(output)["streams"]
    kinds = [s["codec_type"] for s in streams]
    assert "video" in kinds
    assert "audio" in kinds
    assert "subtitle" in kinds, f"a legenda não foi anexada: {kinds}"

    subtitle = next(s for s in streams if s["codec_type"] == "subtitle")
    assert subtitle.get("codec_name") == "mov_text", "MP4 usa mov_text"


@pytest.mark.asyncio
async def test_download_without_subtitles_has_no_subtitle_stream(settings, server) -> None:
    """Sem pedido de legendas, o ficheiro não tem pista de texto."""
    jobs = JobManager()
    service = DownloadService(settings, jobs, _FakeExtraction(_info(server)))
    job = await jobs.create(f"{server}/video.mp4")

    request = DownloadRequest(
        url=f"{server}/video.mp4",
        mode=DownloadMode.AUTO,
        subtitle_lang=None,
    )

    result = await service.execute(job, request)

    streams = _probe(Path(result.path))["streams"]
    kinds = [s["codec_type"] for s in streams]
    assert "subtitle" not in kinds

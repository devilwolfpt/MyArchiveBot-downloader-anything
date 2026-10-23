"""Testes do pós-processamento: detecção de fMP4, remux, merge e áudio.

O YouTube serve os formatos *adaptativos* como MP4 fragmentado (fMP4).
Muitos players não o reproduzem, por isso o pipeline tem de o converter
num MP4 progressivo. Estes testes usam ficheiros gerados pelo proprio
FFmpeg (sem depender da rede).
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.models import DownloadMode
from app.core.postprocess.ffmpeg import FFmpegResult, FFmpegWrapper
from app.core.postprocess.mp4 import is_fragmented_mp4
from app.core.postprocess.processor import FRAGMENTED_WARNING, PostProcessor

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="FFmpeg nao instalado")
requires_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe nao instalado")


def _ffmpeg(*args: str) -> None:
    proc = subprocess.run(
        [FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"ffmpeg falhou: {proc.stderr[-800:]}"


def _duration(path: Path) -> float:
    proc = subprocess.run(
        [
            FFPROBE or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def _stream_types(path: Path) -> list[str]:
    proc = subprocess.run(
        [
            FFPROBE or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def progressive_mp4(tmp_path: Path) -> Path:
    """MP4 progressivo normal (tabelas de amostras preenchidas)."""
    path = tmp_path / "video.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=320x240:rate=15",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(path),
    )
    return path


@pytest.fixture
def fragmented_mp4(tmp_path: Path) -> Path:
    """MP4 fragmentado, tal como o YouTube serve os formatos adaptativos."""
    path = tmp_path / "video_fmp4.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=320x240:rate=15",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        str(path),
    )
    return path


@pytest.fixture
def audio_m4a(tmp_path: Path) -> Path:
    path = tmp_path / "audio.m4a"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(path))
    return path


@pytest.fixture
def av_mp4(tmp_path: Path) -> Path:
    """MP4 com video E audio."""
    path = tmp_path / "video_av.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    )
    return path


class _StubFFmpeg(FFmpegWrapper):
    """FFmpeg falso: regista as chamadas sem executar o binário."""

    def __init__(self, settings, *, available: bool = True) -> None:
        super().__init__(settings)
        self._available = available
        self.calls: list[tuple] = []

    def is_available(self) -> bool:
        return self._available

    async def merge(self, video, audio, output, *, container="mp4", reencode=False, audio_codec=None):
        self.calls.append(("merge", video, audio, output))
        Path(output).write_bytes(b"merged")
        return FFmpegResult(path=Path(output), command=[], stderr="")

    async def remux(self, source, output, *, container="mp4"):
        self.calls.append(("remux", source, output))
        Path(output).write_bytes(b"remuxed")
        return FFmpegResult(path=Path(output), command=[], stderr="")

    async def extract_audio(self, source, output, *, audio_format="mp3", bitrate="192k"):
        self.calls.append(("extract_audio", source, output))
        Path(output).write_bytes(b"audio")
        return FFmpegResult(path=Path(output), command=[], stderr="")


# --------------------------------------------------------------------------- #
# Detecção de MP4 fragmentado
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_detects_fragmented_mp4(fragmented_mp4: Path) -> None:
    assert is_fragmented_mp4(fragmented_mp4) is True


@requires_ffmpeg
def test_progressive_mp4_is_not_fragmented(progressive_mp4: Path) -> None:
    assert is_fragmented_mp4(progressive_mp4) is False


def test_detects_fragmented_mp4_without_file(tmp_path: Path) -> None:
    assert is_fragmented_mp4(tmp_path / "nao_existe.mp4") is False


def test_detects_fragmented_mp4_on_garbage(tmp_path: Path) -> None:
    path = tmp_path / "lixo.mp4"
    path.write_bytes(b"isto nao e um mp4" * 100)
    assert is_fragmented_mp4(path) is False


# --------------------------------------------------------------------------- #
# Remux de fMP4 -> MP4 progressivo
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_remux_converts_fragmented_mp4(settings, fragmented_mp4: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.VIDEO,
            video_path=fragmented_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
        )
    )

    assert result.path == out
    assert out.exists()
    assert is_fragmented_mp4(out) is False, "o resultado deve ser um MP4 progressivo"
    assert not fragmented_mp4.exists(), "o ficheiro fragmentado deve ser limpo"
    assert result.warnings == []


@requires_ffmpeg
@requires_ffprobe
def test_remux_preserves_duration(settings, fragmented_mp4: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)
    before = _duration(fragmented_mp4)

    asyncio.run(
        processor.process(
            mode=DownloadMode.VIDEO,
            video_path=fragmented_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
        )
    )

    assert abs(_duration(out) - before) < 0.5


@requires_ffmpeg
def test_progressive_mp4_is_delivered_untouched(settings, progressive_mp4: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.VIDEO,
            video_path=progressive_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
        )
    )

    assert result.path == progressive_mp4
    assert not out.exists()


def test_fragmented_mp4_without_ffmpeg_warns(settings, fragmented_mp4: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)
    processor.ffmpeg = _StubFFmpeg(settings, available=False)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.VIDEO,
            video_path=fragmented_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
        )
    )

    assert result.path == fragmented_mp4
    assert FRAGMENTED_WARNING in result.warnings


# --------------------------------------------------------------------------- #
# Merge de video + audio
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@requires_ffprobe
def test_merge_produces_single_file_with_both_streams(
    settings, fragmented_mp4: Path, audio_m4a: Path, tmp_path: Path
) -> None:
    out = tmp_path / "merged.mp4"
    processor = PostProcessor(settings)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.BOTH,
            video_path=fragmented_mp4,
            audio_path=audio_m4a,
            output_path=out,
            container="mp4",
        )
    )

    assert result.merged is True
    assert result.path == out
    assert out.exists()
    assert _stream_types(out) == ["video", "audio"]
    assert not fragmented_mp4.exists()
    assert not audio_m4a.exists()


def test_merge_is_actually_executed(settings, tmp_path: Path) -> None:
    """Regressao: o merge corria como corrotina nunca aguardada.

    O `PostProcessor.process` era sincrono e chamava `self.ffmpeg.merge(...)`
    (async) sem `await` -> a fusao nunca acontecia e o ficheiro final nao
    existia. Este teste garante que a chamada e executada de facto.
    """
    video = tmp_path / "video.mp4"
    video.write_bytes(b"v")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"a")
    out = tmp_path / "merged.mp4"

    processor = PostProcessor(settings)
    stub = _StubFFmpeg(settings)
    processor.ffmpeg = stub

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.BOTH,
            video_path=video,
            audio_path=audio,
            output_path=out,
            container="mp4",
        )
    )

    assert [call[0] for call in stub.calls] == ["merge"]
    assert result.path == out
    assert out.exists()
    assert out.read_bytes() == b"merged"


def test_merge_without_ffmpeg_keeps_video_and_warns(settings, tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"v")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"a")
    out = tmp_path / "merged.mp4"

    processor = PostProcessor(settings)
    processor.ffmpeg = _StubFFmpeg(settings, available=False)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.BOTH,
            video_path=video,
            audio_path=audio,
            output_path=out,
            container="mp4",
        )
    )

    assert result.path == video
    assert result.warnings
    assert not result.merged


# --------------------------------------------------------------------------- #
# Extraccao de audio
# --------------------------------------------------------------------------- #
@requires_ffmpeg
def test_extract_audio_produces_mp3(settings, av_mp4: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.AUDIO,
            video_path=av_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
            audio_format="mp3",
        )
    )

    assert result.path.suffix == ".mp3"
    assert result.path.exists()
    assert result.mime_type == "audio/mpeg"
    assert result.warnings == []


def test_extract_audio_without_audio_track_falls_back(settings, progressive_mp4: Path, tmp_path: Path) -> None:
    """Um video sem audio nao pode virar MP3: degrada em vez de rebentar."""
    out = tmp_path / "final.mp4"
    processor = PostProcessor(settings)
    processor.ffmpeg = _StubFFmpeg(settings, available=False)

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.AUDIO,
            video_path=progressive_mp4,
            audio_path=None,
            output_path=out,
            container="mp4",
            audio_format="mp3",
        )
    )

    assert result.path == progressive_mp4
    assert result.warnings


def test_extract_audio_is_actually_executed(settings, tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"v")
    out = tmp_path / "final.mp4"

    processor = PostProcessor(settings)
    stub = _StubFFmpeg(settings)
    processor.ffmpeg = stub

    result = asyncio.run(
        processor.process(
            mode=DownloadMode.AUDIO,
            video_path=video,
            audio_path=None,
            output_path=out,
            container="mp4",
            audio_format="mp3",
        )
    )

    assert [call[0] for call in stub.calls] == ["extract_audio"]
    assert result.path == out.with_suffix(".mp3")


# --------------------------------------------------------------------------- #
# Degradacao sem post-processing
# --------------------------------------------------------------------------- #
def test_postprocess_disabled_returns_original(settings, tmp_path: Path) -> None:
    settings.enable_postprocess = False
    video = tmp_path / "video.mp4"
    video.write_bytes(b"v")
    out = tmp_path / "final.mp4"

    processor = PostProcessor(settings)
    result = asyncio.run(
        processor.process(
            mode=DownloadMode.BOTH,
            video_path=video,
            audio_path=None,
            output_path=out,
            container="mp4",
        )
    )

    assert result.path == video
    assert not out.exists()

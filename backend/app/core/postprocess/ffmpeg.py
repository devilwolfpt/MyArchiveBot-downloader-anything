"""Wrapper do FFmpeg: merge de áudio+vídeo, remux e extracção de áudio.

Todas as operações são assíncronas (subprocess) e capturam stderr para
diagnóstico. Se o FFmpeg não estiver instalado, o pipeline degrada com
clareza: ficheiros TS/MP4 separados são entregues tal como estão.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ...config import Settings
from ..exceptions import PostProcessError

log = logging.getLogger(__name__)

#: Codecs por container de saída.
CONTAINER_CODECS: dict[str, dict[str, str]] = {
    "mp4": {"video": "libx264", "audio": "aac", "extra": ["-movflags", "+faststart"]},
    "mkv": {"video": "libx264", "audio": "aac", "extra": []},
    "webm": {"video": "libvpx-vp9", "audio": "libopus", "extra": []},
}

AUDIO_CODECS: dict[str, dict[str, str]] = {
    "mp3": {"codec": "libmp3lame", "extra": ["-q:a", "2"]},
    "m4a": {"codec": "aac", "extra": ["-b:a", "192k"]},
    "opus": {"codec": "libopus", "extra": ["-b:a", "160k"]},
    "wav": {"codec": "pcm_s16le", "extra": []},
}

#: Codec de texto por container de saída (``None`` = não suportado).
SUBTITLE_CODECS: dict[str, str | None] = {
    "mp4": "mov_text",
    "m4v": "mov_text",
    "mov": "mov_text",
    "mkv": "srt",
    "webm": None,  # WebM não suporta legendas de texto
    "ts": "srt",
}


@dataclass(slots=True)
class FFmpegResult:
    path: Path
    command: list[str]
    stderr: str


class FFmpegWrapper:
    """Interface fina sobre o binário do FFmpeg."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ffmpeg = settings.ffmpeg_path
        self.ffprobe = settings.ffprobe_path

    # ------------------------------------------------------------------ #
    def is_available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None or Path(self.ffmpeg).exists()

    async def _run(self, args: list[str], *, timeout: float | None = None) -> FFmpegResult:
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
        log.debug("Executando: %s", " ".join(command))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PostProcessError(
                f"FFmpeg não encontrado: {self.ffmpeg}",
                details={"hint": "Instale o ffmpeg ou defina MAD_FFMPEG_PATH"},
            ) from exc

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.settings.ffmpeg_timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise PostProcessError(f"FFmpeg excedeu o timeout ({timeout}s)") from exc

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise PostProcessError(
                f"FFmpeg falhou (código {process.returncode})",
                details={"stderr": stderr_text[-2000:], "command": " ".join(command)},
            )
        return FFmpegResult(path=Path(args[-1]), command=command, stderr=stderr_text)

    # ------------------------------------------------------------------ #
    async def merge(
        self,
        video: Path,
        audio: Path,
        output: Path,
        *,
        container: str = "mp4",
        reencode: bool = False,
        audio_codec: str | None = None,
    ) -> FFmpegResult:
        """Funde vídeo + áudio num único ficheiro.

        ``audio_codec`` força a transcodificação **apenas do áudio**
        (ex.: ``aac`` quando a origem é Opus e o container é MP4, que nem
        todos os players suportam).
        """
        codecs = CONTAINER_CODECS.get(container, CONTAINER_CODECS["mp4"])

        if reencode:
            args = [
                "-i", str(video),
                "-i", str(audio),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", codecs["video"],
                "-c:a", codecs["audio"],
                "-pix_fmt", "yuv420p",
                *codecs["extra"],
                str(output),
            ]
        else:
            # Fast path: copia streams (streams de rede/TS nem sempre são
            # compatíveis com o container de destino -> nesse caso re-encode).
            args = [
                "-i", str(video),
                "-i", str(audio),
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
            if audio_codec:
                args += ["-c:v", "copy", "-c:a", audio_codec]
            else:
                args += ["-c", "copy"]
            args += [*codecs["extra"], str(output)]

        try:
            return await self._run(args)
        except PostProcessError:
            if reencode:
                raise
            log.info("Merge sem re-encode falhou; a tentar com re-encode")
            return await self.merge(video, audio, output, container=container, reencode=True)

    async def remux(self, source: Path, output: Path, *, container: str = "mp4") -> FFmpegResult:
        """Remux (sem re-encode) de um ficheiro."""
        codecs = CONTAINER_CODECS.get(container, CONTAINER_CODECS["mp4"])
        args = [
            "-i", str(source),
            "-c", "copy",
            *codecs["extra"],
            str(output),
        ]
        return await self._run(args)

    async def extract_audio(
        self,
        source: Path,
        output: Path,
        *,
        audio_format: str = "mp3",
        bitrate: str = "192k",
    ) -> FFmpegResult:
        """Extrai áudio (ex.: MP3) de um ficheiro de vídeo."""
        codec = AUDIO_CODECS.get(audio_format, AUDIO_CODECS["mp3"])
        args = ["-i", str(source), "-vn", "-c:a", codec["codec"], *codec["extra"], str(output)]
        return await self._run(args)

    async def mux_subtitles(
        self,
        source: Path,
        output: Path,
        subtitles: list[tuple[Path, str]],
        *,
        container: str = "mp4",
    ) -> FFmpegResult:
        """Anexa faixas de legenda ao ficheiro final.

        O vídeo e o áudio são copiados sem recodificar (``-c copy``); só as
        legendas são convertidas para o codec de texto do container
        (``mov_text`` em MP4, ``srt`` em MKV).

        ``subtitles`` é uma lista de pares ``(ficheiro, idioma)``.
        """
        if not subtitles:
            raise PostProcessError("Nenhuma faixa de legenda para anexar")

        codec = SUBTITLE_CODECS.get(container, "srt")
        if codec is None:
            raise PostProcessError(
                f"O container '{container}' não suporta legendas",
                details={"containers_com_legendas": sorted(SUBTITLE_CODECS)},
            )

        args: list[str] = ["-i", str(source)]
        for path, _lang in subtitles:
            args += ["-i", str(path)]

        args += ["-map", "0"]
        for index in range(1, len(subtitles) + 1):
            args += ["-map", f"{index}"]

        args += ["-c", "copy", "-c:s", codec]
        for index, (_path, lang) in enumerate(subtitles):
            if lang:
                args += [f"-metadata:s:s:{index}", f"language={lang}"]
        args += [*CONTAINER_CODECS.get(container, {}).get("extra", []), str(output)]

        try:
            return await self._run(args)
        except PostProcessError:
            if len(subtitles) > 1:
                # Alguns containers aceitam apenas uma faixa de texto.
                log.info("Mux com várias legendas falhou; a tentar com uma só")
                return await self.mux_subtitles(
                    source, output, subtitles[:1], container=container
                )
            raise

    async def probe(self, path: Path) -> dict:
        """Metadados do ficheiro via ffprobe."""
        if shutil.which(self.ffprobe) is None and not Path(self.ffprobe).exists():
            return {}
        command = [
            self.ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
        except OSError:
            return {}
        if process.returncode != 0:
            return {}
        import json

        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except ValueError:
            return {}

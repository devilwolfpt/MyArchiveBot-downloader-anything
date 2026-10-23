"""Pós-processamento: orchestração de merge/remux/extracção de áudio."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ...config import Settings
from ..exceptions import PostProcessError
from ..models import DownloadMode
from .ffmpeg import FFmpegWrapper
from .mp4 import is_fragmented_mp4

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PostProcessOutput:
    path: Path
    mime_type: str
    container: str
    merged: bool = False
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


MIME_TYPES = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "ts": "video/mp2t",
}

#: Aviso mostrado quando um MP4 fragmentado é entregue sem converter.
FRAGMENTED_WARNING = (
    "O ficheiro é um MP4 fragmentado (formato do YouTube). Alguns players não o "
    "reproduzem — instale o FFmpeg para o converter automaticamente."
)


class PostProcessor:
    """Executa o pós-processamento conforme o modo pedido."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ffmpeg = FFmpegWrapper(settings)

    # ------------------------------------------------------------------ #
    async def process(
        self,
        *,
        mode: DownloadMode,
        video_path: Path | None,
        audio_path: Path | None,
        output_path: Path,
        container: str = "mp4",
        audio_format: str = "mp3",
        extract_audio: bool = False,
        audio_codec: str | None = None,
        subtitles: list[tuple[Path, str]] | None = None,
    ) -> PostProcessOutput:
        """Produz o ficheiro final a partir dos ficheiros descarregados.

        ``output_path`` é o caminho **final** (com extensão) e nunca deve
        coincidir com ``video_path``/``audio_path``.

        ``subtitles`` é uma lista de pares ``(ficheiro_srt, idioma)`` que
        são anexados ao ficheiro final. Uma falha ao anexar legendas nunca
        faz falhar o download — o ficheiro é entregue sem elas e o motivo
        fica nos avisos.

        É assíncrono porque delega no FFmpeg (subprocesso).
        """
        warnings: list[str] = []
        subtitles = list(subtitles or [])

        if not self.settings.enable_postprocess:
            source = video_path or audio_path
            if source is None:
                raise PostProcessError("Nada para processar")
            return PostProcessOutput(
                path=source,
                mime_type=MIME_TYPES.get(
                    source.suffix.lstrip(".").lower(), "application/octet-stream"
                ),
                container=source.suffix.lstrip(".").lower() or "bin",
                warnings=warnings,
            )

        if mode is DownloadMode.AUDIO or extract_audio:
            source = audio_path or video_path
            if source is None:
                raise PostProcessError("Não há ficheiro de áudio para extrair")
            if not self.ffmpeg.is_available():
                warnings.append("FFmpeg indisponível: ficheiro original entregue")
                return PostProcessOutput(
                    path=source,
                    mime_type=MIME_TYPES.get(
                        source.suffix.lstrip(".").lower(), "application/octet-stream"
                    ),
                    container=source.suffix.lstrip(".").lower() or "bin",
                    warnings=warnings,
                )
            target = output_path.with_suffix(f".{audio_format}")
            try:
                await self.ffmpeg.extract_audio(source, target, audio_format=audio_format)
            except PostProcessError as exc:
                # Ex.: o ficheiro de origem nao tem pista de audio.
                warnings.append(
                    f"Extracao de audio falhou ({exc.message}); entregue o original"
                )
                return PostProcessOutput(
                    path=source,
                    mime_type=MIME_TYPES.get(
                        source.suffix.lstrip(".").lower(), "application/octet-stream"
                    ),
                    container=source.suffix.lstrip(".").lower() or "bin",
                    warnings=warnings,
                )
            return PostProcessOutput(
                path=target,
                mime_type=MIME_TYPES.get(audio_format, "application/octet-stream"),
                container=audio_format,
                warnings=warnings,
            )

        # Vídeo + áudio separados -> funde num único ficheiro. Isto acontece
        # em `mode=both` e também em `mode=video` quando o formato escolhido
        # é video-only e o pipeline anexou a melhor faixa de áudio.
        if video_path and audio_path:
            if not self.ffmpeg.is_available():
                warnings.append(
                    "FFmpeg indisponível: vídeo e áudio entregues em ficheiros separados"
                )
                return PostProcessOutput(
                    path=video_path,
                    mime_type="video/mp4",
                    container=video_path.suffix.lstrip(".") or "mp4",
                    warnings=warnings,
                )
            target = output_path
            await self.ffmpeg.merge(
                video_path,
                audio_path,
                target,
                container=container,
                audio_codec=audio_codec,
            )
            self._cleanup(video_path, audio_path, keep=target)
            target = await self._attach_subtitles(
                target, subtitles, container=container, warnings=warnings
            )
            return PostProcessOutput(
                path=target,
                mime_type=MIME_TYPES.get(container, "video/mp4"),
                container=container,
                merged=True,
                warnings=warnings,
            )

        source = video_path or audio_path
        if source is None:
            raise PostProcessError("Nada para processar")

        suffix = source.suffix.lstrip(".").lower()
        needs_remux = suffix == "ts" or (
            suffix == "mp4"
            and container in ("mp4", "mkv")
            and is_fragmented_mp4(source)
        )

        target = source
        if needs_remux:
            if self.ffmpeg.is_available():
                target = output_path
                try:
                    await self.ffmpeg.remux(source, target, container=container)
                    self._cleanup(source, keep=target)
                except PostProcessError as exc:
                    warnings.append(f"Remux falhou ({exc.message}); entregue o original")
                    target = source
            elif suffix == "mp4":
                warnings.append(FRAGMENTED_WARNING)
            else:
                warnings.append("FFmpeg indisponível: entregue o ficheiro TS original")

        target = await self._attach_subtitles(
            target, subtitles, container=container, warnings=warnings
        )

        return PostProcessOutput(
            path=target,
            mime_type=MIME_TYPES.get(target.suffix.lstrip(".").lower(), "application/octet-stream"),
            container=target.suffix.lstrip(".").lower() or "bin",
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    async def _attach_subtitles(
        self,
        source: Path,
        subtitles: list[tuple[Path, str]],
        *,
        container: str,
        warnings: list[str],
    ) -> Path:
        """Anexa as faixas de legenda ao ficheiro final.

        Devolve o caminho do ficheiro resultante. Se a operação falhar, o
        ficheiro original é entregue e o motivo fica nos avisos — uma
        legenda em falta nunca invalida o download.
        """
        if not subtitles:
            return source
        if not self.ffmpeg.is_available():
            warnings.append("FFmpeg indisponível: ficheiro entregue sem legendas")
            return source

        temp = source.with_name(f"{source.stem}.subs{source.suffix}")
        try:
            await self.ffmpeg.mux_subtitles(source, temp, subtitles, container=container)
        except PostProcessError as exc:
            warnings.append(f"Não foi possível anexar as legendas ({exc.message})")
            temp.unlink(missing_ok=True)
            return source

        self._cleanup(source, keep=temp)
        return temp

    # ------------------------------------------------------------------ #
    @staticmethod
    def _cleanup(*paths: Path | None, keep: Path) -> None:
        """Remove os ficheiros intermédios, mantendo o resultado final."""
        for path in paths:
            if path is None:
                continue
            if path == keep or not path.exists():
                continue
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover
                log.debug("Não foi possível remover %s: %s", path, exc)

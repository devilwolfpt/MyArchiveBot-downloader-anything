"""Descarga e normalização de faixas de legenda.

Os extractors devolvem as legendas em formatos heterogéneos: WebVTT,
SubRip, TTML ou o JSON3 proprietário do YouTube. Este módulo normaliza
tudo para **SRT** (SubRip), que o FFmpeg lê em qualquer container.

O mux final é feito numa única passagem pelo FFmpeg (``-c:s mov_text`` em
MP4, ``-c:s srt`` em MKV) — o vídeo não é transcodificado.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..http.client import HttpClient
from ..models import SubtitleTrack

log = logging.getLogger(__name__)

#: Cabeçalho de um ficheiro WebVTT.
VTT_MAGIC = "WEBVTT"

#: Expressão de timestamp WebVTT (``00:01:02.500``) ou SRT (``00:01:02,500``).
TIMESTAMP_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{1,3})"
)
CUE_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})"
)
#: Versão sem âncora de início de linha — usada só para *detectar* o
#: formato (no SRT a legenda vem depois do número, na linha seguinte).
CUE_ANY_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}"
)


class SubtitleError(Exception):
    """Falha ao obter ou converter uma faixa de legenda."""


# --------------------------------------------------------------------------- #
# Conversões
# --------------------------------------------------------------------------- #
def _format_timestamp(total_ms: int) -> str:
    """Formata milissegundos como ``HH:MM:SS,mmm`` (formato SRT)."""
    total_ms = max(0, int(total_ms))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _parse_timestamp(value: str) -> int:
    """Converte um timestamp VTT/SRT para milissegundos."""
    match = TIMESTAMP_RE.search(value)
    if not match:
        raise SubtitleError(f"Timestamp inválido: {value!r}")
    millis = match.group("ms").ljust(3, "0")
    return (
        int(match.group("h")) * 3_600_000
        + int(match.group("m")) * 60_000
        + int(match.group("s")) * 1000
        + int(millis)
    )


def vtt_to_srt(text: str) -> str:
    """Converte WebVTT em SubRip.

    O WebVTT permite blocos sem tempo (``NOTE``, Legendas posicionadas,
    etc.) — são simplesmente ignorados.
    """
    blocks: list[str] = []
    index = 1

    for raw_block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [line for line in raw_block.split("\n") if line.strip() != ""]
        if not lines:
            continue

        # Remove o cabeçalho WEBVTT e metadados (NOTA, STYLE, REGION).
        if lines[0].strip().upper().startswith("WEBVTT"):
            lines = lines[1:]
            if not lines:
                continue
        if lines[0].strip().upper().startswith(("NOTE", "STYLE", "REGION")):
            continue

        cue = None
        for pos, line in enumerate(lines):
            match = CUE_RE.match(line)
            if match:
                cue = (match.group("start"), match.group("end"), lines[pos + 1 :])
                break
        if cue is None:
            continue

        start, end, payload = cue
        text_lines = [line.strip() for line in payload]
        text_lines = [line for line in text_lines if line != ""]
        if not text_lines:
            continue

        blocks.append(
            f"{index}\n"
            f"{_format_timestamp(_parse_timestamp(start))} --> "
            f"{_format_timestamp(_parse_timestamp(end))}\n"
            + "\n".join(text_lines)
        )
        index += 1

    if not blocks:
        raise SubtitleError("A faixa de legenda não contém nenhum evento")
    return "\n\n".join(blocks) + "\n"


def json3_to_srt(text: str) -> str:
    """Converte o JSON3 do YouTube em SubRip."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SubtitleError("JSON3 inválido") from exc

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise SubtitleError("JSON3 sem eventos")

    blocks: list[str] = []
    index = 1
    for event in events:
        if not isinstance(event, dict):
            continue
        # ``aAppend`` serve só para inserir quebras de linha.
        if event.get("aAppend"):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        caption = "".join(
            seg.get("utf8", "") for seg in segments if isinstance(seg, dict)
        ).strip()
        if not caption:
            continue
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue
        duration_ms = int(event.get("dDurationMs") or 0)
        end_ms = int(start_ms) + duration_ms
        blocks.append(
            f"{index}\n"
            f"{_format_timestamp(int(start_ms))} --> {_format_timestamp(end_ms)}\n"
            f"{caption}"
        )
        index += 1

    if not blocks:
        raise SubtitleError("A faixa de legenda não contém nenhum evento")
    return "\n\n".join(blocks) + "\n"


def detect_subtitle_format(payload: bytes) -> str:
    """Identifica o formato a partir do conteúdo (``vtt``/``srt``/``json3``)."""
    head = payload[:512].lstrip()
    if head.startswith(b"WEBVTT"):
        return "vtt"
    if head.startswith(b"{") or head.startswith(b"["):
        return "json3"
    if head.startswith(b"<?xml") or head.startswith(b"<tt"):
        return "ttml"
    if CUE_ANY_RE.search(payload[:4096].decode("utf-8", errors="replace")):
        return "srt"
    return "unknown"


def to_srt(payload: bytes, *, fmt: str | None = None) -> str:
    """Normaliza o conteúdo de uma faixa de legenda para SRT."""
    fmt = fmt or detect_subtitle_format(payload)
    text = payload.decode("utf-8", errors="replace")

    if fmt == "json3":
        return json3_to_srt(text)
    if fmt == "vtt":
        return vtt_to_srt(text)
    if fmt == "srt":
        return text
    if fmt == "ttml":
        raise SubtitleError(
            "Legendas TTML não suportadas (o FFmpeg converte-as, se disponível)"
        )
    raise SubtitleError(f"Formato de legenda desconhecido: {fmt}")


# --------------------------------------------------------------------------- #
# Downloader
# --------------------------------------------------------------------------- #
class SubtitleDownloader:
    """Descarrega faixas de legenda e converte-as para SRT."""

    def __init__(self, http: HttpClient, workdir: Path) -> None:
        self.http = http
        self.workdir = workdir

    async def fetch(
        self,
        track: SubtitleTrack,
        *,
        name_hint: str = "legenda",
        headers: dict[str, str] | None = None,
    ) -> Path | None:
        """Descarrega a faixa e devolve o caminho do ficheiro SRT.

        Devolve ``None`` (e regista um aviso) se a faixa não estiver
        disponível — uma legenda em falta nunca deve fazer falhar o
        download do vídeo.
        """
        if not track.url:
            log.info("Faixa de legenda '%s' não tem URL", track.language)
            return None

        try:
            result = await self.http.get(track.url, headers=headers, binary=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao descarregar a legenda '%s': %s", track.language, exc)
            return None

        payload = result.content or b""
        if not payload.strip():
            log.warning("Legenda '%s' veio vazia", track.language)
            return None

        # O formato real é deduzido do conteúdo (mais fiável do que a
        # extensão declarada pelo extractor).
        fmt = detect_subtitle_format(payload)
        if fmt == "unknown":
            fmt = track.ext if track.ext in ("vtt", "srt", "json3") else None

        try:
            srt = to_srt(payload, fmt=fmt)
        except SubtitleError as exc:
            log.warning("Não foi possível converter a legenda '%s': %s", track.language, exc)
            return None

        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name_hint) or "legenda"
        target = self.workdir / f"{safe}.{track.language or 'und'}.srt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(srt, encoding="utf-8")
        log.info("Legenda '%s' guardada em %s", track.language, target)
        return target

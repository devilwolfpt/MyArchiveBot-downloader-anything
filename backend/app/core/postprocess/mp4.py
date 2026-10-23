"""Inspecção de containers ISO-BMFF (MP4) sem depender do FFmpeg.

O YouTube serve os formatos *adaptativos* (vídeo-only / áudio-only) como
**MP4 fragmentado** (fMP4): o ``moov`` inicial contém uma box ``mvex`` e o
media vive em blocos ``moof`` + ``mdat``, com as tabelas de amostras
(``stts``/``stsz``/``stco``) praticamente vazias.

O FFmpeg e o VLC reproduzem fMP4, mas muitos players (Windows Media Player,
alguns players de hardware, webviews sem MSE) mostram um vídeo de 0 segundos
sem imagem nem som. Por isso é útil saber se o ficheiro precisa de remux.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

#: Tamanho máximo de uma box lida para procurar sub-boxes.
_MAX_BOX_SCAN = 1 << 20  # 1 MiB


def iter_top_level_boxes(path: Path, *, limit: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    """Itera ``(tipo, offset, tamanho)`` das boxes de nível superior.

    Tolera ficheiros truncados: para no primeiro box inválido.
    """
    try:
        total = path.stat().st_size
    except OSError:
        return

    with path.open("rb") as handle:
        pos = 0
        seen = 0
        while pos + 8 <= total:
            handle.seek(pos)
            header = handle.read(8)
            if len(header) < 8:
                break
            box_size = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            header_len = 8
            if box_size == 1:
                ext = handle.read(8)
                if len(ext) < 8:
                    break
                box_size = int.from_bytes(ext, "big")
                header_len = 16
            elif box_size == 0:
                # box ate ao fim do ficheiro
                box_size = total - pos
            if box_size < header_len or pos + box_size > total:
                break
            yield box_type, pos, box_size
            seen += 1
            if limit is not None and seen >= limit:
                return
            pos += box_size


def is_fragmented_mp4(path: Path) -> bool:
    """Indica se o ficheiro é um MP4 fragmentado (fMP4).

    Um fMP4 tem uma box ``moof`` ao nível superior (cada fragmento de media)
    ou uma box ``mvex`` dentro do ``moov`` (que declara os timescales).
    """
    try:
        for box_type, offset, box_size in iter_top_level_boxes(path):
            if box_type == b"moof":
                return True
            if box_type == b"moov":
                try:
                    with path.open("rb") as handle:
                        handle.seek(offset + 8)
                        payload = handle.read(min(box_size - 8, _MAX_BOX_SCAN))
                except OSError:
                    continue
                if b"mvex" in payload:
                    return True
    except OSError as exc:  # pragma: no cover - ficheiro inacessivel
        log.debug("Nao foi possivel inspecionar %s: %s", path, exc)
    return False


def has_audio_track(path: Path) -> bool:
    """Indica se o MP4 contem uma pista de audio (``hdlr`` com 'soun')."""
    for box_type, offset, box_size in iter_top_level_boxes(path):
        if box_type != b"moov":
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(offset + 8)
                payload = handle.read(min(box_size - 8, _MAX_BOX_SCAN))
        except OSError:
            return False
        return b"soun" in payload
    return False

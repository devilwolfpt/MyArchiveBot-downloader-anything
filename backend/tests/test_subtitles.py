"""Testes da descarga e conversão de faixas de legenda.

O formato final de todas as faixas é **SRT** (SubRip), que o FFmpeg lê
em qualquer container. São testados os três formatos de origem mais
comuns: WebVTT (YouTube), JSON3 (YouTube legado) e SubRip.
"""
from __future__ import annotations

import pytest

from app.core.downloader.subtitle import (
    SubtitleError,
    detect_subtitle_format,
    json3_to_srt,
    to_srt,
    vtt_to_srt,
)


# --------------------------------------------------------------------------- #
# WebVTT
# --------------------------------------------------------------------------- #
VTT_SAMPLE = """WEBVTT

00:00:01.000 --> 00:00:04.000
Primeira legenda

00:00:04.500 --> 00:00:07.250
Segunda legenda
com duas linhas
"""


def test_detect_vtt() -> None:
    assert detect_subtitle_format(VTT_SAMPLE.encode("utf-8")) == "vtt"


def test_vtt_to_srt() -> None:
    srt = vtt_to_srt(VTT_SAMPLE)

    assert "00:00:01,000 --> 00:00:04,000" in srt
    assert "Primeira legenda" in srt
    assert "00:00:04,500 --> 00:00:07,250" in srt
    assert "com duas linhas" in srt
    # A numeração dasLegendas é sequencial.
    assert srt.startswith("1\n")


def test_vtt_ignores_notes_and_metadata() -> None:
    source = """WEBVTT
Kind: captions
Language: pt

NOTE
 isto e um comentario

00:00:00.000 --> 00:00:02.000
Ola
"""
    srt = vtt_to_srt(source)
    assert "Ola" in srt
    assert "comentario" not in srt
    assert "Language" not in srt


def test_vtt_without_cues_raises() -> None:
    with pytest.raises(SubtitleError):
        vtt_to_srt("WEBVTT\n\n")


# --------------------------------------------------------------------------- #
# JSON3 (YouTube)
# --------------------------------------------------------------------------- #
JSON3_SAMPLE = """{
  "events": [
    {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "Ola "}, {"utf8": "mundo"}]},
    {"tStartMs": 1500, "dDurationMs": 2000, "segs": [{"utf8": "Segunda"}]},
    {"aAppend": 1, "segs": [{"utf8": "\\n"}]},
    {"tStartMs": 3500, "dDurationMs": 0, "segs": []}
  ]
}"""


def test_detect_json3() -> None:
    assert detect_subtitle_format(JSON3_SAMPLE.encode("utf-8")) == "json3"


def test_json3_to_srt() -> None:
    srt = json3_to_srt(JSON3_SAMPLE)

    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert "Ola mundo" in srt
    assert "00:00:01,500 --> 00:00:03,500" in srt
    assert "Segunda" in srt
    # O evento `aAppend` (só quebra de linha) não vira legenda.
    assert srt.count("-->") == 2


def test_json3_invalid_raises() -> None:
    with pytest.raises(SubtitleError):
        json3_to_srt("{isto não é json")


# --------------------------------------------------------------------------- #
# Detecção genérica
# --------------------------------------------------------------------------- #
def test_detect_srt() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,000\nOla\n"
    assert detect_subtitle_format(srt.encode("utf-8")) == "srt"


def test_to_srt_accepts_srt_unchanged() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,000\nOla\n"
    assert to_srt(srt.encode("utf-8")).strip() == srt.strip()


def test_to_srt_detects_the_format() -> None:
    assert "Primeira legenda" in to_srt(VTT_SAMPLE.encode("utf-8"))
    assert "Ola mundo" in to_srt(JSON3_SAMPLE.encode("utf-8"))


def test_to_srt_rejects_unknown_format() -> None:
    with pytest.raises(SubtitleError):
        to_srt(b"blablabla", fmt="desconhecido")

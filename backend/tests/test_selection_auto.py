"""Testes da selecção em modo automático e da escolha de legendas.

Cobrem o contrato novo da API (``mode=auto``, ``video_format_id``,
``audio_format_id``, ``subtitle_lang``) e as regras de merge:

- o ficheiro final tem **uma** faixa de vídeo e, opcionalmente, **uma**
  de áudio;
- vídeo explícito **com** áudio e sem áudio explícito -> sem merge;
- vídeo explícito **sem** áudio -> o melhor áudio é anexado;
- as legendas são escolhidas por id exacto, idioma, ``auto`` ou ``all``.
"""
from __future__ import annotations

from app.core.exceptions import NoFormatFoundError
from app.core.models import (
    Container,
    DownloadMode,
    MediaInfo,
    MediaType,
    StreamFormat,
    SubtitleTrack,
)
from app.services.pipeline import DownloadService


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _video(
    fmt_id: str,
    *,
    height: int = 1080,
    acodec: str | None = None,
    tbr: int = 2000,
    vcodec: str = "avc1.640028",
) -> StreamFormat:
    return StreamFormat(
        id=fmt_id,
        url=f"https://example.com/{fmt_id}",
        media_type=MediaType.VIDEO,
        container=Container.MP4,
        width=height * 16 // 9,
        height=height,
        tbr=tbr,
        acodec=acodec,
        vcodec=vcodec,
    )


def _audio(fmt_id: str, *, abr: int = 128, acodec: str = "mp4a.40.2") -> StreamFormat:
    return StreamFormat(
        id=fmt_id,
        url=f"https://example.com/{fmt_id}",
        media_type=MediaType.AUDIO,
        container=Container.M4A if acodec.startswith("mp4a") else Container.WEBM,
        abr=abr,
        acodec=acodec,
        vcodec="none",
    )


def _info(
    formats: list[StreamFormat],
    *,
    subtitles: dict[str, list[SubtitleTrack]] | None = None,
) -> MediaInfo:
    return MediaInfo(
        webpage_url="https://example.com/watch",
        original_url="https://example.com/watch",
        title="Teste",
        formats=formats,
        subtitles=subtitles or {},
    )


def _service(settings) -> DownloadService:
    return DownloadService(settings, None, None)


def _subtitle(
    language: str,
    name: str | None = None,
    *,
    automatic: bool = False,
    group: str = "subtitles",
) -> SubtitleTrack:
    return SubtitleTrack(
        language=language,
        name=name,
        ext="vtt",
        url=f"https://example.com/{language}-{name or 'default'}.vtt",
        automatic=automatic,
    )


# --------------------------------------------------------------------------- #
# Modo automático
# --------------------------------------------------------------------------- #
def test_auto_mode_picks_best_video_and_merges_audio(settings) -> None:
    """O modo automático escolhe o melhor vídeo e anexa o melhor áudio."""
    info = _info(
        [
            _video("137", height=1080, tbr=4000),
            _video("248", height=2160, tbr=6000),
            _video("18", height=360, acodec="mp4a.40.2", tbr=700),
            _audio("140", abr=128),
            _audio("251", abr=160, acodec="opus"),
        ]
    )
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.AUTO)

    # streams só de vídeo são preferidos, para poder fundir o áudio
    assert selection.video is not None and selection.video.id == "248"
    assert selection.needs_merge is True
    # Em MP4 prefere-se AAC (o Opus dentro de MP4 não é reproduzido por
    # muitos players) — é a mesma regra do backend.
    assert selection.audio is not None and selection.audio.id == "140"
    assert selection.notes, "a selecção automática deve explicar o que escolheu"


def test_auto_mode_without_muxed_video_does_not_merge(settings) -> None:
    """Se o melhor vídeo já tiver som, não há merge."""
    info = _info(
        [
            _video("18", height=1080, acodec="mp4a.40.2", tbr=4000),
            _audio("140", abr=128),
        ]
    )
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.AUTO)

    assert selection.primary.id == "18"
    assert selection.needs_merge is False
    assert selection.audio is None


def test_auto_mode_is_the_default(settings) -> None:
    """Sem modo explícito, o comportamento é o automático."""
    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info)

    assert selection.primary.id == "137"
    assert selection.needs_merge is True


# --------------------------------------------------------------------------- #
# Selectores explicitos
# --------------------------------------------------------------------------- #
def test_explicit_video_with_audio_does_not_merge(settings) -> None:
    """Vídeo explícito **com** áudio e sem áudio explícito -> sem merge."""
    info = _info([_video("18", acodec="mp4a.40.2"), _video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(
        info, video_format_id="18", mode=DownloadMode.AUTO
    )

    assert selection.primary.id == "18"
    assert selection.audio is None
    assert selection.needs_merge is False


def test_explicit_video_without_audio_attaches_best_audio(settings) -> None:
    """Vídeo explícito **sem** áudio -> o melhor áudio é anexado."""
    info = _info([_video("137"), _audio("140", abr=128), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(
        info, video_format_id="137", mode=DownloadMode.AUTO
    )

    assert selection.primary.id == "137"
    assert selection.needs_merge is True
    assert selection.audio is not None and selection.audio.id == "251"


def test_explicit_audio_replaces_the_audio_of_the_video(settings) -> None:
    """Áudio explícito substitui o áudio que o vídeo trazia."""
    info = _info([_video("18", acodec="mp4a.40.2"), _audio("140"), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(
        info,
        video_format_id="18",
        audio_format_id="251",
        mode=DownloadMode.AUTO,
    )

    assert selection.primary.id == "18"
    assert selection.audio is not None and selection.audio.id == "251"
    assert selection.needs_merge is True


def test_unknown_video_format_raises(settings) -> None:
    info = _info([_video("137")])
    service = _service(settings)

    try:
        service.select_formats(info, video_format_id="inexistente")
    except NoFormatFoundError as exc:
        assert "vídeo" in exc.message
    else:  # pragma: no cover
        raise AssertionError("devia levantar NoFormatFoundError")


def test_unknown_audio_format_raises(settings) -> None:
    info = _info([_video("137")])
    service = _service(settings)

    try:
        service.select_formats(info, audio_format_id="inexistente")
    except NoFormatFoundError as exc:
        assert "áudio" in exc.message
    else:  # pragma: no cover
        raise AssertionError("devia levantar NoFormatFoundError")


def test_audio_mode_ignores_video_selectors(settings) -> None:
    """No modo "só áudio" o selector de vídeo é irrelevante."""
    info = _info([_video("137"), _audio("140"), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(
        info, video_format_id="137", mode=DownloadMode.AUDIO
    )

    assert selection.primary.id == "251"
    assert selection.video is None


# --------------------------------------------------------------------------- #
# Legendas
# --------------------------------------------------------------------------- #
def test_no_subtitles_by_default(settings) -> None:
    info = _info([_video("137")], subtitles={"pt": [_subtitle("pt", "Português")]})
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.AUTO)

    assert selection.subtitles == []


def test_subtitle_none_is_ignored(settings) -> None:
    info = _info([_video("137")], subtitles={"pt": [_subtitle("pt", "Português")]})
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="none", mode=DownloadMode.AUTO)

    assert selection.subtitles == []


def test_subtitle_auto_prefers_portuguese(settings) -> None:
    info = _info(
        [_video("137")],
        subtitles={
            "en": [_subtitle("en", "English")],
            "pt": [_subtitle("pt", "Português")],
        },
    )
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="auto", mode=DownloadMode.AUTO)

    assert len(selection.subtitles) == 1
    assert selection.subtitles[0].language == "pt"


def test_subtitle_auto_prefers_manual_over_automatic(settings) -> None:
    info = _info(
        [_video("137")],
        subtitles={
            "pt": [
                _subtitle("pt", "Português (auto-generated)", automatic=True),
                _subtitle("pt", "Português"),
            ],
        },
    )
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="auto", mode=DownloadMode.AUTO)

    assert len(selection.subtitles) == 1
    assert selection.subtitles[0].name == "Português"


def test_subtitle_by_exact_track_id(settings) -> None:
    track = _subtitle("pt", "Português")
    info = _info([_video("137")], subtitles={"pt": [track, _subtitle("en", "English")]})
    service = _service(settings)

    selection = service.select_formats(
        info, subtitle_lang=track.track_id("pt"), mode=DownloadMode.AUTO
    )

    assert len(selection.subtitles) == 1
    assert selection.subtitles[0].language == "pt"
    assert selection.subtitles[0].name == "Português"


def test_subtitle_by_language(settings) -> None:
    info = _info([_video("137")], subtitles={"en": [_subtitle("en", "English")]})
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="en", mode=DownloadMode.AUTO)

    assert len(selection.subtitles) == 1
    assert selection.subtitles[0].language == "en"


def test_subtitle_all(settings) -> None:
    info = _info(
        [_video("137")],
        subtitles={
            "pt": [_subtitle("pt", "Português")],
            "en": [_subtitle("en", "English")],
        },
    )
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="all", mode=DownloadMode.AUTO)

    assert len(selection.subtitles) == 2


def test_subtitle_without_url_is_ignored(settings) -> None:
    info = _info(
        [_video("137")],
        subtitles={"pt": [SubtitleTrack(language="pt", name="Sem URL", url=None)]},
    )
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="auto", mode=DownloadMode.AUTO)

    assert selection.subtitles == []


def test_notes_mention_the_subtitle(settings) -> None:
    info = _info([_video("137")], subtitles={"pt": [_subtitle("pt", "Português")]})
    service = _service(settings)

    selection = service.select_formats(info, subtitle_lang="auto", mode=DownloadMode.AUTO)

    assert any("Legendas" in note for note in selection.notes)

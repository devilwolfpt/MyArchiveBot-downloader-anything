"""Testes da selecção de formatos (`DownloadService.select_formats`).

Cobrem os casos que quebram em produção:
- formato adaptativo sem áudio (o YouTube serve video-only) -> o áudio
  tem de ser ligado automaticamente, senao o ficheiro final fica sem som;
- `mode=both` com `format_id` explicito;
- o caminho do ficheiro final nunca pode coincidir com o do download.
"""
from __future__ import annotations

from pathlib import Path

from app.core.models import Container, DownloadMode, MediaInfo, MediaType, StreamFormat
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
        vcodec="avc1.640028",
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


def _info(formats: list[StreamFormat], title: str = "Teste de Video") -> MediaInfo:
    return MediaInfo(
        webpage_url="https://example.com/watch",
        original_url="https://example.com/watch",
        title=title,
        formats=formats,
    )


def _service(settings) -> DownloadService:
    # `select_formats` e `_output_path` nao usam o job manager nem o extractor.
    return DownloadService(settings, None, None)


# --------------------------------------------------------------------------- #
# Formato explicito
# --------------------------------------------------------------------------- #
def test_explicit_video_only_format_attaches_best_audio(settings) -> None:
    """Um formato video-only (1080p) tem de ser ligado ao melhor audio."""
    info = _info([_video("137"), _video("18", height=360, acodec="mp4a.40.2", tbr=700), _audio("140", abr=128), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(info, format_id="137", mode=DownloadMode.VIDEO)

    assert selection.primary.id == "137"
    assert selection.video is not None and selection.video.id == "137"
    assert selection.needs_merge is True
    assert selection.audio is not None
    assert selection.audio.id == "251", "deve escolher a faixa de audio com maior bitrate"


def test_explicit_video_only_format_without_merge(settings) -> None:
    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(
        info, format_id="137", mode=DownloadMode.VIDEO, merge=False
    )

    assert selection.needs_merge is False
    assert selection.audio is None


def test_explicit_muxed_format_needs_no_merge(settings) -> None:
    info = _info([_video("18", height=360, acodec="mp4a.40.2"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, format_id="18", mode=DownloadMode.VIDEO)

    assert selection.needs_merge is False
    assert selection.audio is None


def test_explicit_audio_format(settings) -> None:
    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, format_id="140", mode=DownloadMode.AUDIO)

    assert selection.primary.id == "140"
    assert selection.audio is not None and selection.audio.id == "140"
    assert selection.needs_merge is False


def test_unknown_format_raises(settings) -> None:
    from app.core.exceptions import NoFormatFoundError

    info = _info([_video("137")])
    service = _service(settings)

    try:
        service.select_formats(info, format_id="inexistente", mode=DownloadMode.VIDEO)
    except NoFormatFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("devia levantar NoFormatFoundError")


def test_both_mode_with_explicit_format(settings) -> None:
    """Regressao: `audio_pool` era usado antes de estar definido."""
    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, format_id="137", mode=DownloadMode.BOTH)

    assert selection.video is not None and selection.video.id == "137"
    assert selection.audio is not None and selection.audio.id == "140"
    assert selection.needs_merge is True


# --------------------------------------------------------------------------- #
# Modo automatico
# --------------------------------------------------------------------------- #
def test_video_mode_prefers_muxed_format(settings) -> None:
    info = _info([_video("137"), _video("18", height=360, acodec="mp4a.40.2", tbr=700), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.VIDEO)

    assert selection.primary.id == "18"
    assert selection.needs_merge is False


def test_original_mode_does_not_merge(settings) -> None:
    """`original` = stream unico tal como esta (sem merge)."""
    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, format_id="137", mode=DownloadMode.ORIGINAL)

    assert selection.needs_merge is False
    assert selection.audio is None


def test_video_mode_falls_back_to_merge_when_no_muxed(settings) -> None:
    info = _info([_video("137"), _video("248", height=2160), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.VIDEO)

    assert selection.primary.id == "248"
    assert selection.needs_merge is True
    assert selection.audio is not None and selection.audio.id == "140"


def test_audio_mode_picks_best_audio(settings) -> None:
    info = _info([_video("137"), _audio("140", abr=128), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.AUDIO)

    assert selection.primary.id == "251"
    assert selection.needs_merge is False


def test_both_mode_picks_best_video_and_audio(settings) -> None:
    info = _info([_video("137", height=1080), _video("248", height=2160), _audio("140", abr=128), _audio("251", abr=160)])
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.BOTH)

    assert selection.primary.id == "248"
    assert selection.audio is not None and selection.audio.id == "251"
    assert selection.needs_merge is True


def test_max_height_limits_the_choice(settings) -> None:
    info = _info([_video("137", height=1080), _video("248", height=2160), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(info, mode=DownloadMode.VIDEO, max_height=1080)

    assert selection.primary.height == 1080


def test_mp4_prefers_aac_audio_over_opus(settings) -> None:
    """Opus dentro de MP4 nao e reproduzido por muitos players -> preferimos AAC."""
    info = _info(
        [
            _video("137"),
            _audio("140", abr=128, acodec="mp4a.40.2"),
            _audio("251", abr=160, acodec="opus"),
        ]
    )
    service = _service(settings)

    selection = service.select_formats(
        info, format_id="137", mode=DownloadMode.VIDEO, container="mp4"
    )

    assert selection.audio is not None
    assert selection.audio.id == "140", "em MP4 deve-se preferir a faixa AAC"


def test_webm_prefers_opus_audio(settings) -> None:
    info = _info(
        [
            _video("137"),
            _audio("140", abr=128, acodec="mp4a.40.2"),
            _audio("251", abr=160, acodec="opus"),
        ]
    )
    service = _service(settings)

    selection = service.select_formats(
        info, format_id="137", mode=DownloadMode.VIDEO, container="webm"
    )

    assert selection.audio is not None
    assert selection.audio.id == "251"


def test_merge_audio_codec_forces_aac_for_opus_in_mp4(settings) -> None:
    """Opus em MP4 -> o merge tem de transcode o audio para AAC."""
    from app.services.pipeline import DownloadService

    info = _info([_video("137"), _audio("251", abr=160, acodec="opus")])
    service = _service(settings)

    selection = service.select_formats(
        info, format_id="137", mode=DownloadMode.VIDEO, container="mp4"
    )
    codec = DownloadService._merge_audio_codec(selection, "mp4")

    # Sem AAC disponivel, o Opus tem de ser transcodeado.
    assert codec == "aac"


def test_merge_audio_codec_is_none_for_aac(settings) -> None:
    from app.services.pipeline import DownloadService

    info = _info([_video("137"), _audio("140")])
    service = _service(settings)

    selection = service.select_formats(
        info, format_id="137", mode=DownloadMode.VIDEO, container="mp4"
    )

    assert DownloadService._merge_audio_codec(selection, "mp4") is None


# --------------------------------------------------------------------------- #
# Caminho do ficheiro final
# --------------------------------------------------------------------------- #
def test_output_path_does_not_collide_with_source(settings, tmp_path: Path) -> None:
    """O ficheiro final nunca pode ter o mesmo nome do ficheiro descarregado."""
    service = _service(settings)
    info = _info([_video("137")], title="Meu Video")

    source = service.storage.final_path("Meu Video_video.mp4")
    source.write_bytes(b"video")

    out = service._output_path(info, "mp4")

    assert out != source
    assert out.suffix == ".mp4"
    assert out.name == "Meu Video.mp4"


def test_output_path_is_unique_per_job(settings) -> None:
    service = _service(settings)
    info = _info([_video("137")], title="Mesmo Nome")

    first = service._output_path(info, "mp4")
    first.write_bytes(b"x")

    second = service._output_path(info, "mp4")

    assert second != first
    assert second.exists() is False

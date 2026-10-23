"""Catálogo universal de extractores de sites do MyArchive Downloader.

Liga os 940 ficheiros de extractores residentes em `app/core/extractors/extractor/`
ao motor assíncrono do MyArchive Downloader, permitindo suportar mais de 1.750 plataformas
de vídeo e áudio sem necessidade de dependências externas.

Funciona como fallback universal para qualquer URL não coberto pelos extractores nativos
de alta prioridade (YouTube, TikTok, Twitter, etc.).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..exceptions import ExtractionError
from ..models import Container, MediaInfo, MediaType, Protocol, StreamFormat, SubtitleTrack
from .base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap do namespace nativo do catálogo
# ---------------------------------------------------------------------------
_extractors_dir = str(Path(__file__).resolve().parent)
if _extractors_dir not in sys.path:
    sys.path.insert(0, _extractors_dir)

import app.core.extractors as native_engine  # noqa: E402
# Garante compatibilidade interna sem requerer pacote externo
sys.modules.setdefault("yt_dlp", native_engine)

from . import gen_extractor_classes  # noqa: E402
from .YoutubeDL import YoutubeDL  # noqa: E402
MediaEngine = YoutubeDL


# ---------------------------------------------------------------------------
# Mapeamentos de containers / tipos / protocolos
# ---------------------------------------------------------------------------
_CONTAINER_MAP: dict[str, Container] = {
    "mp4": Container.MP4, "m4v": Container.MP4,
    "mkv": Container.MKV, "webm": Container.WEBM,
    "mov": Container.MOV, "mp3": Container.MP3,
    "m4a": Container.M4A, "opus": Container.OPUS,
    "ogg": Container.OGG, "oga": Container.OGG,
    "wav": Container.WAV, "flv": Container.FLV, "ts": Container.TS,
}

_AUDIO_EXTS = {"mp3", "m4a", "opus", "wav", "ogg", "oga", "flac", "aac", "wma"}


def _map_container(ext: str | None) -> Container:
    if not ext:
        return Container.UNKNOWN
    return _CONTAINER_MAP.get(ext.lower().strip("."), Container.UNKNOWN)


def _map_media_type(vcodec: str | None, acodec: str | None, ext: str | None) -> MediaType:
    v_none = not vcodec or vcodec == "none"
    a_none = not acodec or acodec == "none"
    if v_none and not a_none:
        return MediaType.AUDIO
    if ext and ext.lower().strip(".") in _AUDIO_EXTS:
        return MediaType.AUDIO
    if not v_none:
        return MediaType.VIDEO
    return MediaType.UNKNOWN


def _map_protocol(raw_proto: str | None, url: str) -> tuple[Protocol, str | None, str | None]:
    p = (raw_proto or "").lower()
    low_url = url.lower()
    if "m3u8" in p or ".m3u8" in low_url:
        return Protocol.HLS, "hls", url
    if "mpd" in p or ".mpd" in low_url:
        return Protocol.DASH, "dash", url
    if url.startswith("https://"):
        return Protocol.HTTPS, None, None
    return Protocol.HTTP, None, None


def _friendly_error(exc: Exception, url: str) -> str:
    """Traduz os erros do motor para mensagens compreensíveis ao utilizador."""
    msg = str(exc)
    site = urlparse(url).netloc or url

    if "HTTP Error 403" in msg or "Forbidden" in msg:
        return (
            f"Acesso negado por {site} (HTTP 403). "
            "Tenta iniciar sessão no site no browser e usar a opção 'Conta do browser' nas Definições."
        )
    if "HTTP Error 404" in msg or "Not Found" in msg:
        return f"Conteúdo não encontrado em {site}. Verifica se o link está correcto e se o vídeo ainda existe."
    if "HTTP Error 429" in msg or "Too Many Requests" in msg:
        return f"{site} bloqueou temporariamente os pedidos (demasiados acessos). Tenta de novo mais tarde."
    if "HTTP Error 401" in msg or "Unauthorized" in msg:
        return (
            f"{site} requer autenticação. "
            "Inicia sessão no browser e usa a opção 'Conta do browser' nas Definições."
        )
    if "Private video" in msg or "Sign in" in msg or "age" in msg.lower():
        return (
            f"Este vídeo em {site} é privado ou tem restrição de idade. "
            "Usa a opção 'Conta do browser' nas Definições para autenticares."
        )
    if "DRM" in msg or "Widevine" in msg or "PlayReady" in msg:
        return f"Este conteúdo em {site} está protegido por DRM e não pode ser descarregado."
    if "Unsupported URL" in msg or "no suitable" in msg.lower():
        return f"URL não reconhecido: {site}. Verifica se é um link directo de vídeo ou áudio."
    if "Unable to download" in msg or "network" in msg.lower():
        return f"Erro de rede ao aceder {site}. Verifica a tua ligação à internet e tenta de novo."
    if "cookies" in msg.lower() or "login" in msg.lower():
        return (
            f"{site} requer que estejas autenticado. "
            "Usa a opção 'Conta do browser' nas Definições."
        )
    # Mensagem genérica com o erro original (sem stack trace)
    first_line = msg.split("\n")[0].replace("ERROR: ", "").strip()
    return f"Falha em {site}: {first_line}"


# ---------------------------------------------------------------------------
# Extractor principal do catálogo
# ---------------------------------------------------------------------------
class SiteCatalogExtractor(BaseExtractor):
    """Extractor universal: cobre 1.750+ plataformas via catálogo nativo integrado."""

    name: ClassVar[str] = "catalog"
    priority: ClassVar[int] = 500
    # catch_all=True: intercepta TUDO que não foi já tratado por extractores
    # de prioridade mais alta (youtube/tiktok/etc.). Assim nenhum URL é perdido.
    catch_all: ClassVar[bool] = True

    _cached_classes: ClassVar[list[Any] | None] = None

    @classmethod
    def _get_classes(cls) -> list[Any]:
        if cls._cached_classes is None:
            cls._cached_classes = [c for c in gen_extractor_classes() if c.IE_NAME != "generic"]
        return cls._cached_classes

    def matches(self, url: str) -> bool:
        # catch_all=True significa que este método é chamado mas o routing já
        # garante que chegamos aqui apenas quando nenhum extractor de prioridade
        # maior fez match. Devolvemos sempre True.
        return True

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        """Extrai metadados e formatos usando o motor completo do catálogo nativo."""
        headers = ctx.request_headers()
        cookies = ctx.cookies

        try:
            raw_info = await asyncio.to_thread(
                self._extract_sync, url, headers, cookies, ctx
            )
        except Exception as exc:
            friendly = _friendly_error(exc, url)
            log.warning("Catálogo falhou em %s: %s", url, exc)
            raise ExtractionError(
                friendly,
                details={"url": url, "raw_error": str(exc)},
            ) from exc

        if not raw_info:
            raise ExtractionError(f"Nenhum metadado retornado para {url}")

        # Se for uma playlist, pega no primeiro item com formatos
        if raw_info.get("_type") in ("playlist", "multi_video") or "entries" in raw_info:
            entries = [e for e in (raw_info.get("entries") or []) if e]
            if not entries:
                raise ExtractionError(f"Sem streams disponíveis em: {url}")
            # Filtra entradas que tenham formatos reais
            for entry in entries:
                if entry.get("formats") or entry.get("url"):
                    raw_info = entry
                    break
            else:
                raw_info = entries[0]

        return self._convert(raw_info, original_url=url)

    @staticmethod
    def _extract_sync(
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str],
        ctx: ExtractContext | None = None,
    ) -> dict[str, Any]:
        """Execução síncrona do motor de extracção (corre em thread separada)."""
        ydl_opts: dict[str, Any] = {
            "skip_download": True,
            "extract_flat": False,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "socket_timeout": 30,
            # Retries automáticas em caso de falha de rede
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            # User-Agent realista
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        }

        # Sobrepõe com os headers da sessão do utilizador (podem incluir cookies de auth)
        if headers:
            ydl_opts["http_headers"].update(headers)

        # Suporte nativo a ficheiro Netscape cookies.txt (ex.: Get cookies.txt LOCALLY)
        cookie_path = None
        if ctx is not None:
            if ctx.cookie_file and Path(ctx.cookie_file).exists():
                cookie_path = str(ctx.cookie_file)
            elif getattr(ctx, "settings", None) and ctx.settings.active_cookie_file:
                cookie_path = str(ctx.settings.active_cookie_file)
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path

        with YoutubeDL(ydl_opts) as ydl:
            # Cookies adicionais passados como string no header
            if cookies:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                ydl.params["http_headers"]["Cookie"] = cookie_str
            return ydl.extract_info(url, download=False)

    def _convert(self, raw: dict[str, Any], original_url: str) -> MediaInfo:
        """Converte o dict de metadados do motor para o modelo interno MediaInfo."""
        webpage_url = raw.get("webpage_url") or original_url
        original_url_final = raw.get("original_url") or original_url
        title = raw.get("title") or "Sem título"
        extractor_name = raw.get("extractor") or raw.get("extractor_key") or "catalog"

        # Normaliza formatos — se não houver lista, usa o próprio dict como formato único
        raw_formats = raw.get("formats") or []
        if not raw_formats and raw.get("url"):
            raw_formats = [raw]

        formats: list[StreamFormat] = []
        seen_urls: set[str] = set()

        for i, f in enumerate(raw_formats):
            stream_url = f.get("url")
            if not stream_url or stream_url in seen_urls:
                continue
            seen_urls.add(stream_url)

            vcodec = f.get("vcodec") or None
            acodec = f.get("acodec") or None
            ext = f.get("ext")

            if vcodec == "none":
                vcodec = None
            if acodec == "none":
                acodec = None

            media_type = _map_media_type(vcodec, acodec, ext)
            container = _map_container(ext)
            protocol, manifest_kind, manifest_url = _map_protocol(f.get("protocol"), stream_url)

            # manifest_url explícito sobrepõe o detectado
            if f.get("manifest_url"):
                manifest_url = f["manifest_url"]

            fmt_id = str(f.get("format_id") or f"fmt-{i}")
            # Herda headers do formato ou do item pai (para CDNs que exigem Referer)
            fmt_headers = dict(f.get("http_headers") or raw.get("http_headers") or {})

            formats.append(
                StreamFormat(
                    id=fmt_id,
                    url=stream_url,
                    media_type=media_type,
                    container=container,
                    protocol=protocol,
                    width=f.get("width"),
                    height=f.get("height"),
                    fps=float(f["fps"]) if f.get("fps") else None,
                    tbr=float(f["tbr"]) if f.get("tbr") else None,
                    abr=float(f["abr"]) if f.get("abr") else None,
                    vcodec=vcodec,
                    acodec=acodec,
                    filesize=f.get("filesize"),
                    filesize_approx=f.get("filesize_approx"),
                    duration=float(raw["duration"]) if raw.get("duration") else None,
                    language=f.get("language"),
                    audio_channels=f.get("audio_channels"),
                    manifest_url=manifest_url,
                    manifest_kind=manifest_kind,  # type: ignore[arg-type]
                    extractor_data={
                        "http_headers": fmt_headers,
                        "format_note": f.get("format_note"),
                        "quality": f.get("quality"),
                        "dynamic_range": f.get("dynamic_range"),
                    },
                )
            )

        # Legendas (manuais e automáticas)
        subtitles: dict[str, list[SubtitleTrack]] = {}
        for is_auto, sub_dict in (
            (False, raw.get("subtitles") or {}),
            (True, raw.get("automatic_captions") or {}),
        ):
            if not isinstance(sub_dict, dict):
                continue
            for lang, track_list in sub_dict.items():
                if not isinstance(track_list, list):
                    continue
                tracks = subtitles.setdefault(lang, [])
                for trk in track_list:
                    if not isinstance(trk, dict):
                        continue
                    trk_url = trk.get("url")
                    if not trk_url:
                        continue
                    tracks.append(
                        SubtitleTrack(
                            language=lang,
                            name=trk.get("name") or (f"{lang} (auto)" if is_auto else lang),
                            ext=trk.get("ext") or "vtt",
                            url=trk_url,
                            automatic=is_auto,
                        )
                    )

        return MediaInfo(
            id=str(raw.get("id") or ""),
            extractor=extractor_name,
            webpage_url=webpage_url,
            original_url=original_url_final,
            title=title,
            description=raw.get("description"),
            thumbnail=raw.get("thumbnail"),
            duration=float(raw["duration"]) if raw.get("duration") else None,
            uploader=raw.get("uploader") or raw.get("channel") or raw.get("creator"),
            uploader_id=raw.get("uploader_id") or raw.get("channel_id"),
            upload_date=str(raw.get("upload_date") or "") or None,
            view_count=raw.get("view_count"),
            is_live=bool(raw.get("is_live", False)),
            age_limit=raw.get("age_limit"),
            tags=raw.get("tags") or [],
            formats=formats,
            subtitles=subtitles,
            extractor_data={"site_extractor": extractor_name},
        )

"""Modelos de domínio (Pydantic v2) partilhados por todas as camadas.

Estes modelos são o *contrato* entre o core (extractores/manifestos/downloader)
e a API. Tudo o que sai da API é serializado a partir daqui, o que garante
um formato estável para o frontend e para o motor de busca.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class MediaType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


class Container(str, Enum):
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"
    MOV = "mov"
    MP3 = "mp3"
    M4A = "m4a"
    OPUS = "opus"
    OGG = "ogg"
    WAV = "wav"
    FLV = "flv"
    TS = "ts"
    UNKNOWN = "unknown"


class Protocol(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    HLS = "m3u8"
    DASH = "mpd"
    DATA = "data"


class DownloadMode(str, Enum):
    """Modo pedido pelo utilizador."""

    VIDEO = "video"          # melhor vídeo com áudio incorporado (ou merge)
    AUDIO = "audio"          # apenas áudio -> extrai mp3
    BOTH = "both"            # merge de vídeo + áudio de melhor qualidade
    ORIGINAL = "original"    # stream único tal como está
    AUTO = "auto"            # o programa escolhe tudo (vídeo, áudio, legendas)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobPhase(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    FINALIZING = "finalizing"
    DONE = "done"


# --------------------------------------------------------------------------- #
# Streams e formatos
# --------------------------------------------------------------------------- #
class StreamFormat(BaseModel):
    """Um formato individual de media (video-only, audio-only ou combinado)."""

    id: str
    url: str
    media_type: MediaType = MediaType.UNKNOWN
    container: Container = Container.UNKNOWN
    protocol: Protocol = Protocol.HTTPS

    width: int | None = None
    height: int | None = None
    fps: float | None = None
    tbr: float | None = None
    abr: float | None = None

    vcodec: str | None = None
    acodec: str | None = None

    filesize: int | None = None
    filesize_approx: int | None = None
    duration: float | None = None

    language: str | None = None
    audio_channels: int | None = None

    # Manifesto (HLS/DASH) do qual este formato foi derivado, se aplicável.
    manifest_url: str | None = None
    manifest_kind: Literal["hls", "dash"] | None = None

    # Metadados "crus" devolvidos pelo extractor (ex.: itag, track id).
    extractor_data: dict[str, Any] = Field(default_factory=dict)

    # Flag interno: formato com token expirado / não resolvivel.
    unavailable: bool = False
    unavailable_reason: str | None = None

    @property
    def label(self) -> str:
        """Rótulo legível para UI (ex.: ``1080p · mp4 · 4.5 MB``)."""
        parts: list[str] = []
        if self.height:
            parts.append(f"{self.height}p")
        elif self.width:
            parts.append(f"{self.width}w")
        elif self.media_type is MediaType.AUDIO:
            parts.append("áudio")
        if self.fps and self.fps >= 50:
            parts.append(f"{int(self.fps)}fps")
        if self.container.value not in ("unknown",):
            parts.append(self.container.value)
        if self.abr:
            parts.append(f"{int(self.abr)}kbps")
        elif self.tbr:
            parts.append(f"{int(self.tbr)}kbps")
        return " · ".join(parts) or self.id


class SubtitleTrack(BaseModel):
    language: str
    name: str | None = None
    ext: str = "vtt"
    url: str | None = None
    automatic: bool = False

    def track_id(self, group: str = "") -> str:
        """Identificador estável da faixa (usado pela interface)."""
        return f"{group}:{self.language}:{self.name or 'default'}"


class MediaInfo(BaseModel):
    """Resultado da extracção de metadados de um URL."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    extractor: str = "generic"
    webpage_url: str
    original_url: str
    title: str = "Sem título"
    description: str | None = None
    thumbnail: str | None = None
    duration: float | None = None
    uploader: str | None = None
    uploader_id: str | None = None
    upload_date: str | None = None
    view_count: int | None = None
    is_live: bool = False
    age_limit: int | None = None
    tags: list[str] = Field(default_factory=list)

    formats: list[StreamFormat] = Field(default_factory=list)
    subtitles: dict[str, list[SubtitleTrack]] = Field(default_factory=dict)

    # Codecs/formatos preferidos a usar quando `mode=both`.
    video_selector: str | None = None
    audio_selector: str | None = None

    warning: str | None = None
    extractor_data: dict[str, Any] = Field(default_factory=dict)

    created_at: float = Field(default_factory=time.time)

    @field_validator("original_url", "webpage_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def best_video(self) -> StreamFormat | None:
        """Melhor stream de vídeo (preferindo resolução e depois bitrate)."""
        cands = [
            f
            for f in self.formats
            if f.media_type is MediaType.VIDEO and not f.unavailable
        ]
        if not cands:
            return None
        return max(cands, key=lambda f: (f.height or 0, f.tbr or 0, f.filesize or 0))

    def best_audio(self) -> StreamFormat | None:
        cands = [
            f
            for f in self.formats
            if f.media_type is MediaType.AUDIO and not f.unavailable
        ]
        if not cands:
            return None
        return max(cands, key=lambda f: (f.abr or 0, f.tbr or 0, f.filesize or 0))

    def best_muxed(self) -> StreamFormat | None:
        """Formato único que já contém vídeo+áudio."""
        cands = [
            f
            for f in self.formats
            if f.media_type is MediaType.VIDEO
            and not f.unavailable
            and f.vcodec
            and f.acodec
            and f.vcodec != "none"
            and f.acodec != "none"
        ]
        if not cands:
            return None
        return max(cands, key=lambda f: (f.height or 0, f.tbr or 0))


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #
class SegmentRef(BaseModel):
    """Um segmento individual (HLS media segment ou DASH chunk)."""

    index: int
    url: str
    duration: float = 0.0
    byte_range: tuple[int, int] | None = None  # (offset, length)
    title: str | None = None
    discontinuity: bool = False
    init: bool = False


class ManifestInfo(BaseModel):
    """Manifesto de media já parseado e normalizado."""

    kind: Literal["hls", "dash"]
    url: str
    duration: float | None = None
    is_live: bool = False
    segment_count: int = 0
    total_bytes: int | None = None
    variants: list["VariantInfo"] = Field(default_factory=list)
    segments: list[SegmentRef] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    initializations: list[SegmentRef] = Field(default_factory=list)


class VariantInfo(BaseModel):
    """Variant de um master playlist (HLS) ou Representation (DASH)."""

    id: str
    url: str
    media_type: MediaType = MediaType.UNKNOWN
    container: Container = Container.UNKNOWN
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    codecs: str | None = None
    frame_rate: float | None = None
    audio_group: str | None = None
    language: str | None = None
    manifest_url: str | None = None
    manifest_kind: Literal["hls", "dash"] | None = None
    media_playlist_url: str | None = None


# --------------------------------------------------------------------------- #
# Progresso / Jobs
# --------------------------------------------------------------------------- #
class ProgressEvent(BaseModel):
    """Evento de progresso emitido pelo job (REST + WebSocket)."""

    job_id: str
    status: JobStatus = JobStatus.RUNNING
    phase: JobPhase = JobPhase.IDLE
    percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float = 0.0  # bytes/s
    eta: float | None = None  # segundos
    segment_index: int = 0
    segment_total: int = 0
    message: str | None = None
    filename: str | None = None
    timestamp: float = Field(default_factory=time.time)

    @field_validator("percent")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(100.0, round(v, 2)))


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    filename: str | None = None
    path: str | None = None
    size: int | None = None
    mime_type: str | None = None
    download_url: str | None = None
    thumbnail: str | None = None
    title: str | None = None
    error: dict[str, Any] | None = None
    created_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    duration: float | None = None
    #: Avisos não bloqueantes (ex.: FFmpeg ausente, formato fragmentado).
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    cookies_from_browser: Literal["chrome", "firefox", "edge", "brave", "safari"] | None = None
    preferred_extractor: str | None = None


class DownloadRequest(BaseModel):
    """Pedido de download.

    A selecção é feita em três eixos independentes — vídeo, áudio e
    legendas — e cada um aceita o valor especial ``"auto"``, que deixa a
    escolha para o programa (a melhor qualidade disponível).

    Quando nenhum selector é indicado, o modo ``auto`` é assumido: o
    programa escolhe o melhor vídeo, a melhor faixa de áudio (anexada ao
    vídeo se necessário) e as legendas disponíveis.
    """

    url: str = Field(..., min_length=4, max_length=2048)

    #: Formato único explícito (equivalente a ``-f``).
    format_id: str | None = None
    #: Selector explícito do stream de vídeo.
    video_format_id: str | None = None
    #: Selector explícito da faixa de áudio.
    audio_format_id: str | None = None
    #: Legendas: ``None`` (nenhuma), ``"auto"`` (o programa escolhe),
    #: um identificador de faixa, um código de idioma ou ``"all"``.
    subtitle_lang: str | None = None

    mode: DownloadMode = DownloadMode.AUTO
    container: str | None = None
    extract_audio: bool = False
    audio_format: Literal["mp3", "m4a", "opus", "wav"] = "mp3"
    quality: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    cookies_from_browser: Literal["chrome", "firefox", "edge", "brave", "safari"] | None = None
    filename: str | None = None
    max_height: int | None = None
    merge: bool = True


class StreamRequest(BaseModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    start: int = 0
    end: int | None = None


ManifestInfo.model_rebuild()

"""Parser HLS (RFC 8216) — master playlists e media playlists.

Suporta:
  * ``EXT-X-STREAM-INF`` / ``EXT-X-I-FRAME-STREAM-INF`` (master);
  * ``EXT-X-MEDIA`` (faixas de áudio alternadas);
  * ``EXTINF``, ``EXT-X-TARGETDURATION``, ``EXT-X-MEDIA-SEQUENCE``, ``EXT-X-ENDLIST``;
  * ``EXT-X-KEY`` (AES-128 / SAMPLE-AES) e ``EXT-X-SESSION-KEY``;
  * ``EXT-X-MAP`` (segmento de inicialização);
  * ``EXT-X-BYTERANGE`` (segmentos com byte range);
  * ``EXT-X-DISCONTINUITY``, ``EXT-X-GAP``, ``EXT-X-PROGRAM-DATE-TIME``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..exceptions import ManifestParseError
from ..models import Container, MediaType, Protocol

_ATTR_RE = re.compile(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)')


def parse_attribute_list(raw: str) -> dict[str, str]:
    """Parse de ``KEY=VALUE,KEY="quoted value"`` preservando vírgulas internas."""
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        key = match.group(1)
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[key] = value
    return attrs


def _split_codecs(codecs: str) -> tuple[str | None, str | None]:
    """Separa codecs DASH/HLS em (video, audio)."""
    video = audio = None
    for codec in codecs.split(","):
        codec = codec.strip().lower()
        if not codec:
            continue
        if codec.startswith(("avc", "hvc", "hev", "vp9", "vp8", "av01", "theora")):
            video = codec
        elif codec.startswith(("mp4a", "opus", "vorbis", "ac-3", "ec-3", "flac")):
            audio = codec
    return video, audio


def _container_from_mime(mime: str | None) -> Container:
    if not mime:
        return Container.UNKNOWN
    mime = mime.lower()
    if "mp4" in mime:
        return Container.MP4
    if "webm" in mime:
        return Container.WEBM
    if "mp2t" in mime:
        return Container.TS
    if "mp3" in mime or "mpeg" in mime:
        return Container.MP3
    return Container.UNKNOWN


@dataclass(slots=True)
class HlsKey:
    method: str
    uri: str | None
    iv: str | None
    key_format: str = "identity"
    key_format_versions: str | None = None

    @property
    def is_encrypted(self) -> bool:
        return self.method not in ("NONE", "")


@dataclass(slots=True)
class HlsSegment:
    index: int
    uri: str
    duration: float = 0.0
    title: str | None = None
    byte_range: tuple[int, int] | None = None  # (offset, length)
    discontinuity: bool = False
    gap: bool = False
    key: HlsKey | None = None
    map_uri: str | None = None
    map_byte_range: tuple[int, int] | None = None


@dataclass(slots=True)
class HlsVariant:
    """Um ``EXT-X-STREAM-INF`` (ou I-FRAME) do master playlist."""

    id: str
    uri: str
    bandwidth: int | None = None
    average_bandwidth: int | None = None
    codecs: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    audio_group: str | None = None
    video_group: str | None = None
    subtitles_group: str | None = None
    name: str | None = None
    is_iframe: bool = False
    media_type: MediaType = MediaType.VIDEO
    container: Container = Container.TS

    @property
    def vcodec(self) -> str | None:
        return _split_codecs(self.codecs or "")[0]

    @property
    def acodec(self) -> str | None:
        return _split_codecs(self.codecs or "")[1]


@dataclass(slots=True)
class HlsMediaRendition:
    """Uma entrada ``EXT-X-MEDIA`` (áudio/subtítulo alternativo)."""

    type: str
    group_id: str
    name: str
    uri: str | None
    language: str | None = None
    default: bool = False
    autoselect: bool = False
    channels: str | None = None


@dataclass(slots=True)
class HlsMasterPlaylist:
    variants: list[HlsVariant] = field(default_factory=list)
    renditions: list[HlsMediaRendition] = field(default_factory=list)
    session_keys: list[HlsKey] = field(default_factory=list)
    target_duration: float | None = None
    media_sequence: int = 0
    is_live: bool = False
    duration: float | None = None


@dataclass(slots=True)
class HlsMediaPlaylist:
    segments: list[HlsSegment] = field(default_factory=list)
    target_duration: float | None = None
    media_sequence: int = 0
    is_live: bool = False
    duration: float = 0.0
    playlist_type: str | None = None


def _parse_resolution(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    match = re.match(r"^(\d+)x(\d+)$", value.strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_byte_range(value: str) -> tuple[int, int]:
    """``<length>[@<offset>]`` — offset herda do segmento anterior se ausente."""
    length_str, _, offset_str = value.partition("@")
    length = int(length_str)
    offset = int(offset_str) if offset_str else 0
    return offset, length


class HlsParser:
    """Parser stateless de playlists m3u8."""

    def is_master(self, content: str) -> bool:
        return "#EXT-X-STREAM-INF" in content or "#EXT-X-I-FRAME-STREAM-INF" in content

    # ------------------------------------------------------------------ #
    def parse_master(self, content: str, base_url: str) -> HlsMasterPlaylist:
        if "#EXTM3U" not in content:
            raise ManifestParseError("Playlist inválida: falta #EXTM3U")
        master = HlsMasterPlaylist()
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        pending: dict[str, str] | None = None
        pending_iframe = False

        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF"):
                pending = parse_attribute_list(line.split(":", 1)[1])
                continue
            if line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
                attrs = parse_attribute_list(line.split(":", 1)[1])
                uri = attrs.get("URI")
                if uri:
                    master.variants.append(self._build_variant(uri, attrs, base_url, is_iframe=True))
                continue
            if line.startswith("#EXT-X-MEDIA"):
                attrs = parse_attribute_list(line.split(":", 1)[1])
                rend = HlsMediaRendition(
                    type=attrs.get("TYPE", "UNKNOWN").upper(),
                    group_id=attrs.get("GROUP-ID", ""),
                    name=attrs.get("NAME", ""),
                    uri=urljoin(base_url, attrs["URI"]) if attrs.get("URI") else None,
                    language=attrs.get("LANGUAGE"),
                    default=attrs.get("DEFAULT", "NO").upper() == "YES",
                    autoselect=attrs.get("AUTOSELECT", "NO").upper() == "YES",
                    channels=attrs.get("CHANNELS"),
                )
                master.renditions.append(rend)
                continue
            if line.startswith("#EXT-X-SESSION-KEY"):
                attrs = parse_attribute_list(line.split(":", 1)[1])
                master.session_keys.append(self._build_key(attrs, base_url))
                continue
            if line.startswith("#EXT-X-TARGETDURATION"):
                try:
                    master.target_duration = float(line.split(":", 1)[1])
                except ValueError:
                    pass
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    master.media_sequence = int(line.split(":", 1)[1])
                except ValueError:
                    pass
                continue
            if line.startswith("#EXT-X-PLAYLIST-TYPE"):
                master.playlist_type = line.split(":", 1)[1].strip()
                continue
            if line.startswith("#EXT-X-ENDLIST"):
                master.is_live = False
                continue
            if line.startswith("#"):
                continue

            # Linha de URI
            if pending is not None:
                master.variants.append(self._build_variant(line, pending, base_url))
                pending = None
            elif pending_iframe:
                pending_iframe = False

        if not master.variants and not master.renditions:
            raise ManifestParseError("Master playlist sem variantes")
        return master

    def _build_variant(
        self,
        uri: str,
        attrs: dict[str, str],
        base_url: str,
        *,
        is_iframe: bool = False,
    ) -> HlsVariant:
        width, height = _parse_resolution(attrs.get("RESOLUTION"))
        codecs = attrs.get("CODECS")
        v_codec, a_codec = _split_codecs(codecs or "")
        media_type = MediaType.VIDEO if v_codec else (MediaType.AUDIO if a_codec else MediaType.VIDEO)
        variant_id = attrs.get("VIDEO") or attrs.get("AUDIO") or ""
        return HlsVariant(
            id=variant_id or f"{height or '?'}p-{attrs.get('BANDWIDTH', '0')}",
            uri=urljoin(base_url, uri),
            bandwidth=int(attrs["BANDWIDTH"]) if attrs.get("BANDWIDTH", "").isdigit() else None,
            average_bandwidth=(
                int(attrs["AVERAGE-BANDWIDTH"]) if attrs.get("AVERAGE-BANDWIDTH", "").isdigit() else None
            ),
            codecs=codecs,
            width=width,
            height=height,
            frame_rate=float(attrs["FRAME-RATE"]) if attrs.get("FRAME-RATE") else None,
            audio_group=attrs.get("AUDIO"),
            video_group=attrs.get("VIDEO"),
            subtitles_group=attrs.get("SUBTITLES"),
            is_iframe=is_iframe,
            media_type=media_type,
            container=Container.TS,
        )

    def _build_key(self, attrs: dict[str, str], base_url: str) -> HlsKey:
        return HlsKey(
            method=attrs.get("METHOD", "NONE").upper(),
            uri=urljoin(base_url, attrs["URI"]) if attrs.get("URI") else None,
            iv=attrs.get("IV"),
            key_format=attrs.get("KEYFORMAT", "identity"),
            key_format_versions=attrs.get("KEYFORMATVERSIONS"),
        )

    # ------------------------------------------------------------------ #
    def parse_media(self, content: str, base_url: str) -> HlsMediaPlaylist:
        if "#EXTM3U" not in content:
            raise ManifestParseError("Playlist inválida: falta #EXTM3U")
        playlist = HlsMediaPlaylist()
        current_key: HlsKey | None = None
        pending_duration = 0.0
        pending_title: str | None = None
        pending_range: tuple[int, int] | None = None
        pending_discontinuity = False
        pending_gap = False
        map_uri: str | None = None
        map_range: tuple[int, int] | None = None
        last_range_end = 0
        index = 0

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("#EXTINF:"):
                payload = line[len("#EXTINF:"):]
                duration_str, _, title = payload.partition(",")
                try:
                    pending_duration = float(duration_str.strip())
                except ValueError:
                    pending_duration = 0.0
                pending_title = title.strip() or None
                continue

            if line.startswith("#EXT-X-KEY"):
                attrs = parse_attribute_list(line.split(":", 1)[1])
                key = self._build_key(attrs, base_url)
                current_key = None if key.method == "NONE" else key
                continue

            if line.startswith("#EXT-X-SESSION-KEY"):
                continue

            if line.startswith("#EXT-X-MAP"):
                attrs = parse_attribute_list(line.split(":", 1)[1])
                if attrs.get("URI"):
                    map_uri = urljoin(base_url, attrs["URI"])
                    map_range = (
                        _parse_byte_range(attrs["BYTERANGE"]) if attrs.get("BYTERANGE") else None
                    )
                continue

            if line.startswith("#EXT-X-BYTERANGE"):
                offset, length = _parse_byte_range(line.split(":", 1)[1])
                if "@" not in line.split(":", 1)[1]:
                    offset = last_range_end
                pending_range = (offset, length)
                last_range_end = offset + length
                continue

            if line.startswith("#EXT-X-TARGETDURATION"):
                try:
                    playlist.target_duration = float(line.split(":", 1)[1])
                except ValueError:
                    pass
                continue

            if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    playlist.media_sequence = int(line.split(":", 1)[1])
                except ValueError:
                    pass
                continue

            if line.startswith("#EXT-X-PLAYLIST-TYPE"):
                playlist.playlist_type = line.split(":", 1)[1].strip()
                continue

            if line.startswith("#EXT-X-ENDLIST"):
                playlist.is_live = False
                continue

            if line.startswith("#EXT-X-DISCONTINUITY"):
                pending_discontinuity = True
                continue

            if line.startswith("#EXT-X-GAP"):
                pending_gap = True
                continue

            if line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
                continue

            if line.startswith("#"):
                continue

            # URI do segmento
            playlist.segments.append(
                HlsSegment(
                    index=index,
                    uri=urljoin(base_url, line),
                    duration=pending_duration,
                    title=pending_title,
                    byte_range=pending_range,
                    discontinuity=pending_discontinuity,
                    gap=pending_gap,
                    key=current_key,
                    map_uri=map_uri,
                    map_byte_range=map_range,
                )
            )
            playlist.duration += pending_duration
            index += 1
            pending_duration = 0.0
            pending_title = None
            pending_range = None
            pending_discontinuity = False
            pending_gap = False

        if not playlist.segments:
            raise ManifestParseError("Media playlist sem segmentos")
        # Live = playlist sem EXT-X-ENDLIST e com media sequence > 0
        playlist.is_live = "#EXT-X-ENDLIST" not in content
        return playlist


def protocol_for(url: str) -> Protocol:
    lowered = url.lower().split("?")[0]
    if lowered.endswith(".m3u8") or ".m3u8/" in lowered:
        return Protocol.HLS
    if lowered.endswith(".mpd"):
        return Protocol.DASH
    return Protocol.HTTPS

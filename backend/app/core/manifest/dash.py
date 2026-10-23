"""Parser DASH (ISO/IEC 23009-1) — ficheiros MPD.

Suporta:
  * ``BaseURL`` (aninhado) para resolver URLs;
  * ``SegmentTemplate`` com ``$Number$``, ``$RepresentationID$``, ``$Bandwidth$``, ``$Time$``;
  * ``SegmentTimeline`` (``S@t``, ``@d``, ``@r``);
  * ``SegmentList`` (``Initialization`` + ``SegmentURL``);
  * ``SegmentBase`` (``Initialization`` + ``indexRange``);
  * múltiplos ``Period`` concatenados num único plano de segmentos.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..exceptions import ManifestParseError
from ..models import Container, MediaType

NS = {
    "mpd": "urn:mpeg:dash:schema:mpd:2011",
    "scte35": "urn:mpeg:dash:schema:mpd:2011:scte35",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _tag(element: ET.Element) -> str:
    """Nome do elemento sem namespace."""
    return element.tag.split("}")[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _tag(child) == name:
            return child
    return None


def _findall(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _tag(child) == name]


def _parse_iso8601_duration(value: str | None) -> float | None:
    """Converte ``PT1H2M3.5S`` em segundos."""
    if not value:
        return None
    match = re.match(
        r"^P(?:(?P<days>[\d.]+)D)?(?:T(?:(?P<h>[\d.]+)H)?(?:(?P<m>[\d.]+)M)?(?:(?P<s>[\d.]+)S)?)?$",
        value.strip(),
    )
    if not match:
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v}
    total = 0.0
    total += parts.get("days", 0) * 86400
    total += parts.get("h", 0) * 3600
    total += parts.get("m", 0) * 60
    total += parts.get("s", 0)
    return total or None


def _split_codecs(codecs: str | None) -> tuple[str | None, str | None]:
    if not codecs:
        return None, None
    video = audio = None
    for codec in codecs.split(","):
        codec = codec.strip().lower()
        if codec.startswith(("avc", "hvc", "hev", "vp9", "vp8", "av01")):
            video = codec
        elif codec.startswith(("mp4a", "opus", "vorbis", "ac-3", "ec-3", "flac")):
            audio = codec
    return video, audio


@dataclass(slots=True)
class DashSegment:
    index: int
    url: str
    duration: float = 0.0
    byte_range: tuple[int, int] | None = None
    init: bool = False


@dataclass(slots=True)
class DashRepresentation:
    id: str
    url: str
    bandwidth: int | None = None
    codecs: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    mime_type: str | None = None
    content_type: str = "video"
    audio_sampling_rate: int | None = None
    segments: list[DashSegment] = field(default_factory=list)
    initialization: DashSegment | None = None
    duration: float | None = None
    is_live: bool = False

    @property
    def vcodec(self) -> str | None:
        return _split_codecs(self.codecs)[0]

    @property
    def acodec(self) -> str | None:
        return _split_codecs(self.codecs)[1]

    @property
    def media_type(self) -> MediaType:
        if self.content_type == "audio":
            return MediaType.AUDIO
        if self.content_type in ("video", "text"):
            return MediaType.VIDEO
        return MediaType.UNKNOWN


@dataclass(slots=True)
class DashManifest:
    representations: list[DashRepresentation] = field(default_factory=list)
    duration: float | None = None
    is_live: bool = False
    min_buffer_time: float | None = None


class DashParser:
    """Parser MPD baseado no ``xml.etree`` standard (sem dependências extra)."""

    def parse(self, content: bytes | str, base_url: str) -> DashManifest:
        try:
            if isinstance(content, bytes):
                root = ET.fromstring(content)
            else:
                root = ET.fromstring(content.encode("utf-8"))
        except ET.ParseError as exc:
            raise ManifestParseError(f"MPD inválido: {exc}") from exc

        if _tag(root) != "MPD":
            raise ManifestParseError("XML não é um MPD")

        manifest = DashManifest()
        manifest.duration = _parse_iso8601_duration(root.get("mediaPresentationDuration"))
        manifest.min_buffer_time = _parse_iso8601_duration(root.get("minBufferTime"))
        manifest.is_live = root.get("type") == "dynamic"

        mpd_base = self._resolve_base(root, base_url)

        for period in _findall(root, "Period"):
            period_base = self._resolve_base(period, mpd_base)
            period_duration = _parse_iso8601_duration(period.get("duration")) or manifest.duration
            for adaptation in _findall(period, "AdaptationSet"):
                self._parse_adaptation_set(adaptation, period_base, period_duration, manifest)

        if not manifest.representations:
            raise ManifestParseError("MPD sem representações")
        return manifest

    # ------------------------------------------------------------------ #
    def _resolve_base(self, element: ET.Element, parent_base: str) -> str:
        base = parent_base
        base_el = _find(element, "BaseURL")
        if base_el is not None and base_el.text:
            base = urljoin(base, base_el.text.strip())
        return base

    def _parse_adaptation_set(
        self,
        adaptation: ET.Element,
        base_url: str,
        period_duration: float | None,
        manifest: DashManifest,
    ) -> None:
        adapt_base = self._resolve_base(adaptation, base_url)
        mime_type = adaptation.get("mimeType")
        content_type = adaptation.get("contentType")
        if content_type is None:
            content_type = "audio" if (mime_type or "").startswith("audio") else "video"

        for rep_el in _findall(adaptation, "Representation"):
            rep_base = self._resolve_base(rep_el, adapt_base)
            rep_mime = rep_el.get("mimeType") or mime_type
            frame_rate = self._parse_frame_rate(rep_el.get("frameRate") or adaptation.get("frameRate"))
            bandwidth = rep_el.get("bandwidth")
            representation = DashRepresentation(
                id=rep_el.get("id") or f"rep-{len(manifest.representations)}",
                url=rep_base,
                bandwidth=int(bandwidth) if bandwidth and bandwidth.isdigit() else None,
                codecs=rep_el.get("codecs") or adaptation.get("codecs"),
                width=int(rep_el.get("width")) if rep_el.get("width") else None,
                height=int(rep_el.get("height")) if rep_el.get("height") else None,
                frame_rate=frame_rate,
                mime_type=rep_mime,
                content_type=content_type or "video",
                audio_sampling_rate=int(rep_el.get("audioSamplingRate"))
                if rep_el.get("audioSamplingRate")
                else None,
                duration=period_duration,
                is_live=manifest.is_live,
            )

            self._fill_segments(rep_el, representation, adapt_base, adaptation)
            manifest.representations.append(representation)

    def _parse_frame_rate(self, value: str | None) -> float | None:
        if not value:
            return None
        if "/" in value:
            num, _, den = value.partition("/")
            try:
                return float(num) / float(den) if float(den) else None
            except ValueError:
                return None
        try:
            return float(value)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    def _fill_segments(
        self,
        rep_el: ET.Element,
        representation: DashRepresentation,
        base_url: str,
        adaptation: ET.Element,
    ) -> None:
        # O SegmentTemplate pode estar ao nível do Representation ou da AdaptationSet.
        # NB: `Element.__bool__` é False quando não tem filhos -> usar `is not None`.
        template = _find(rep_el, "SegmentTemplate")
        if template is None:
            template = _find(adaptation, "SegmentTemplate")
        if template is not None:
            self._fill_from_template(template, representation, base_url)
            return

        seg_list = _find(rep_el, "SegmentList")
        if seg_list is not None:
            self._fill_from_list(seg_list, representation, base_url)
            return

        seg_base = _find(rep_el, "SegmentBase")
        if seg_base is not None:
            self._fill_from_base(seg_base, representation, base_url)
            return

        # Sem segment info: assume ficheiro único progressivo.
        representation.segments.append(DashSegment(index=0, url=base_url))

    def _fill_from_template(
        self, template: ET.Element, representation: DashRepresentation, base_url: str
    ) -> None:
        timescale = int(template.get("timescale") or 1)
        start_number = int(template.get("startNumber") or 1)
        init_template = template.get("initialization")
        media_template = template.get("media")

        rep_id = representation.id
        bandwidth = representation.bandwidth or 0

        def substitute(template_str: str, number: int, time_value: int | None = None) -> str:
            out = template_str.replace("$RepresentationID$", rep_id)
            out = out.replace("$Bandwidth$", str(bandwidth))
            out = out.replace("$Number$", str(number))
            if time_value is not None:
                out = out.replace("$Time$", str(time_value))
            if "$" in out:
                # Formatos não suportados ($Number%05d$, etc.) -> fallback seguro.
                out = re.sub(r"\$Number%0\d+d\$", str(number), out)
                out = re.sub(r"\$Time%0\d+d\$", str(time_value or 0), out)
            return out

        if init_template:
            url = urljoin(base_url, substitute(init_template, start_number))
            representation.initialization = DashSegment(index=-1, url=url, init=True)

        timeline = _find(template, "SegmentTimeline")
        if timeline is not None:
            number = start_number
            current_time = 0
            for s_el in _findall(timeline, "S"):
                t = s_el.get("t")
                if t is not None:
                    current_time = int(t)
                d = int(s_el.get("d") or 0)
                r = int(s_el.get("r") or 0)
                repeat = r if r >= 0 else 0
                for _ in range(repeat + 1):
                    if not media_template:
                        break
                    url = urljoin(base_url, substitute(media_template, number, current_time))
                    representation.segments.append(
                        DashSegment(
                            index=number - start_number,
                            url=url,
                            duration=d / timescale if timescale else 0.0,
                        )
                    )
                    number += 1
                    current_time += d
            return

        duration = int(template.get("duration") or 0)
        if duration and media_template:
            total = representation.duration or 0
            count = int(total * timescale / duration) if total and timescale else 0
            if count:
                for i in range(count):
                    number = start_number + i
                    url = urljoin(base_url, substitute(media_template, number))
                    representation.segments.append(
                        DashSegment(
                            index=i,
                            url=url,
                            duration=duration / timescale if timescale else 0.0,
                        )
                    )

    def _fill_from_list(
        self, seg_list: ET.Element, representation: DashRepresentation, base_url: str
    ) -> None:
        init_el = _find(seg_list, "Initialization")
        if init_el is not None and init_el.get("sourceURL"):
            url = urljoin(base_url, init_el.get("sourceURL", ""))
            byte_range = None
            if init_el.get("range"):
                byte_range = self._parse_range(init_el.get("range", ""))
            representation.initialization = DashSegment(index=-1, url=url, init=True, byte_range=byte_range)

        duration = float(seg_list.get("duration") or 0)
        timescale = float(seg_list.get("timescale") or 1)
        for idx, seg_el in enumerate(_findall(seg_list, "SegmentURL")):
            media = seg_el.get("media") or seg_el.get("sourceURL")
            if not media:
                continue
            url = urljoin(base_url, media)
            byte_range = None
            if seg_el.get("mediaRange"):
                byte_range = self._parse_range(seg_el.get("mediaRange", ""))
            representation.segments.append(
                DashSegment(
                    index=idx,
                    url=url,
                    duration=duration / timescale if timescale else 0.0,
                    byte_range=byte_range,
                )
            )

    def _fill_from_base(
        self, seg_base: ET.Element, representation: DashRepresentation, base_url: str
    ) -> None:
        """SegmentBase: assume download progressivo do ficheiro inteiro."""
        init_el = _find(seg_base, "Initialization")
        if init_el is not None and init_el.get("sourceURL"):
            url = urljoin(base_url, init_el.get("sourceURL", ""))
            byte_range = self._parse_range(init_el.get("range", "")) if init_el.get("range") else None
            representation.initialization = DashSegment(index=-1, url=url, init=True, byte_range=byte_range)
        representation.segments.append(DashSegment(index=0, url=base_url))

    def _parse_range(self, value: str) -> tuple[int, int]:
        value = value.strip()
        if "-" in value:
            start, _, end = value.partition("-")
            start_i = int(start) if start else 0
            end_i = int(end) if end else start_i
            return start_i, max(0, end_i - start_i + 1)
        return 0, int(value or 0)


def container_for(mime_type: str | None) -> Container:
    if not mime_type:
        return Container.UNKNOWN
    mime = mime_type.lower()
    if "mp4" in mime or "dash" in mime:
        return Container.MP4
    if "webm" in mime:
        return Container.WEBM
    if "mp2t" in mime:
        return Container.TS
    return Container.UNKNOWN

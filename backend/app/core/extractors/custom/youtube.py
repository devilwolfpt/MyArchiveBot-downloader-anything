"""Custom YouTube Extractor for MyArchive Downloader."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlparse

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol as MediaProtocol, StreamFormat, SubtitleTrack
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Cipher solver (extensível)
# --------------------------------------------------------------------------- #
class CipherSolver(Protocol):
    """Resolve assinaturas protegidas (``signatureCipher``)."""

    async def solve(self, url: str, *, player_js: str | None = None) -> dict[str, str] | None:
        """Devolve headers/params actualizados, ou ``None`` se não resolver."""
        ...


class NullCipherSolver:
    """Solver por omissão: não resolve assinaturas."""

    async def solve(self, url: str, *, player_js: str | None = None) -> dict[str, str] | None:
        return None


# --------------------------------------------------------------------------- #
# Clients InnerTube
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class InnertubeClient:
    name: str
    version: str
    api_key: str
    client_name_header: str
    extra_context: dict = field(default_factory=dict)
    user_agent: str | None = None


#: Clients InnerTube por ordem de preferência.
#:
#: Android fornece um visitorData que permite usar VisionOS, o client
#: JavaScript-free usado por omissão pelo MyArchive Downloader. Outros clients são fallback.
INNERTUBE_CLIENTS: list[InnertubeClient] = [
    InnertubeClient(
        name="ANDROID",
        version="21.26.364",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="3",
        extra_context={
            "androidSdkVersion": 30,
            "osName": "Android",
            "osVersion": "11",
        },
        user_agent="com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip",
    ),
    InnertubeClient(
        name="VISIONOS",
        version="1.02",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="101",
        extra_context={
            "deviceMake": "Apple",
            "deviceModel": "RealityDevice17,1",
            "osName": "visionOS",
            "osVersion": "26.5.23O471",
        },
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/26.0 Safari/605.1.15"
        ),
    ),
    InnertubeClient(
        name="IOS",
        version="21.26.4",
        api_key="AIzaSyB-63vPrdThhKuerbB2N_l7KwwcxJa6G2g",
        client_name_header="5",
        extra_context={
            "deviceMake": "Apple",
            "deviceModel": "iPhone16,2",
            "osName": "iPhone",
            "osVersion": "18.3.2.22D82",
        },
        user_agent=(
            "com.google.ios.youtube/21.26.4 "
            "(iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"
        ),
    ),
    InnertubeClient(
        name="TVHTML5",
        version="5.20260707",
        api_key="AIzaSyDCU8hByM-4DrUqRUYnGn-3llEO78bcxq8",
        client_name_header="7",
        extra_context={
            "deviceMake": "Samsung",
            "deviceModel": "UKS9800",
            "osName": "Tizen",
            "osVersion": "2.4.0",
        },
        user_agent=(
            "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 "
            "(KHTML, like Gecko) Version/2.4.0 TV Safari/538.1"
        ),
    ),
    InnertubeClient(
        name="TVHTML5_SIMPLY_EMBEDDED_PLAYER",
        version="2.0",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="85",
        extra_context={
            "thirdParty": {"embedUrl": "https://www.youtube.com/"},
        },
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    ),
    InnertubeClient(
        name="WEB_EMBEDDED_PLAYER",
        version="2.20260708.00.00",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="56",
        extra_context={
            "thirdParty": {"embedUrl": "https://www.youtube.com/"},
        },
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 "
            "Safari/605.1.15,gzip(gfe)"
        ),
    ),
    InnertubeClient(
        name="WEB",
        version="2.20260708.00.00",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="1",
    ),
    InnertubeClient(
        name="MWEB",
        version="2.20260708.05.00",
        api_key="AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
        client_name_header="2",
        user_agent=(
            "Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
            "Mobile/15E148 Safari/604.1,gzip(gfe)"
        ),
    ),
]


def _parse_mime_codecs(mime: str) -> tuple[str | None, str | None]:
    """Extrai ``(vcodec, acodec)`` do campo ``mimeType``.

    O YouTube codifica os codecs no mimeType quando nao devolve os campos
    ``videoCodec``/``audioCodec`` (caso dos clients mobile)::

        'video/mp4; codecs="avc1.42001E, mp4a.40.2"' -> ('avc1.42001E', 'mp4a.40.2')
        'video/webm; codecs="vp9"'                    -> ('vp9', None)
        'audio/mp4; codecs="mp4a.40.2"'               -> (None, 'mp4a.40.2')
    """
    if not mime:
        return None, None
    kind = mime.split("/", 1)[0].strip().lower()
    codecs: list[str] = []
    match = re.search(r'codecs\s*=\s*"([^"]+)"', mime, re.IGNORECASE)
    if match:
        codecs = [c.strip() for c in match.group(1).split(",") if c.strip()]
    if kind == "audio":
        return None, (codecs[0] if codecs else None)
    if kind == "video":
        # O primeiro codec e' o de video; o segundo (se existir) e' o de audio.
        return (codecs[0] if codecs else None), (codecs[1] if len(codecs) > 1 else None)
    return None, None


@dataclass(slots=True)
class _PlayerResponse:
    raw: dict
    client: InnertubeClient


class YouTubeExtractor(BaseExtractor):
    """Extrator de vídeos do YouTube."""

    name = "youtube"
    priority = 10
    #: As URLs de media do YouTube estão vinculadas ao IP de origem e
    #: expiram — não faz sentido guardá-las em cache.
    cacheable = False
    patterns = [
        r"^https?://(?:www\.|m\.|music\.)?youtube\.com/watch",
        r"^https?://(?:www\.)?youtube\.com/shorts/",
        r"^https?://(?:www\.)?youtube\.com/embed/",
        r"^https?://(?:www\.)?youtube\.com/live/",
        r"^https?://youtu\.be/",
    ]

    #: Solver de assinaturas (substituível em runtime).
    cipher_solver: CipherSolver = NullCipherSolver()

    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_video_id(url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.endswith("youtu.be"):
            video_id = parsed.path.lstrip("/")
            return video_id.split("/")[0] if video_id else None
        if "youtube" in host:
            if parsed.path == "/watch":
                values = parse_qs(parsed.query).get("v")
                return values[0] if values else None
            for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
                if parsed.path.startswith(prefix):
                    return parsed.path[len(prefix):].split("/")[0] or None
        return None

    # ------------------------------------------------------------------ #
    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        video_id = self.extract_video_id(url)
        if not video_id:
            raise ExtractionError(f"Não foi possível extrair o videoId de {url}")

        player = await self._fetch_player(video_id, ctx)
        raw = player.raw

        playability = raw.get("playabilityStatus") or {}
        status = playability.get("status", "OK")
        if status not in ("OK", "LIVE_STREAM_OFFLINE"):
            reason = playability.get("reason") or status
            raise ExtractionError(
                f"Vídeo indisponível ({status}): {reason}",
                details={"video_id": video_id, "playability": playability},
            )

        details = raw.get("videoDetails") or {}
        micro = (raw.get("microformat") or {}).get("playerMicroformatRenderer") or {}

        formats = await self._build_formats(raw, ctx)
        if not formats:
            raise ExtractionError(
                "O YouTube não devolveu formatos media",
                details={"video_id": video_id},
            )

        title = details.get("title") or micro.get("title") or f"youtube-{video_id}"
        thumbnail = self._thumbnail(video_id, micro.get("thumbnail"))
        subtitles = self._build_subtitles(raw)

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            original_url=url,
            title=title,
            description=details.get("shortDescription") or micro.get("description"),
            thumbnail=thumbnail,
            duration=float(details["lengthSeconds"]) if details.get("lengthSeconds") else None,
            uploader=details.get("author") or micro.get("ownerChannelName"),
            uploader_id=details.get("channelId"),
            upload_date=self._format_date(micro.get("publishDate") or micro.get("uploadDate")),
            view_count=int(details["viewCount"]) if str(details.get("viewCount", "")).isdigit() else None,
            is_live=details.get("isLiveContent", False) or status == "LIVE_STREAM_OFFLINE",
            age_limit=int(details["ageLimit"]) if str(details.get("ageLimit", "")).isdigit() else None,
            tags=details.get("keywords") or [],
            formats=formats,
            subtitles=subtitles,
            extractor_data={
                "video_id": video_id,
                "client": player.client.name,
                "live_json": raw.get("streamingData", {}).get("hlsManifestUrl")
                or raw.get("streamingData", {}).get("dashManifestUrl"),
            },
        )

    # ------------------------------------------------------------------ #
    async def _fetch_player(self, video_id: str, ctx: ExtractContext) -> _PlayerResponse:
        """Tenta cada client até obter uma resposta com ``streamingData``."""
        last_error: Exception | None = None
        statuses: list[str] = []
        visitor_data: str | None = None

        for client in INNERTUBE_CLIENTS:
            try:
                raw = await self._call_innertube(
                    client, video_id, ctx, visitor_data=visitor_data
                )
            except Exception as exc:  # noqa: BLE001 - tenta o próximo client
                last_error = exc
                log.debug("Client YouTube %s falhou: %s", client.name, exc)
                continue

            visitor_data = (
                (raw.get("responseContext") or {}).get("visitorData") or visitor_data
            )
            streaming = raw.get("streamingData") or {}
            if streaming.get("formats") or streaming.get("adaptiveFormats"):
                if client.name == "ANDROID" and visitor_data:
                    visionos = next(
                        (item for item in INNERTUBE_CLIENTS if item.name == "VISIONOS"),
                        None,
                    )
                    if visionos is not None:
                        try:
                            visionos_raw = await self._call_innertube(
                                visionos, video_id, ctx, visitor_data=visitor_data
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "Client YouTube VISIONOS falhou; a usar Android: %s",
                                exc,
                            )
                        else:
                            visionos_streaming = visionos_raw.get("streamingData") or {}
                            if (
                                visionos_streaming.get("formats")
                                or visionos_streaming.get("adaptiveFormats")
                            ):
                                return _PlayerResponse(raw=visionos_raw, client=visionos)
                            log.debug(
                                "Client YouTube VISIONOS sem streamingData; "
                                "a usar Android"
                            )
                return _PlayerResponse(raw=raw, client=client)

            playability = raw.get("playabilityStatus") or {}
            status = playability.get("status")
            reason = playability.get("reason")
            if status:
                statuses.append(f"{client.name}: {status}{f' ({reason})' if reason else ''}")
            log.debug(
                "Client YouTube %s sem streamingData (status=%s, reason=%s)",
                client.name,
                status,
                reason,
            )
            last_error = ExtractionError(
                f"Resposta sem streamingData{f' ({status}: {reason})' if status else ''}"
            )

        detail = "; ".join(statuses)
        raise ExtractionError(
            f"Falha ao obter dados do YouTube para {video_id}: {last_error}"
            + (f" [{detail}]" if detail else ""),
            details={"video_id": video_id},
        )

    async def _call_innertube(
        self,
        client: InnertubeClient,
        video_id: str,
        ctx: ExtractContext,
        *,
        visitor_data: str | None = None,
    ) -> dict:
        context_client: dict = {
            "clientName": client.name,
            "clientVersion": client.version,
            "hl": "pt",
            "gl": "PT",
            **client.extra_context,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-YouTube-Client-Name": client.client_name_header,
            "X-YouTube-Client-Version": client.version,
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        }
        if visitor_data:
            context_client.update(
                visitorData=visitor_data,
                timeZone="UTC",
                utcOffsetMinutes=0,
            )
            headers["X-Goog-Visitor-Id"] = visitor_data
        payload = {
            "context": {"client": context_client},
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

        # Não reutilizar os headers do browser: a combinação de UA/Origin de
        # localhost com a identidade InnerTube pode remover streamingData.
        request_headers = ctx.request_headers()
        if request_headers.get("Accept-Language"):
            headers["Accept-Language"] = request_headers["Accept-Language"]
        if client.user_agent:
            headers["User-Agent"] = client.user_agent

        url = f"https://www.youtube.com/youtubei/v1/player?key={client.api_key}&prettyPrint=false"
        result = await ctx.http.post_json(url, payload, headers=headers)
        return result

    # ------------------------------------------------------------------ #
    async def _build_formats(self, raw: dict, ctx: ExtractContext) -> list[StreamFormat]:
        streaming = raw.get("streamingData") or {}
        formats: list[StreamFormat] = []

        for group, adaptive in (("formats", False), ("adaptiveFormats", True)):
            for entry in streaming.get(group) or []:
                fmt = await self._format_from_entry(entry, adaptive, ctx)
                if fmt is not None:
                    formats.append(fmt)

        # HLS/DASH directos (formato unico com multiplos streams)
        hls = streaming.get("hlsManifestUrl")
        if hls:
            formats.extend(
                await self.formats_from_manifest(hls, ctx, headers=ctx.request_headers())
            )
        dash = streaming.get("dashManifestUrl")
        if dash:
            formats.extend(
                await self.formats_from_manifest(dash, ctx, headers=ctx.request_headers())
            )

        # De-duplicar por (tipo, altura, codec)
        unique: dict[tuple, StreamFormat] = {}
        for fmt in formats:
            key = (fmt.media_type, fmt.height, fmt.vcodec, fmt.acodec, fmt.id)
            unique.setdefault(key, fmt)
        return list(unique.values())

    async def _format_from_entry(
        self, entry: dict, adaptive: bool, ctx: ExtractContext
    ) -> StreamFormat | None:
        itag = entry.get("itag")
        mime = entry.get("mimeType", "")
        bitrate = entry.get("bitrate")
        url = entry.get("url")

        # --- signatureCipher / cipher ---
        protected = False
        if not url:
            cipher = entry.get("signatureCipher") or entry.get("cipher")
            if not cipher:
                return None
            protected = True
            cipher_params = parse_qs(cipher)
            url = cipher_params.get("url", [None])[0]
            signature = cipher_params.get("s", [None])[0]
            sp = cipher_params.get("sp", ["signature"])[0]
            if not url:
                return None
            resolved = await self.cipher_solver.solve(url)
            if resolved and resolved.get("signature"):
                parsed = urlparse(url)
                query = parse_qs(parsed.query)
                query[sp] = [resolved["signature"]]
                url = parsed._replace(query=urlencode(query, doseq=True)).geturl()
                protected = False
            elif not signature:
                # Sem solver e sem assinatura: o URL gerado nao serve.
                return None

        if not url:
            return None

        # --- codecs ------------------------------------------------------
        # Os clients mobile nao devolvem os campos ``videoCodec`` /
        # ``audioCodec``: a informacao vem codificada no ``mimeType``
        # (ex.: 'video/mp4; codecs="avc1.42001E, mp4a.40.2"').
        vcodec = entry.get("videoCodec")
        acodec = entry.get("audioCodec")
        if vcodec is None or acodec is None:
            parsed_v, parsed_a = _parse_mime_codecs(mime)
            vcodec = vcodec or parsed_v
            acodec = acodec or parsed_a

        width = entry.get("width")
        height = entry.get("height")
        fps = entry.get("fps")
        content_length = entry.get("contentLength")
        approx_length = entry.get("approxDurationMs")

        mime_kind = mime.split("/", 1)[0].strip().lower()
        is_video = bool(vcodec and vcodec != "none") or mime_kind == "video"
        is_audio = bool(acodec and acodec != "none") or mime_kind == "audio"

        if is_video:
            media_type = MediaType.VIDEO
        elif is_audio:
            media_type = MediaType.AUDIO
        else:
            media_type = MediaType.UNKNOWN

        container = self._container_from_mime(mime)
        if container is Container.UNKNOWN:
            container = Container.MP4 if is_video else Container.M4A

        # Um formato cuja assinatura nao foi resolvida fica indisponivel
        # para nao entregar links que devolvem 403.
        unavailable = protected
        reason = "Assinatura nao resolvida (sem cipher solver)" if protected else None

        return StreamFormat(
            id=str(itag) if itag is not None else entry.get("qualityLabel", "yt"),
            url=url,
            media_type=media_type,
            container=container,
            protocol=MediaProtocol.HTTPS,
            width=width,
            height=height,
            fps=fps,
            tbr=(bitrate / 1000) if bitrate else None,
            abr=(bitrate / 1000) if (bitrate and not is_video) else None,
            vcodec=vcodec,
            acodec=acodec,
            filesize=int(content_length) if content_length and str(content_length).isdigit() else None,
            duration=float(approx_length) / 1000 if approx_length else None,
            unavailable=unavailable,
            unavailable_reason=reason,
            extractor_data={
                "itag": itag,
                "qualityLabel": entry.get("qualityLabel"),
                "init_range": entry.get("initRange"),
                "index_range": entry.get("indexRange"),
            },
        )

    @staticmethod
    def _container_from_mime(mime: str) -> Container:
        mime = mime.lower()
        if "webm" in mime:
            return Container.WEBM
        if "mp4" in mime:
            return Container.M4A if mime.startswith("audio/") else Container.MP4
        return Container.UNKNOWN

    @staticmethod
    def _thumbnail(video_id: str, micro_thumb: dict | None) -> str:
        if micro_thumb:
            thumbs = micro_thumb.get("thumbnails") or []
            if thumbs:
                return thumbs[-1].get("url", f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    @staticmethod
    def _format_date(value: str | None) -> str | None:
        if not value:
            return None
        match = re.match(r"^(\d{4})(\d{2})(\d{2})$", value)
        if match:
            return "-".join(match.groups())
        return value

    @staticmethod
    def _build_subtitles(raw: dict) -> dict[str, list[SubtitleTrack]]:
        captions = (raw.get("captions") or {}).get("playerCaptionsTracklistRenderer")
        if not captions:
            return {}
        tracks: dict[str, list[SubtitleTrack]] = {}
        for caption in captions.get("captionTracks") or []:
            language = caption.get("languageCode", "und")
            base_url = caption.get("baseUrl")
            if not base_url:
                continue
            # O YouTube serve WebVTT (``fmt=vtt``), que o FFmpeg lê
            # directamente. Extensão e formato coerentes.
            suffix = "&fmt=vtt"
            tracks.setdefault(language, []).append(
                SubtitleTrack(
                    language=language,
                    name=caption.get("name", {}).get("simpleText") if isinstance(caption.get("name"), dict) else None,
                    ext="vtt",
                    url=base_url + suffix,
                    automatic=caption.get("kind") == "asr",
                )
            )
        return tracks

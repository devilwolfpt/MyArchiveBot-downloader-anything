"""Custom Generic Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor genérico para páginas HTML.

É o fallback do routing: varre a página à procura de
  * ``<video>`` / ``<source>``;
  * OpenGraph (``og:video``, ``og:video:url``, ``og:video:secure_url``);
  * Twitter (``twitter:player:stream``);
  * JSON-LD (``VideoObject`` -> ``contentUrl``);
  * URLs de media embebidas em JSON inline (HLS/DASH).

É também a base que os extractores de plataforma reutilizam via
:meth:`BaseExtractor` helpers.
"""

import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.core.exceptions import ExtractionError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

META_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?P<key>[^"']+)["'][^>]*content\s*=\s*["'](?P<value>[^"']*)["']""",
    re.IGNORECASE,
)
META_RE_REVERSED = re.compile(
    r"""<meta[^>]+content\s*=\s*["'](?P<value>[^"']*)["'][^>]*(?:property|name)\s*=\s*["'](?P<key>[^"']+)["']""",
    re.IGNORECASE,
)
JSONLD_RE = re.compile(
    r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(?P<body>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
SCRIPT_RE = re.compile(
    r"""<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>""",
    re.IGNORECASE | re.DOTALL,
)
SCRIPT_TYPE_RE = re.compile(r"""\btype\s*=\s*["'](?P<type>[^"']+)["']""", re.IGNORECASE)
MEDIA_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\]+?\.(?:m3u8|m3u|mpd|mp4|m4v|webm|mov|mkv|flv|ts|mp3|m4a|aac|opus|ogg|wav|flac)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
HLS_URL_RE = re.compile(
    r"""["'](?P<url>(?:https?:)?//[^"']+?\.m3u8[^"']*)["']""", re.IGNORECASE
)
DASH_URL_RE = re.compile(
    r"""["'](?P<url>(?:https?:)?//[^"']+?\.mpd[^"']*)["']""", re.IGNORECASE
)
JW_PLAYER_SCRIPT_RE = re.compile(
    r"""^(?:https?:)?//(?:content\.jwplatform\.com|cdn\.jwplayer\.com)/players/[a-zA-Z0-9]{8}[^"'<>]*""",
    re.IGNORECASE,
)
JW_MEDIA_ID_RE = re.compile(r"^[a-zA-Z0-9]{8}$")
BRIGHTCOVE_PLAYER_RE = re.compile(
    r"""^(?:https?:)?//players\.brightcove\.net/\d+/[^/?#]+/index(?:\.html)?(?:\?|$)""",
    re.IGNORECASE,
)
KALTURA_PLAYER_RE = re.compile(
    r"""^(?:https?:)?//(?:(?:www|cdnapi(?:sec)?)\.)?kaltura\.com(?::\d+)?/"""
    r"""(?:index\.php/(?:kwidget|extwidget/preview)|html5/html5lib/[^/]+/mwEmbedFrame\.php)""",
    re.IGNORECASE,
)
KALTURA_WIDGET_RE = re.compile(r"""kWidget\.(?:thumb)?[Ee]mbed\s*\(\s*\{(?P<body>.*?)\}\s*\)""", re.DOTALL)


class _MediaTagParser(HTMLParser):
    """Extrai ``<video>``, ``<source>``, ``<iframe>`` e atributos relevantes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[tuple[str, str]] = []  # (src, type)
        self.iframes: list[str] = []
        self.provider_embeds: list[str] = []
        self.video_attrs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "source" and attr_map.get("src"):
            self.sources.append((attr_map["src"], attr_map.get("type", "")))
        elif tag in {"video", "video-js"}:
            if attr_map.get("src"):
                self.sources.append((attr_map["src"], attr_map.get("type", "")))
            self.video_attrs.update(attr_map)
            video_id = attr_map.get("data-video-id")
            account_id = attr_map.get("data-account")
            if video_id and account_id:
                player_id = attr_map.get("data-player") or "default"
                embed_id = attr_map.get("data-embed") or "default"
                self.provider_embeds.append(
                    "https://players.brightcove.net/"
                    f"{account_id}/{player_id}_{embed_id}/index.html"
                    f"?videoId={video_id}"
                )
        elif tag == "iframe" and attr_map.get("src"):
            iframe_url = attr_map["src"]
            self.iframes.append(iframe_url)
            if BRIGHTCOVE_PLAYER_RE.match(iframe_url) or KALTURA_PLAYER_RE.match(iframe_url):
                self.provider_embeds.append(iframe_url)
        elif tag == "script" and attr_map.get("src"):
            script_url = attr_map["src"]
            if JW_PLAYER_SCRIPT_RE.match(script_url):
                self.provider_embeds.append(script_url)
            elif BRIGHTCOVE_PLAYER_RE.match(script_url):
                self.provider_embeds.append(script_url)
        elif tag == "audio" and attr_map.get("src"):
            self.sources.append((attr_map["src"], attr_map.get("type", "")))

        jw_media_id = attr_map.get("data-video-jw-id", "")
        if JW_MEDIA_ID_RE.fullmatch(jw_media_id):
            self.provider_embeds.append(
                f"https://cdn.jwplayer.com/v2/media/{jw_media_id}"
            )


def extract_meta(html: str) -> dict[str, str]:
    """Meta tags OpenGraph/Twitter."""
    meta: dict[str, str] = {}
    for regex in (META_RE, META_RE_REVERSED):
        for match in regex.finditer(html):
            key = match.group("key").lower()
            value = match.group("value").strip()
            if value and key not in meta:
                meta[key] = value
    return meta


def extract_jsonld(html: str) -> list[dict]:
    """Objectos JSON-LD encontrados na página."""
    objects: list[dict] = []
    for match in JSONLD_RE.finditer(html):
        body = match.group("body").strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            # Alguns sites usam JSON com trailing commas ou multiplos objetos.
            try:
                parsed = json.loads(body.replace(",}", "}").replace(",]", "]"))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, list):
            objects.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def extract_player_sources(html: str) -> list[tuple[str, str]]:
    """Lê fontes de media em blocos JSON usados por players e frameworks."""
    decoder = json.JSONDecoder()
    sources: list[tuple[str, str]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            mime = next(
                (
                    item
                    for key, item in value.items()
                    if key.lower() in {"type", "mime", "mimetype", "contenttype"}
                    and isinstance(item, str)
                ),
                "",
            )
            for key, item in value.items():
                if key.lower() in {
                    "file",
                    "src",
                    "url",
                    "contenturl",
                    "manifesturl",
                    "streamurl",
                    "playbackurl",
                    "hls",
                    "dash",
                } and isinstance(item, str):
                    sources.append((item, mime))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for match in SCRIPT_RE.finditer(html):
        body = match.group("body").strip()
        if not body or len(body) > 1_000_000:
            continue

        attrs = match.group("attrs")
        type_match = SCRIPT_TYPE_RE.search(attrs)
        script_type = type_match.group("type").lower() if type_match else ""
        if script_type and script_type not in {
            "application/json",
            "application/ld+json",
            "application/vnd.player+json",
        } and not script_type.endswith("+json"):
            continue

        start = 0
        if not body.startswith(("{", "[")):
            if script_type:
                continue
            brace_positions = [
                position
                for position in (body.find("{"), body.find("["))
                if position >= 0
            ]
            if not brace_positions:
                continue
            start = min(brace_positions)

        try:
            parsed, _ = decoder.raw_decode(body, start)
        except json.JSONDecodeError:
            continue
        visit(parsed)
        if len(sources) >= 500:
            break

    return sources


class GenericHTMLExtractor(BaseExtractor):
    """Fallback genérico baseado em HTML."""

    name = "generic"
    priority = 900
    catch_all = True
    max_embed_depth = 3
    max_iframes_per_page = 5

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        return await self._extract_page(url, ctx, visited=set(), depth=0)

    async def _extract_page(
        self,
        url: str,
        ctx: ExtractContext,
        *,
        visited: set[str],
        depth: int,
    ) -> MediaInfo:
        visited = {*visited, url}
        result = await ctx.http.get(
            url,
            headers=ctx.request_headers(),
            max_bytes=ctx.max_page_bytes,
        )
        html = self._normalize_embedded_urls(result.text)
        final_url = result.url_final or url

        meta = extract_meta(html)
        jsonld = extract_jsonld(html)
        player_sources = extract_player_sources(html)
        tag_parser = _MediaTagParser()
        try:
            tag_parser.feed(html)
        except Exception as exc:  # noqa: BLE001 - HTML malformado é comum
            log.debug("Parser HTML falhou em %s: %s", url, exc)
        for widget_match in KALTURA_WIDGET_RE.finditer(html):
            widget_body = widget_match.group("body")
            partner_match = re.search(
                r"""['"]wid['"]\s*:\s*['"]_?(?P<partner>[\w-]+)['"]""",
                widget_body,
            )
            entry_match = re.search(
                r"""['"]entry_?[Ii]d['"]\s*:\s*['"](?P<entry>[\w-]+)['"]""",
                widget_body,
            )
            if partner_match and entry_match:
                tag_parser.provider_embeds.append(
                    "https://cdnapi.kaltura.com/index.php/kwidget"
                    f"/wid/_{partner_match.group('partner')}"
                    f"/entry_id/{entry_match.group('entry')}"
                )

        title = (
            meta.get("og:title")
            or meta.get("twitter:title")
            or self._first_jsonld(jsonld, "name")
            or self._title_tag(html)
            or "Sem título"
        )
        thumbnail = (
            meta.get("og:image")
            or meta.get("twitter:image")
            or self._first_jsonld(jsonld, "thumbnailUrl")
        )
        description = meta.get("og:description") or meta.get("description")
        duration = self._parse_duration(self._first_jsonld(jsonld, "duration"))

        formats: list[StreamFormat] = []
        seen: set[str] = set()

        def _add(
            candidate_url: str,
            mime: str = "",
            *,
            assume_video: bool = False,
        ) -> None:
            if not candidate_url:
                return
            absolute_url = urljoin(final_url, candidate_url.strip())
            if absolute_url in seen:
                return

            path = urlsplit(absolute_url).path.lower()
            mime = mime.lower()
            is_hls = path.endswith((".m3u8", ".m3u")) or "mpegurl" in mime
            is_dash = path.endswith(".mpd") or "dash+xml" in mime
            audio_extensions = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}
            extension = next(
                (suffix for suffix in audio_extensions | {
                    ".mp4", ".m4v", ".webm", ".mov", ".mkv", ".flv", ".ts", ".avi"
                } if path.endswith(suffix)),
                "",
            )
            is_audio = mime.startswith("audio/") or extension in audio_extensions
            is_video = mime.startswith("video/") or extension in {
                ".mp4", ".m4v", ".webm", ".mov", ".mkv", ".flv", ".ts", ".avi"
            }
            if not (is_hls or is_dash or is_audio or is_video or assume_video):
                return

            seen.add(absolute_url)
            if is_hls or is_dash:
                manifest_kind = "hls" if is_hls else "dash"
                formats.append(
                    StreamFormat(
                        id=f"manifest-{len(formats)}",
                        url=absolute_url,
                        media_type=MediaType.VIDEO,
                        container=Container.TS if is_hls else Container.MP4,
                        protocol=Protocol.HLS if is_hls else Protocol.DASH,
                        manifest_url=absolute_url,
                        manifest_kind=manifest_kind,
                    )
                )
            else:
                container = {
                    ".webm": Container.WEBM,
                    ".mov": Container.MOV,
                    ".mkv": Container.MKV,
                    ".flv": Container.FLV,
                    ".ts": Container.TS,
                    ".mp3": Container.MP3,
                    ".m4a": Container.M4A,
                    ".aac": Container.M4A,
                    ".opus": Container.OPUS,
                    ".ogg": Container.OGG,
                    ".wav": Container.WAV,
                }.get(extension, Container.MP4)
                formats.append(
                    StreamFormat(
                        id=f"direct-{len(formats)}",
                        url=absolute_url,
                        media_type=MediaType.AUDIO if is_audio else MediaType.VIDEO,
                        container=container,
                        protocol=Protocol.HTTPS if absolute_url.startswith("https") else Protocol.HTTP,
                        extractor_data={"mime": mime},
                    )
                )

        # 1) OpenGraph / Twitter
        for key in ("og:video:url", "og:video:secure_url", "og:video", "twitter:player:stream"):
            if meta.get(key):
                _add(meta[key], assume_video=True)

        # 2) JSON-LD
        for obj in jsonld:
            content_url = obj.get("contentUrl")
            if isinstance(content_url, str):
                _add(content_url, obj.get("encodingFormat", ""), assume_video=True)
            embed = obj.get("embedUrl")
            if isinstance(embed, str) and not content_url:
                _add(embed, assume_video=True)

        # 3) <video>/<source>
        for src, mime in tag_parser.sources:
            _add(src, mime)

        # 4) Configurações JSON de players (sources/file/src) e frameworks.
        for source_url, mime in player_sources:
            _add(source_url, mime)

        # 5) URLs de media em JSON inline
        for regex in (HLS_URL_RE, DASH_URL_RE):
            for match in regex.finditer(html):
                _add(match.group("url"))

        # 6) Qualquer URL de media solta
        for match in MEDIA_URL_RE.finditer(html):
            candidate = match.group(0)
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            _add(candidate)

        # Se encontrámos manifests, expande-os em variantes concretas.
        expanded: list[StreamFormat] = []
        embedded_info: MediaInfo | None = None
        for fmt in formats:
            if fmt.manifest_url:
                try:
                    variants = await self.formats_from_manifest(
                        fmt.manifest_url, ctx, headers=ctx.request_headers()
                    )
                    expanded.extend(variants)
                    if variants:
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.debug("Falha ao expandir manifesto %s: %s", fmt.manifest_url, exc)
            expanded.append(fmt)

        if not expanded:
            if depth < self.max_embed_depth:
                from .registry import registry

                embed_urls = list(dict.fromkeys([*tag_parser.iframes, *tag_parser.provider_embeds]))
                for embed_url in embed_urls[: self.max_iframes_per_page]:
                    embedded_url = urljoin(final_url, embed_url)
                    parts = urlsplit(embedded_url)
                    if (
                        parts.scheme not in {"http", "https"}
                        or embedded_url in visited
                    ):
                        continue

                    try:
                        extractor = registry.resolve(embedded_url)
                        # Se o extractor for catch_all (generic ou catalog), tratar como
                        # travessia recursiva de HTML para reutilizar o transporte HTTP
                        # já mockado nos testes e evitar chamadas reais à rede.
                        if isinstance(extractor, GenericHTMLExtractor) or extractor.catch_all:
                            embedded_info = await self._extract_page(
                                embedded_url,
                                ctx,
                                visited=visited,
                                depth=depth + 1,
                            )
                        else:
                            embedded_info = await extractor.extract(embedded_url, ctx)
                    except Exception as exc:  # noqa: BLE001 - outros embeds podem continuar a funcionar
                        log.debug(
                            "Não foi possível extrair um player embebido (%s): %s",
                            parts.hostname,
                            exc,
                        )
                        continue

                    if embedded_info.formats:
                        expanded = embedded_info.formats
                        break

        if not expanded:
            raise ExtractionError(
                f"Nenhuma media encontrada em {url}",
                details={"extractor": self.name},
            )

        return MediaInfo(
            extractor=embedded_info.extractor if embedded_info else self.name,
            webpage_url=final_url,
            original_url=url,
            title=(
                embedded_info.title
                if title.strip() == "Sem título" and embedded_info
                else title.strip()
            ),
            description=description or (embedded_info.description if embedded_info else None),
            thumbnail=thumbnail or (embedded_info.thumbnail if embedded_info else None),
            duration=duration or (embedded_info.duration if embedded_info else None),
            formats=expanded,
            subtitles=embedded_info.subtitles if embedded_info else {},
            extractor_data={
                "iframes": tag_parser.iframes[:10],
                "embedded_extractor": embedded_info.extractor if embedded_info else None,
                "embedded_host": (
                    urlsplit(expanded[0].url).hostname if expanded else None
                ),
            },
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_embedded_urls(html: str) -> str:
        """Normaliza escapes comuns de URLs em JSON e configurações JavaScript."""
        html = html.replace(r"\/", "/")
        html = re.sub(r"\\u002f", "/", html, flags=re.IGNORECASE)
        return re.sub(r"\\u0026", "&", html, flags=re.IGNORECASE)

    @staticmethod
    def _title_tag(html: str) -> str | None:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    @staticmethod
    def _first_jsonld(objects: list[dict], key: str) -> str | None:
        for obj in objects:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    url = first.get("url")
                    if isinstance(url, str):
                        return url
        return None

    @staticmethod
    def _parse_duration(value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        try:
            return float(value)
        except ValueError:
            pass
        match = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", value, re.IGNORECASE)
        if match:
            days, hours, minutes, seconds = match.groups()
            total = 0.0
            total += float(days or 0) * 86400
            total += float(hours or 0) * 3600
            total += float(minutes or 0) * 60
            total += float(seconds or 0)
            return total or None
        return None

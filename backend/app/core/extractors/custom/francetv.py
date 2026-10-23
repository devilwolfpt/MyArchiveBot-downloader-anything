"""Custom Francetv Extractor for MyArchive Downloader."""
from __future__ import annotations

"""Extractor de France TV / France Télévisions / france.tv.

Usa o endpoint oficial ``https://k7.ftven.fr/videos/<id>`` que
devolve o manifest HLS/DASH real, usando o identificador ``playerReplayId``
(ou ``siId``) presente no JSON da página.

Fluxo:

  1. extrai ``playerReplayId`` / ``siId`` da página (apenas UUID);
  2. chama ``/videos/<id>`` em desktop Chrome e mobile Safari, passando o
     ``domain`` real da página (o site valida a origem);
  3. interpreta o ``code`` de erro devolvido pela API (geo, DRM, directo);
  4. assina o manifest com o token **Akamai** (``hdfauth.ftven.fr/esi/TA``);
  5. expande HLS/DASH em formatos úteis;
  6. detecta bloqueio geográfico (403 + ``x-errortype: geo``).

Nota importante: o media do France TV é servido apenas a partir de França.
Fora de França o CDN responde ``403`` com ``x-errortype: geo``. Nesse caso a solução é
usar um IP de saída em França (VPN/proxy), configurável em
``MAD_PROXY_URL``.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

from app.core.exceptions import ExtractionError, GeoRestrictedError, HttpForbiddenError
from app.core.models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from app.core.extractors.base import BaseExtractor, ExtractContext
from app.core.extractors.custom.generic import extract_jsonld, extract_meta

log = logging.getLogger(__name__)

M3U8_RE = re.compile(r"""["'](?P<url>(?:https?:)?//[^"']+?\.m3u8[^"']*)["']""", re.IGNORECASE)
DATA_ATTR_RE = re.compile(
    r"""data-(?:video-url|m3u8|src|media-url|stream)\s*=\s*["'](?P<value>[^"']+)["']""",
    re.IGNORECASE,
)
INLINE_JSON_RE = re.compile(
    r"""<script[^>]*type\s*=\s*["']application/json["'][^>]*>(?P<body>.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)

#: UUID (o identificador de video do France TV tem sempre este formato).
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

#: Campos da página que contêm o identificador do video.
ID_FIELDS = ("playerReplayId", "siId")

#: Perfis de cliente pedidos à API de vídeo.
API_PROFILES: tuple[tuple[str, str], ...] = (("desktop", "chrome"), ("mobile", "safari"))

#: Códigos de erro da API oficial k7.ftven.fr.
API_CODES: dict[int, str] = {
    2003: "Vídeo indisponível (transmissão em direto terminada)",
    2004: "Vídeo indisponível (transmissão em direto não disponível)",
    2009: "Vídeo restrito geograficamente a França",
    2015: "Vídeo protegido por DRM — não é descarregável",
    2017: "Vídeo protegido por DRM — não é descarregável",
    2019: "Vídeo protegido por DRM (disponível só na aplicação oficial)",
}


@dataclass(slots=True)
class _ApiVideo:
    """Vídeo devolvido pela API oficial do France TV."""

    video_id: str
    url: str | None = None
    token: str | None = None
    format: str | None = None
    is_live: bool = False
    duration: float | None = None
    meta: dict = field(default_factory=dict)
    code: int | None = None
    error: str | None = None


class FranceTVExtractor(BaseExtractor):
    name = "francetv"
    priority = 40
    patterns = [
        r"^https?://(?:www\.|m\.)?francetv\.fr/[\w\-/]+",
        r"^https?://(?:www\.|m\.)?france\.tv/[\w\-/]+",
        r"^https?://[\w\-.]*francetvinfo[\w\-.]*/[\w\-/]+",
        r"^https?://[\w\-.]*france3[\w\-.]*/[\w\-/]+",
        r"^https?://[\w\-.]*france2[\w\-.]*/[\w\-/]+",
        r"^https?://[\w\-.]*france5[\w\-.]*/[\w\-/]+",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        result = await ctx.http.get(url, headers=ctx.request_headers(referer=url))
        html = result.text
        final_url = result.url_final or url
        page_headers = ctx.request_headers(referer=final_url)
        domain = self._domain_of(final_url)

        page_meta = extract_meta(html)
        title = page_meta.get("og:title") or page_meta.get("twitter:title") or "France TV"
        description = page_meta.get("og:description")
        thumbnail = page_meta.get("og:image") or page_meta.get("twitter:image")

        # --- 1) API oficial ------------------------------------------------
        api_videos: list[_ApiVideo] = []
        api_error: ExtractionError | None = None
        for video_id in self._extract_video_ids(html):
            for profile in API_PROFILES:
                api_video = await self._query_api(
                    video_id, profile, ctx, domain=domain, referer=final_url
                )
                if api_video is None:
                    continue
                api_videos.append(api_video)
                if api_video.error:
                    api_error = self._error_for(api_video, video_id)
                    break
                break  # um perfil que responda com video chega

        if api_videos and all(v.error for v in api_videos):
            # Todos os identificadores devolveram erro ->(propaga a mensagem
            # mais especifica (geo / DRM / directo).
            raise api_error or ExtractionError("France TV: conteúdo indisponível")

        # --- 2) Manifestos -------------------------------------------------
        manifest_candidates: list[str] = []
        for api_video in api_videos:
            if api_video.error:
                continue
            signed = await self._sign_manifest(api_video, ctx, headers=page_headers)
            if signed:
                manifest_candidates.append(signed)
            elif api_video.url:
                manifest_candidates.append(api_video.url)

        # --- 3) Metadados da API ------------------------------------------
        api_meta = next((v.meta for v in api_videos if v.meta), {})
        if api_meta:
            title = api_meta.get("title") or title
            if api_meta.get("additional_title"):
                title = f"{title} — {api_meta['additional_title']}"
            thumbnail = api_meta.get("image_url") or thumbnail
            description = api_meta.get("description") or description
        is_live = any(v.is_live for v in api_videos)
        duration = next((v.duration for v in api_videos if v.duration), None)

        # --- 4) Fallbacks legacy da pagina --------------------------------
        legacy: list[str] = []
        for match in INLINE_JSON_RE.finditer(html):
            body = match.group("body")
            for m in M3U8_RE.finditer(body):
                legacy.append(m.group("url"))
            try:
                self._harvest_json(body, legacy)
            except Exception:  # noqa: BLE001
                pass
        for match in DATA_ATTR_RE.finditer(html):
            legacy.append(match.group("value"))
        for obj in extract_jsonld(html):
            content_url = obj.get("contentUrl")
            if isinstance(content_url, str):
                legacy.append(content_url)
        for match in M3U8_RE.finditer(html):
            legacy.append(match.group("url"))

        candidates = self._normalize(manifest_candidates + legacy, base=final_url)
        if not candidates:
            raise ExtractionError(
                "Nenhum stream encontrado na página France TV", details={"url": url}
            )

        # --- 5) Expansao dos manifestos -----------------------------------
        formats: list[StreamFormat] = []
        geo_blocked = False
        for candidate in candidates:
            if ".m3u8" in candidate.lower() or ".mpd" in candidate.lower():
                try:
                    variants = await self.formats_from_manifest(
                        candidate, ctx, headers=page_headers
                    )
                    formats.extend(variants)
                    continue
                except GeoRestrictedError:
                    geo_blocked = True
                    break
                except HttpForbiddenError as exc:
                    if (exc.details or {}).get("x_errortype") == "geo":
                        geo_blocked = True
                        break
                    log.debug("Falha ao expandir manifesto France TV %s: %s", candidate, exc)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Falha ao expandir manifesto France TV %s: %s", candidate, exc)
            formats.append(
                StreamFormat(
                    id=f"ftv-{len(formats)}",
                    url=candidate,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                )
            )

        if geo_blocked:
            raise GeoRestrictedError(
                "O France TV só serve este conteúdo a partir de França. "
                "O teu IP de saída está fora de França — usa uma VPN ou um "
                "proxy com IP francês (MAD_PROXY_URL) para o descarregar.",
                details={"country": "FR"},
            )

        if not formats:
            raise ExtractionError("France TV: nenhum formato utilizável", details={"url": url})

        return MediaInfo(
            extractor=self.name,
            webpage_url=final_url,
            original_url=url,
            title=title,
            description=description,
            thumbnail=thumbnail,
            duration=duration or self._parse_duration(page_meta.get("video:duration")),
            uploader=page_meta.get("og:site_name"),
            is_live=is_live,
            formats=formats,
        )

    # ------------------------------------------------------------------ #
    # Identificadores
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_video_ids(html: str) -> list[str]:
        """Extrai os identificadores de video do JSON da pagina.

        O site actual embute o JSON com aspas escapadas (``\\"playerReplayId\\"``),
        por isso procura-se o UUID na proximidade do nome do campo em vez de
        depender de um regex literal.

        Só são aceites UUIDs: o ``mediaVideoId`` (numérico) nao e um
        identificador valido para a API.
        """
        ids: list[str] = []
        for field_name in ID_FIELDS:
            lowered = html.lower()
            start = lowered.find(field_name.lower())
            while start != -1:
                snippet = html[start : start + 260]
                match = UUID_RE.search(snippet)
                if match:
                    value = match.group(0).lower()
                    if value not in ids:
                        ids.append(value)
                start = lowered.find(field_name.lower(), start + 1)
        return ids

    @staticmethod
    def _domain_of(url: str) -> str:
        """Hostname da pagina (a API valida a origem do pedido)."""
        host = urlsplit(url).hostname or "www.france.tv"
        return host.lower()

    # ------------------------------------------------------------------ #
    # API oficial
    # ------------------------------------------------------------------ #
    async def _query_api(
        self,
        video_id: str,
        profile: tuple[str, str],
        ctx: ExtractContext,
        *,
        domain: str,
        referer: str | None,
    ) -> _ApiVideo | None:
        """Consulta a API oficial para um identificador."""
        device_type, browser = profile
        url = (
            f"https://k7.ftven.fr/videos/{quote(video_id)}"
            f"?device_type={device_type}&browser={browser}&domain={quote(domain)}"
        )
        headers = ctx.request_headers(referer=referer)
        try:
            result = await ctx.http.get(url, headers=headers)
            payload = json.loads(result.text)
        except Exception as exc:  # noqa: BLE001
            log.debug("France TV API falhou para %s (%s): %s", video_id, profile[0], exc)
            return None

        if not isinstance(payload, dict):
            return None

        code = payload.get("code")
        if code is not None:
            message = API_CODES.get(int(code), str(payload.get("message") or "erro desconhecido"))
            log.info("France TV API devolveu code=%s (%s) para %s", code, message, video_id)
            return _ApiVideo(video_id=video_id, code=int(code), error=message)

        video = payload.get("video") or {}
        if not isinstance(video, dict):
            video = {}
        token = (video.get("token") or {}).get("akamai")
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

        return _ApiVideo(
            video_id=video_id,
            url=video.get("url") if isinstance(video.get("url"), str) else None,
            token=token if isinstance(token, str) else None,
            format=video.get("format"),
            is_live=bool(video.get("is_live")),
            duration=self._as_float(video.get("duration")),
            meta=meta,
        )

    @staticmethod
    def _error_for(api_video: _ApiVideo, video_id: str) -> ExtractionError:
        """Converte um codigo de erro da API numa excecao especifica."""
        message = api_video.error or "conteúdo indisponível"
        details = {"code": api_video.code, "video_id": video_id}
        if api_video.code == 2009:
            return GeoRestrictedError(
                "O France TV só serve este conteúdo a partir de França "
                "(restrição geográfica). Usa uma VPN ou um proxy com IP "
                "francês (MAD_PROXY_URL).",
                details=details,
            )
        if api_video.code in (2003, 2004):
            return ExtractionError(
                f"France TV: {message}", details=details
            )
        return ExtractionError(f"France TV: {message}", details=details)

    async def _sign_manifest(
        self,
        api_video: _ApiVideo,
        ctx: ExtractContext,
        *,
        headers: dict[str, str],
    ) -> str | None:
        """Assina o manifest com o token Akamai.

        O Akamai devolve um URL prefixado com um token (``hdnea=...``) que
        tem de ser anexado ao manifest; sem ele o CDN responde 403.
        """
        if not api_video.url:
            return None
        if not api_video.token:
            return api_video.url
        try:
            token_result = await ctx.http.get(
                api_video.token,
                headers=headers,
                params={"format": "json", "url": api_video.url},
            )
            payload = json.loads(token_result.text)
        except Exception as exc:  # noqa: BLE001
            log.debug("France TV: token Akamai falhou (%s): %s", api_video.video_id, exc)
            return api_video.url

        signed = payload.get("url") or payload.get("stream")
        return signed if isinstance(signed, str) and signed else api_video.url

    # ------------------------------------------------------------------ #
    # Utilitarios
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(candidates: list[str], *, base: str) -> list[str]:
        """Normaliza e deduplica a lista de candidatos."""
        out: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            candidate = candidate.strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            absolute = urljoin(base, candidate)
            if absolute in seen:
                continue
            seen.add(absolute)
            out.append(absolute)
        return out

    def _harvest_json(self, body: str, candidates: list[str]) -> None:
        """Percorre JSON arbitrario a procura de strings que terminem em .m3u8."""
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return

        stack = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str) and ".m3u8" in node.lower():
                candidates.append(node)

    @staticmethod
    def _as_float(value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_duration(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

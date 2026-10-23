"""Serviço de extracção de metadados, com cache, fallback e normalização.

Envolve o registo de extractores e lida com:
  * selecção do extractor (por URL ou por nome explícito);
  * propagação de cookies/headers do cliente;
  * cadeia de fallback: extractor nativo → catálogo universal;
  * cache TTL em memória (evita re-scraping em pesquisas repetidas);
  * tradução de erros para as excepções de negócio.
"""
from __future__ import annotations

import logging
import time
from http.cookiejar import CookieJar
from typing import Any

from ..config import Settings
from ..core.exceptions import ExtractionError, UnsupportedUrlError
from ..core.extractors import ExtractContext, registry
from ..core.http.client import HttpClient
from ..core.http.cookies import (
    load_cookies_from_browser,
    load_netscape_cookie_file,
    load_netscape_cookiejar,
    merge_cookies,
)
from ..core.models import MediaInfo

log = logging.getLogger(__name__)


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: MediaInfo, ttl: float) -> None:
        self.value = value
        self.expires_at = time.time() + ttl


class ExtractionService:
    """Facade sobre os extractores, com fallback automático para o catálogo."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------------ #
    def _cache_key(
        self, url: str, headers: dict[str, str], cookies: dict[str, str], extractor: str | None
    ) -> str:
        return f"{extractor or 'auto'}|{url}|{sorted(headers.items())}|{sorted(cookies.items())}"

    def invalidate(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ #
    async def extract(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        cookies_file: str | None = None,
        cookies_from_browser: str | None = None,
        cookie_jar: CookieJar | None = None,
        preferred_extractor: str | None = None,
        use_cache: bool = True,
    ) -> MediaInfo:
        """Extrai metadados e formatos de um URL, com fallback automático."""
        url = url.strip()
        if not url:
            raise ExtractionError("URL vazio")

        effective_cookie_file = cookies_file
        if not effective_cookie_file and self.settings.active_cookie_file:
            effective_cookie_file = str(self.settings.active_cookie_file)

        extra_cookies: dict[str, str] = {}
        if effective_cookie_file:
            try:
                extra_cookies.update(load_netscape_cookie_file(effective_cookie_file))
                if cookie_jar is None:
                    cookie_jar = load_netscape_cookiejar(effective_cookie_file)
            except Exception as exc:
                if cookies_file:
                    raise ExtractionError(f"Ficheiro de cookies inválido: {cookies_file}") from exc
                log.warning("Ficheiro de cookies ativo ignorado devido a erro: %s", exc)

        if cookies_from_browser:
            try:
                if cookie_jar is None:
                    cookie_jar = load_cookies_from_browser(cookies_from_browser, url)
            except (RuntimeError, ValueError) as exc:
                raise ExtractionError(str(exc)) from exc

        merged_cookies = merge_cookies(cookies, extra_cookies)
        merged_headers = dict(headers or {})

        # Selecciona o extractor principal
        if preferred_extractor:
            extractor = registry.get(preferred_extractor)
            if extractor is None:
                raise UnsupportedUrlError(
                    f"Extractor desconhecido: {preferred_extractor}",
                    details={"available": [e.name for e in registry.all()]},
                )
        else:
            extractor = registry.resolve(url)

        # Extractors cujas URLs de media estão presas ao IP de origem
        # (YouTube) não são cacheados: uma URL antiga em cache provoca 403.
        cache_enabled = bool(
            use_cache
            and self.settings.cache_enabled
            and extractor.cacheable
            and not merged_cookies
            and cookie_jar is None
            and not merged_headers
        )

        key = self._cache_key(url, {}, {}, preferred_extractor)
        if cache_enabled:
            entry = self._cache.get(key)
            if entry and entry.expires_at > time.time():
                log.debug("Cache hit para %s", url)
                return entry.value

        info = await self._run_extractor(
            extractor,
            url,
            merged_headers,
            merged_cookies,
            cookie_jar,
            cookie_file=effective_cookie_file,
        )

        if not info.extractor:
            info.extractor = extractor.name
        if cache_enabled:
            self._cache[key] = _CacheEntry(info, self.settings.cache_ttl_seconds)
            self._purge_cache()
        return info

    # ------------------------------------------------------------------ #
    async def _run_extractor(
        self,
        extractor,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str],
        cookie_jar: CookieJar | None,
        cookie_file: str | None = None,
    ) -> MediaInfo:
        """Tenta o extractor principal; se falhar, faz fallback para o catálogo."""
        async with HttpClient(
            self.settings,
            headers=headers,
            cookies=cookies,
            cookie_jar=cookie_jar,
            cookie_scope_url=url,
        ) as http:
            ctx = ExtractContext(
                http=http,
                settings=self.settings,
                headers=headers,
                cookies=cookies,
                cookie_file=cookie_file,
                cookie_jar=cookie_jar,
                referer=url,
            )
            try:
                info = await extractor.extract(url, ctx)
                return info
            except ExtractionError:
                # Se já é o catálogo, não há mais fallback — propaga o erro
                if extractor.name == "catalog":
                    raise
                # Extractor nativo falhou → tenta o catálogo
                log.warning(
                    "Extractor nativo '%s' falhou para %s — a tentar catálogo universal...",
                    extractor.name,
                    url,
                )
            except UnsupportedUrlError:
                raise
            except Exception as exc:  # noqa: BLE001
                if extractor.name == "catalog":
                    raise ExtractionError(
                        f"Falha na extracção: {exc}",
                        details={"extractor": extractor.name, "url": url},
                    ) from exc
                log.warning(
                    "Extractor nativo '%s' falhou para %s (%s) — a tentar catálogo universal...",
                    extractor.name,
                    url,
                    exc,
                )

            # Fallback: catálogo universal
            catalog = registry.get("catalog")
            if catalog is None:
                raise ExtractionError(
                    f"Extractor '{extractor.name}' falhou e o catálogo não está disponível.",
                    details={"url": url},
                )
            try:
                log.info("Catálogo a tratar %s", url)
                info = await catalog.extract(url, ctx)
                return info
            except ExtractionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ExtractionError(
                    f"Falha na extracção: {exc}",
                    details={"extractor": "catalog", "url": url},
                ) from exc

    # ------------------------------------------------------------------ #
    def _purge_cache(self) -> None:
        now = time.time()
        for key in [k for k, v in self._cache.items() if v.expires_at < now]:
            self._cache.pop(key, None)

    def stats(self) -> dict[str, Any]:
        return {
            "cached_entries": len(self._cache),
            "extractors": [
                {"name": e.name, "priority": e.priority, "patterns": e.patterns}
                for e in registry.all()
            ],
        }

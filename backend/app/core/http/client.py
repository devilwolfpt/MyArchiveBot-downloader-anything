"""Cliente HTTP assíncrono partilhado por todos os extractores.

Características:
  * Pool de conexões partilhado (``httpx.AsyncClient``) com limites configuráveis;
  * Rotação de User-Agent por request;
  * Cookies de sessão persistidos num ``httpx.Cookies`` por sessão;
  * Retry com backoff exponencial para 429/5xx e 403 (token expirado);
  * Hook ``token_provider`` para regenerar tokens/assinaturas em 403;
  * Protecção SSRF: bloqueio de hosts privados por omissão.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from ...config import Settings
from ..exceptions import (
    HttpForbiddenError,
    HttpRequestError,
    HttpTooManyRequestsError,
    SSRFBlockedError,
)
from .headers import HeaderFactory

log = logging.getLogger(__name__)

#: Callback que recebe o request e devolve headers actualizados (ex.: nova `sig`).
TokenProvider = Callable[[str], Awaitable[dict[str, str] | None]]


def _total_from_content_range(headers: dict[str, str]) -> int | None:
    """Extrai o tamanho total de um header ``Content-Range``."""
    value = headers.get("content-range")
    if value and "/" in value:
        total = value.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    return None


@dataclass(slots=True)
class HttpResult:
    """Resposta normalizada."""

    url: str
    status_code: int
    content: bytes = b""
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url_final: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def is_private_host(host: str) -> bool:
    """Detecta hosts RFC1918 / loopback / link-local / metadata endpoints."""
    if not host:
        return True
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    if lowered.endswith(".local") or lowered.endswith(".internal"):
        return True
    # AWS/GCP/Azure metadata
    if lowered in {"169.254.169.254", "metadata.google.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        try:
            infos = socket.getaddrinfo(lowered, None)
        except socket.gaierror:
            return False  # não resolve -> deixamos o cliente HTTP lidar
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return True
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class HttpClient:
    """Wrapper fino sobre ``httpx.AsyncClient`` com política de retry."""

    def __init__(
        self,
        settings: Settings,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        cookie_jar: CookieJar | httpx.Cookies | None = None,
        cookie_scope_url: str | None = None,
        token_provider: TokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.header_factory = HeaderFactory(settings)
        self.cookies = httpx.Cookies(cookie_jar) if cookie_jar is not None else httpx.Cookies()
        if cookies:
            host = urlsplit(cookie_scope_url or "").hostname
            if host:
                for name, value in cookies.items():
                    self.cookies.jar.set_cookie(
                        Cookie(
                            version=0,
                            name=name,
                            value=value,
                            port=None,
                            port_specified=False,
                            domain=host,
                            domain_specified=False,
                            domain_initial_dot=False,
                            path="/",
                            path_specified=True,
                            secure=(urlsplit(cookie_scope_url or "").scheme == "https"),
                            expires=None,
                            discard=True,
                            comment=None,
                            comment_url=None,
                            rest={},
                            rfc2109=False,
                        )
                    )
            else:
                for name, value in cookies.items():
                    self.cookies.set(name, value)
        self.token_provider = token_provider
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.settings.http_timeout,
                    connect=self.settings.http_connect_timeout,
                    read=self.settings.http_timeout,
                ),
                limits=httpx.Limits(
                    max_connections=self.settings.http_max_connections,
                    max_keepalive_connections=self.settings.http_max_connections // 2 or 1,
                    keepalive_expiry=self.settings.http_keepalive_expiry,
                ),
                follow_redirects=self.settings.http_follow_redirects,
                max_redirects=self.settings.http_max_redirects,
                verify=self.settings.http_verify_tls,
                proxy=self.settings.proxy_url or None,
                transport=self._transport,
                cookies=self.cookies,
                http2=False,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient não inicializado; use `async with` ou `start()`")
        return self._client

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _assert_allowed(self, url: str) -> None:
        if self.settings.allow_private_urls:
            return
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise SSRFBlockedError(f"Esquema não suportado: {parts.scheme!r}")
        if is_private_host(parts.hostname or ""):
            raise SSRFBlockedError(f"Destino bloqueado (rede privada): {parts.hostname}")

    def _build_headers(
        self,
        *,
        referer: str | None,
        extra: dict[str, str] | None,
        binary: bool,
    ) -> dict[str, str]:
        return self.header_factory.build(referer=referer, extra=extra, binary=binary)

    async def _sleep_backoff(self, attempt: int) -> None:
        base = self.settings.retry_backoff_base ** attempt
        delay = min(base, self.settings.retry_backoff_max)
        delay *= 0.75 + random.random() * 0.5  # jitter
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        content: Any = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        binary: bool = False,
        max_bytes: int | None = None,
    ) -> HttpResult:
        """Executa um request com retry automático.

        ``max_bytes`` limita a leitura da resposta (protege contra respostas
        gigantes/maliciosas).
        """
        await self.start()
        self._assert_allowed(url)

        attempts = self.settings.max_retries
        last_error: Exception | None = None

        for attempt in range(attempts):
            request_headers = self._build_headers(referer=referer, extra=headers, binary=binary)
            try:
                response = await self.client.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    data=data,
                    json=json,
                    content=content,
                    timeout=timeout or self.settings.http_timeout,
                    follow_redirects=allow_redirects,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning("Timeout em %s (tentativa %d/%d)", url, attempt + 1, attempts)
                await self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Erro HTTP %s: %s (tentativa %d/%d)", url, exc, attempt + 1, attempts)
                await self._sleep_backoff(attempt)
                continue

            # 403: o token/assinatura pode ter expirado -> tenta renovar.
            if response.status_code == 403 and self.token_provider is not None:
                log.info("403 em %s; a tentar renovar token", url)
                try:
                    refreshed = await self.token_provider(url)
                except Exception:  # pragma: no cover - o provider é do extractor
                    refreshed = None
                if refreshed:
                    merged = dict(request_headers)
                    merged.update(refreshed)
                    response = await self.client.request(
                        method,
                        url,
                        headers=merged,
                        params=params,
                        data=data,
                        json=json,
                        content=content,
                        timeout=timeout or self.settings.http_timeout,
                        follow_redirects=allow_redirects,
                    )

            if response.status_code == 429:
                last_error = HttpTooManyRequestsError(
                    f"Rate limited em {url}", details={"retry_after": response.headers.get("Retry-After")}
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 403:
                # O Akamai (usado pelo France TV) sinaliza bloqueio
                # geografico com ``x-errortype: geo`` — é informação
                # valiosa para o extractor dar uma mensagem clara.
                last_error = HttpForbiddenError(
                    f"Acesso negado (403) em {url}",
                    details={"x_errortype": response.headers.get("x-errortype")},
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 500:
                last_error = HttpRequestError(
                    f"Erro {response.status_code} em {url}", details={"status": response.status_code}
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                raise HttpRequestError(
                    f"Erro {response.status_code} em {url}", details={"status": response.status_code}
                )

            body = response.content
            if max_bytes is not None and len(body) > max_bytes:
                body = body[:max_bytes]

            return HttpResult(
                url=url,
                status_code=response.status_code,
                content=body,
                text=body.decode("utf-8", errors="replace"),
                headers=dict(response.headers),
                url_final=str(response.url),
            )

        if isinstance(last_error, HttpForbiddenError):
            raise last_error
        if last_error is not None and "403" in str(last_error).upper():
            raise HttpForbiddenError(f"Acesso negado (403) em {url}") from last_error
        raise HttpRequestError(
            f"Falha após {attempts} tentativas: {url}",
            details={"cause": str(last_error)},
        ) from last_error

    async def get(self, url: str, **kwargs: Any) -> HttpResult:
        return await self.request("GET", url, **kwargs)

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        """POST JSON e devolve o JSON já parseado."""
        result = await self.request("POST", url, json=payload, **kwargs)
        try:
            import json as _json

            return _json.loads(result.text)
        except ValueError as exc:
            raise HttpRequestError(
                f"Resposta JSON inválida de {url}", details={"body": result.text[:400]}
            ) from exc

    async def head(self, url: str, **kwargs: Any) -> HttpResult:
        """HEAD com fallback para GET quando o servidor não suporta HEAD."""
        try:
            return await self.request("HEAD", url, **kwargs)
        except HttpRequestError:
            kwargs.pop("allow_redirects", None)
            return await self.request("GET", url, **kwargs)

    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        range_start: int | None = None,
        range_end: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[tuple[httpx.Response, AsyncIterator[bytes]]]:
        """Stream de resposta sem buffering. Usado pelo chunk downloader.

        Devolve o objecto ``httpx.Response`` (para ler headers/status) e o
        iterador de bytes.
        """
        await self.start()
        self._assert_allowed(url)

        request_headers = self._build_headers(referer=referer, extra=headers, binary=True)
        if range_start is not None:
            value = f"bytes={range_start}-"
            if range_end is not None:
                value = f"bytes={range_start}-{range_end}"
            request_headers["Range"] = value

        req = self.client.build_request(
            method,
            url,
            headers=request_headers,
            timeout=timeout or self.settings.download_timeout,
        )
        response = await self.client.send(req, stream=True, follow_redirects=True)
        if response.status_code == 403:
            await response.aclose()
            raise HttpForbiddenError(
                f"Resposta 403 do host {urlsplit(url).hostname or 'desconhecido'}",
                details={"x_errortype": response.headers.get("x-errortype")},
            )
        if response.status_code == 429:
            await response.aclose()
            raise HttpTooManyRequestsError(f"Resposta 429 em {url}")
        if response.status_code >= 500:
            await response.aclose()
            raise HttpRequestError(f"Resposta {response.status_code} em {url}")
        return response, response.aiter_bytes()

    async def content_length(self, url: str, *, headers: dict[str, str] | None = None) -> int | None:
        """Tamanho do recurso em bytes (``None`` se desconhecido).

        Alguns CDNs — como o do YouTube, cujos URLs são assinados com
        ``rqh=1`` — recusam com HTTP 403 qualquer pedido sem header
        ``Range``. Nesses casos sonda-se com ``Range: bytes=0-0`` e lê-se o
        total no ``Content-Range``.
        """
        try:
            result = await self.head(url, headers=headers)
            length = result.headers.get("content-length")
            if length and length.isdigit():
                return int(length)
            total = _total_from_content_range(result.headers)
            if total is not None:
                return total
        except Exception as exc:  # noqa: BLE001
            # 403 (rqh=1), 405 (sem HEAD) ou timeout -> tenta a sonda com Range.
            log.debug("HEAD falhou em %s: %s", url, exc)

        try:
            response, _iterator = await self.stream(
                "GET", url, headers=headers, range_start=0, range_end=0
            )
            try:
                return _total_from_content_range(response.headers)
            finally:
                await response.aclose()
        except Exception as exc:  # noqa: BLE001
            log.debug("Sonda com Range falhou em %s: %s", url, exc)
            return None

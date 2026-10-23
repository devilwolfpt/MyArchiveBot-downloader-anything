"""Hierarquia de exceções do core.

Todas as exceções de negócio derivam de ``DownloaderError`` e expõem um
``code`` estável, que a camada HTTP traduz para um status code e um payload
JSON consistente.
"""
from __future__ import annotations


class DownloaderError(Exception):
    """Base de todas as erros de negócio."""

    code = "downloader_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class UnsupportedUrlError(DownloaderError):
    code = "unsupported_url"
    http_status = 400


class ExtractionError(DownloaderError):
    code = "extraction_failed"
    http_status = 422


class NoFormatFoundError(DownloaderError):
    code = "no_format_found"
    http_status = 422


class ManifestParseError(DownloaderError):
    code = "manifest_parse_error"
    http_status = 422


class GeoRestrictedError(ExtractionError):
    """O conteúdo está restrito geograficamente.

    Alguns emissores (o France TV é o caso típico) só servem o media a
    partir de France. O CDN responde ``403`` com o header ``x-errortype:
    geo``, que é a forma canónica de o sinalizar no MyArchive Downloader.

    A solução prática é sair de França (VPN/proxy com IP francês).
    """

    code = "geo_restricted"
    http_status = 451


class HttpRequestError(DownloaderError):
    code = "http_request_failed"
    http_status = 502


class HttpForbiddenError(HttpRequestError):
    code = "http_forbidden"
    http_status = 403


class HttpTooManyRequestsError(HttpRequestError):
    code = "http_rate_limited"
    http_status = 429


class DownloadError(DownloaderError):
    code = "download_failed"
    http_status = 500


class MediaUrlExpiredError(DownloadError):
    """O servidor de media recusou um pedido (HTTP 403).

    A URL ou o client de extração pode não ser compatível com a política
    atual do servidor. A reextração permite tentar com um plano renovado.
    """

    code = "media_url_expired"
    http_status = 502


class DownloadCancelledError(DownloadError):
    code = "download_cancelled"
    http_status = 499


class FileTooLargeError(DownloadError):
    code = "file_too_large"
    http_status = 413


class JobNotFoundError(DownloaderError):
    code = "job_not_found"
    http_status = 404


class JobCancelledError(DownloaderError):
    code = "job_cancelled"
    http_status = 409


class PostProcessError(DownloaderError):
    code = "postprocess_failed"
    http_status = 500


class UnauthorizedError(DownloaderError):
    code = "unauthorized"
    http_status = 401


class RateLimitError(DownloaderError):
    code = "rate_limited"
    http_status = 429


class SSRFBlockedError(DownloaderError):
    code = "ssrf_blocked"
    http_status = 400

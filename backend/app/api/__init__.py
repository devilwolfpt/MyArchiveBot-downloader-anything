"""Camada HTTP (FastAPI)."""
from .deps import get_download_service, get_extraction_service, get_job_manager, rate_limit, require_api_key

__all__ = [
    "get_download_service",
    "get_extraction_service",
    "get_job_manager",
    "rate_limit",
    "require_api_key",
]

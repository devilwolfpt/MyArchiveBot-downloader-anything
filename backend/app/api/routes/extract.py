"""Rotas de extracção de metadados."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ...core.models import ExtractRequest, MediaInfo
from ...services.extraction import ExtractionService
from ..deps import get_extraction_service, rate_limit, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["extract"])


@router.post(
    "/extract-info",
    response_model=MediaInfo,
    summary="Extrai metadados e formatos de um URL de vídeo",
)
async def extract_info(
    payload: ExtractRequest,
    service: ExtractionService = Depends(get_extraction_service),
    _: None = Depends(require_api_key),
    __: None = Depends(rate_limit),
) -> MediaInfo:
    """Recebe um URL e devolve metadados + lista de formatos disponíveis.

    Exemplo de resposta::

        {
          "title": "Um vídeo",
          "duration": 213.0,
          "extractor": "youtube",
          "formats": [
            {"id": "137", "height": 1080, "container": "mp4", "url": "..."}
          ]
        }
    """
    return await service.extract(
        payload.url,
        headers=payload.headers,
        cookies=payload.cookies,
        cookies_from_browser=payload.cookies_from_browser,
        preferred_extractor=payload.preferred_extractor,
    )


@router.get("/formats", summary="Lista os extractores disponíveis")
async def list_extractors(
    service: ExtractionService = Depends(get_extraction_service),
    _: None = Depends(require_api_key),
) -> dict:
    """Devolve os extractores registados (útil para debugging e UI dinâmica)."""
    return service.stats()

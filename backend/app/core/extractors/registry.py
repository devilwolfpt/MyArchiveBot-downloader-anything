"""Registo de extractores: routing por regex e prioridade.

O registo é carregado de forma lazy — importar extractors não deve custar
regex compilation de plataformas que não vão ser usadas.
"""
from __future__ import annotations

import logging
from typing import Iterable

from ..exceptions import UnsupportedUrlError
from .base import BaseExtractor

log = logging.getLogger(__name__)


class ExtractorRegistry:
    """Mapeia URLs para extractores."""

    def __init__(self) -> None:
        self._extractors: dict[str, BaseExtractor] = {}
        self._order: list[BaseExtractor] = []
        self._loaded = False

    # ------------------------------------------------------------------ #
    def register(self, extractor: BaseExtractor | type[BaseExtractor]) -> BaseExtractor:
        """Regista um extractor (instância ou classe)."""
        instance = extractor() if isinstance(extractor, type) else extractor
        if instance.name in self._extractors:
            log.debug("Extractor %s já registado; a substituir", instance.name)
        self._extractors[instance.name] = instance
        self._sort()
        self._loaded = True
        return instance

    def register_all(self, extractors: Iterable[BaseExtractor | type[BaseExtractor]]) -> None:
        for extractor in extractors:
            self.register(extractor)

    def _sort(self) -> None:
        self._order = sorted(self._extractors.values(), key=lambda e: (e.priority, e.name))

    # ------------------------------------------------------------------ #
    def load_defaults(self) -> None:
        """Carrega os extractores embutidos (lazy import)."""
        from .bilibili import BilibiliExtractor
        from .brightcove import BrightcoveExtractor
        from .dailymotion import DailymotionExtractor
        from .direct import DirectMediaExtractor
        from .catalog import SiteCatalogExtractor
        from .facebook import FacebookExtractor
        from .francetv import FranceTVExtractor
        from .generic import GenericHTMLExtractor
        from .instagram import InstagramExtractor
        from .jwplatform import JWPlatformExtractor
        from .kaltura import KalturaExtractor
        from .pinterest import PinterestExtractor
        from .reddit import RedditExtractor
        from .soundcloud import SoundCloudExtractor
        from .tiktok import TikTokExtractor
        from .twitch import TwitchExtractor
        from .twitter import TwitterExtractor
        from .vimeo import VimeoExtractor
        from .youtube import YouTubeExtractor

        self.register_all(
            [
                YouTubeExtractor,
                VimeoExtractor,
                DailymotionExtractor,
                TikTokExtractor,
                TwitterExtractor,
                RedditExtractor,
                InstagramExtractor,
                TwitchExtractor,
                SoundCloudExtractor,
                FacebookExtractor,
                PinterestExtractor,
                BilibiliExtractor,
                BrightcoveExtractor,
                JWPlatformExtractor,
                FranceTVExtractor,
                KalturaExtractor,
                DirectMediaExtractor,
                SiteCatalogExtractor,
                GenericHTMLExtractor,
            ]
        )
        self._loaded = True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_defaults()

    # ------------------------------------------------------------------ #
    def get(self, name: str) -> BaseExtractor | None:
        self.ensure_loaded()
        return self._extractors.get(name)

    def all(self) -> list[BaseExtractor]:
        self.ensure_loaded()
        return list(self._order)

    def resolve(self, url: str) -> BaseExtractor:
        """Devolve o extractor adequado para o URL.

        Ordem de tentativa:
        1. Extractores por prioridade, excluindo catch_all (regex match exacto).
        2. O primeiro extractor catch_all encontrado (catálogo universal).
        Garante que o catálogo é sempre o último recurso antes de falhar.
        """
        self.ensure_loaded()
        catch_all_candidate: BaseExtractor | None = None

        for extractor in self._order:
            if extractor.catch_all:
                # Guarda como candidato final, mas continua a procurar match exacto
                if catch_all_candidate is None:
                    catch_all_candidate = extractor
                continue
            try:
                if extractor.matches(url):
                    return extractor
            except Exception as exc:  # noqa: BLE001
                log.warning("Erro no matching do extractor %s: %s", extractor.name, exc)

        # Nenhum extractor específico fez match — usa o catch_all (catálogo)
        if catch_all_candidate is not None:
            return catch_all_candidate

        raise UnsupportedUrlError(
            f"Nenhum extractor suporta este URL: {url}",
            details={"supported": [e.name for e in self._order]},
        )


#: Registo global (process-wide).
registry = ExtractorRegistry()

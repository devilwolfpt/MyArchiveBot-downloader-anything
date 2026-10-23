"""Motor Universal e Catálogo de Extractores do MyArchive Downloader."""
from __future__ import annotations

import sys

def import_extractors():
    """Inicializa os extractores do catálogo."""
    from . import extractors
    return extractors

def gen_extractor_classes():
    """Retorna a lista completa de classes de extractores suportadas."""
    from .extractors import _CLASS_LOOKUP
    return list(_CLASS_LOOKUP.values())

def gen_extractors():
    return [cls() for cls in gen_extractor_classes()]

def list_extractor_classes():
    return gen_extractor_classes()

def list_extractors():
    return [getattr(cls, "IE_NAME", getattr(cls, "__name__", "")) for cls in gen_extractor_classes()]

def get_info_extractor(ie_name):
    """Encontra uma classe de extractor pelo nome."""
    from .extractors import _CLASS_LOOKUP
    if ie_name in _CLASS_LOOKUP:
        return _CLASS_LOOKUP[ie_name]
    ie_key = f"{ie_name}IE"
    if ie_key in _CLASS_LOOKUP:
        return _CLASS_LOOKUP[ie_key]
    for name, cls in _CLASS_LOOKUP.items():
        if getattr(cls, "IE_NAME", "").lower() == ie_name.lower():
            return cls
    return None

# Alias interno para retrocompatibilidade
sys.modules.setdefault("app.core.extractors.extractor", sys.modules[__name__])

from .base import BaseExtractor, ExtractContext
from .catalog import SiteCatalogExtractor
from .direct import DirectMediaExtractor
from .registry import ExtractorRegistry, registry
from .YoutubeDL import YoutubeDL, YoutubeDL as MediaEngine

from .custom import (
    BilibiliExtractor,
    BrightcoveExtractor,
    CipherSolver,
    DailymotionExtractor,
    FacebookExtractor,
    FranceTVExtractor,
    GenericHTMLExtractor,
    InstagramExtractor,
    JWPlatformExtractor,
    KalturaExtractor,
    NullCipherSolver,
    PinterestExtractor,
    RedditExtractor,
    SoundCloudExtractor,
    TikTokExtractor,
    TwitchExtractor,
    TwitterExtractor,
    VimeoExtractor,
    YouTubeExtractor,
)

__all__ = [
    "BaseExtractor",
    "ExtractContext",
    "ExtractorRegistry",
    "registry",
    "CipherSolver",
    "NullCipherSolver",
    "YouTubeExtractor",
    "VimeoExtractor",
    "FranceTVExtractor",
    "DailymotionExtractor",
    "TikTokExtractor",
    "TwitterExtractor",
    "RedditExtractor",
    "InstagramExtractor",
    "TwitchExtractor",
    "SoundCloudExtractor",
    "FacebookExtractor",
    "PinterestExtractor",
    "BilibiliExtractor",
    "BrightcoveExtractor",
    "JWPlatformExtractor",
    "KalturaExtractor",
    "DirectMediaExtractor",
    "SiteCatalogExtractor",
    "GenericHTMLExtractor",
    "MediaEngine",
    "YoutubeDL",
    "gen_extractor_classes",
    "gen_extractors",
    "list_extractor_classes",
    "list_extractors",
    "get_info_extractor",
    "import_extractors",
]

"""Configuração central da aplicação (12-factor)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MAD_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "MyArchive Downloader"
    debug: bool = False
    api_prefix: str = "/api"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    root_path: str = ""

    # --- Armazenamento -----------------------------------------------------
    data_dir: Path = Path("./data")
    downloads_dir: Path = Path("./data/downloads")
    cache_dir: Path = Path("./data/cache")
    temp_dir: Path = Path("./data/tmp")

    # --- Camada HTTP -------------------------------------------------------
    http_timeout: float = 30.0
    http_connect_timeout: float = 10.0
    http_max_connections: int = 32
    http_keepalive_expiry: float = 30.0
    http_follow_redirects: bool = True
    http_max_redirects: int = 10
    http_verify_tls: bool = True
    proxy_url: str | None = None
    cookie_file: Path | None = None
    user_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_AGENTS))
    rotate_user_agents: bool = True
    default_headers: dict[str, str] = Field(default_factory=dict)

    # --- Downloader --------------------------------------------------------
    download_concurrency: int = 8
    chunk_size: int = 1024 * 1024
    max_retries: int = 5
    retry_backoff_base: float = 1.5
    retry_backoff_max: float = 30.0
    download_timeout: float = 600.0
    max_file_size: int = 8 * 1024**3  # 8 GiB
    max_segments: int = 20_000
    resume_enabled: bool = True
    #: Download paralelo por pedidos Range. O downloader usa ranges
    #: sequenciais e limitados para URLs do YouTube.
    parallel_download: bool = True
    #: Abaixo deste tamanho nao vale a pena abrir varias ligacoes.
    parallel_min_size: int = 1024 * 1024
    #: Chunk minimo por ligacao (define quantas ligacoes sao abertas).
    parallel_min_chunk_size: int = 512 * 1024
    #: Quantas vezes re-extrair os metadados quando o CDN devolve 403.
    #: As URLs do YouTube estao vinculadas ao IP de origem: se o IP de saida
    #: mudar (proxy rotativo, failover de ISP) o media responde 403.
    max_url_refreshes: int = 2

    # --- Jobs --------------------------------------------------------------
    job_ttl_seconds: int = 3600
    max_concurrent_jobs: int = 4
    job_history_limit: int = 200

    # --- Pós-processamento -------------------------------------------------
    enable_postprocess: bool = True
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    ffmpeg_timeout: float = 1800.0
    default_container: str = "mp4"

    # --- Segurança ---------------------------------------------------------
    api_key: str | None = None
    rate_limit_per_minute: int = 60
    allow_private_urls: bool = False
    max_page_bytes: int = 5 * 1024**2

    # --- Cache -------------------------------------------------------------
    cache_ttl_seconds: int = 1800
    cache_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("user_agents", mode="before")
    @classmethod
    def _split_uas(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split("|") if v.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return not self.debug

    @property
    def active_cookie_file(self) -> Path | None:
        """Devolve o ficheiro de cookies activo (configurado explicitamente ou data/cookies.txt)."""
        if self.cookie_file and self.cookie_file.exists() and self.cookie_file.stat().st_size > 0:
            return self.cookie_file
        candidate = self.data_dir / "cookies.txt"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
        return None

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.downloads_dir, self.cache_dir, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings

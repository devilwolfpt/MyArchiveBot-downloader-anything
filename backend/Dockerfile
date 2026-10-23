# ---------------------------------------------------------------------------
# MyArchive Downloader — Imagem de produção (Backend standalone / Render)
# Motor Universal incorporado nativamente (sem dependência pip do yt-dlp)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instala FFmpeg para fusão de streams/áudio, curl para healthchecks e tini
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências Python (sem yt-dlp no pip - motor incorporado directamente)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação (inclui o motor incorporado em app/core/extractors/)
COPY app ./app
COPY pyproject.toml .

# Cria utilizador sem privilégios e directórios de dados
RUN useradd --create-home --shell /usr/bin/bash appuser \
    && mkdir -p /app/data/downloads /app/data/cache /app/data/tmp \
    && chown -R appuser:appuser /app
USER appuser

ENV MAD_DATA_DIR=/app/data \
    MAD_DOWNLOADS_DIR=/app/data/downloads \
    MAD_CACHE_DIR=/app/data/cache \
    MAD_TEMP_DIR=/app/data/tmp \
    PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --proxy-headers --forwarded-allow-ips '*'"]

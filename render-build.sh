#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Script de Build para Render.com (Ambiente Nativo Python)
# ---------------------------------------------------------------------------
set -o errexit

echo "==> A instalar dependências Python do MyArchive Downloader..."
python -m pip install --upgrade pip
pip install -r backend/requirements.txt

# Verifica se o FFmpeg está presente no sistema
if ! command -v ffmpeg &> /dev/null; then
    echo "==> FFmpeg não encontrado no sistema. A instalar binário estático Linux..."
    mkdir -p "$HOME/bin"
    FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    curl -fsSL "$FFMPEG_URL" | tar -xJ --strip-components=1 -C "$HOME/bin"
    export PATH="$HOME/bin:$PATH"
    echo "==> FFmpeg instalado com sucesso em $HOME/bin/ffmpeg:"
    "$HOME/bin/ffmpeg" -version | head -n 1
else
    echo "==> FFmpeg já está disponível no sistema:"
    ffmpeg -version | head -n 1
fi

echo "==> Build concluído com sucesso para o Render!"

@echo off
title MyArchive Downloader - Enviar para o GitHub
echo ============================================================
echo   MyArchive Downloader - Enviar para o GitHub
echo ============================================================
echo.
echo Repositorio:
echo https://github.com/devilwolfpt/MyArchiveBot-downloader-anything.git
echo.
echo A enviar ficheiros para o GitHub...
echo.

git push -u origin main

echo.
if %ERRORLEVEL% EQU 0 (
    echo ============================================================
    echo   [OK] Projeto enviado com sucesso para o GitHub!
    echo ============================================================
) else (
    echo ============================================================
    echo   [!] Ocorreu um erro no envio.
    echo ============================================================
)
echo.
pause

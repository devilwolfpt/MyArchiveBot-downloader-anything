"""Pipeline de download: extract → select → plan → download → post-process.

Este módulo coordena o core e traduz o estado interno em eventos de
progresso publishados no :class:`JobManager`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..core.downloader.chunk import ChunkDownloader
from ..core.downloader.manager import Job, JobManager
from ..core.downloader.plan import DownloadPlan, PlanBuilder, sanitize_filename
from ..core.downloader.storage import Storage
from ..core.downloader.subtitle import SubtitleDownloader
from ..core.exceptions import ExtractionError, MediaUrlExpiredError, NoFormatFoundError
from ..core.http.client import HttpClient
from ..core.http.cookies import load_cookies_from_browser, load_netscape_cookiejar
from ..core.models import (
    DownloadMode,
    JobPhase,
    JobResult,
    JobStatus,
    MediaInfo,
    MediaType,
    ProgressEvent,
    StreamFormat,
    SubtitleTrack,
)
from ..core.postprocess.processor import PostProcessor
from .extraction import ExtractionService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Selection:
    """Formatos escolhidos para o download."""

    primary: StreamFormat
    video: StreamFormat | None = None
    audio: StreamFormat | None = None
    needs_merge: bool = False
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    #: Descrição legível do que foi escolhido (mostrada no painel).
    notes: list[str] = field(default_factory=list)


class DownloadService:
    """Serviço de alto nível: cria e executa jobs de download."""

    def __init__(
        self,
        settings: Settings,
        job_manager: JobManager,
        extraction: ExtractionService,
    ) -> None:
        self.settings = settings
        self.jobs = job_manager
        self.extraction = extraction
        self.storage = Storage(settings.downloads_dir, settings.temp_dir)
        self.postprocess = PostProcessor(settings)

    # ------------------------------------------------------------------ #
    # Selecção de formato
    # ------------------------------------------------------------------ #
    def select_formats(
        self,
        info: MediaInfo,
        *,
        format_id: str | None = None,
        video_format_id: str | None = None,
        audio_format_id: str | None = None,
        subtitle_lang: str | None = None,
        mode: DownloadMode = DownloadMode.AUTO,
        max_height: int | None = None,
        merge: bool = True,
        container: str = "mp4",
    ) -> Selection:
        """Escolhe vídeo, áudio e legendas conforme o pedido.

        Os três eixos são independentes e cada um aceita a selecção
        automática:

        * ``video_format_id`` / ``audio_format_id`` — formato explícito.
          O valor ``"auto"`` (ou ``None``) deixa o programa escolher a
          melhor qualidade disponível.
        * ``subtitle_lang`` — ``None``/``"none"`` (sem legendas),
          ``"auto"``, um identificador de faixa, um idioma ou ``"all"``.

        Regras de merge (o ficheiro final tem sempre **uma** faixa de vídeo
        e, opcionalmente, **uma** de áudio):

        * vídeo explícito **com** áudio e sem áudio explícito → sem merge;
        * vídeo explícito **sem** áudio → anexa-se a melhor faixa de áudio;
        * vídeo explícito + áudio explícito → o áudio escolhido substitui
          o que o vídeo trazia.

        Em MP4 prefere-se áudio AAC: Opus dentro de MP4 é válido mas muitos
        players (Windows Media Player, alguns hardware) não o reproduzem.
        """
        formats = [f for f in info.formats if not f.unavailable]
        if not formats:
            raise NoFormatFoundError("Nenhum formato disponível para este URL")

        by_id = {f.id: f for f in formats}
        video_pool = [f for f in formats if f.media_type is MediaType.VIDEO]
        audio_pool = [f for f in formats if f.media_type is MediaType.AUDIO]
        muxed_pool = [
            f
            for f in video_pool
            if f.acodec and f.acodec != "none" and f.vcodec and f.vcodec != "none"
        ]
        notes: list[str] = []

        def _apply_limit(pool: list[StreamFormat]) -> list[StreamFormat]:
            if max_height is None:
                return pool
            limited = [f for f in pool if (f.height or 0) <= max_height]
            return limited or pool

        def _best_audio(target_container: str = "mp4") -> StreamFormat | None:
            """Melhor faixa de audio, preferindo o codec mais portavel."""
            if not audio_pool:
                return None

            def _rank(fmt: StreamFormat) -> tuple[int, int]:
                return (fmt.abr or fmt.tbr or 0, fmt.height or 0)

            if target_container == "mp4":
                aac = [
                    f for f in audio_pool if (f.acodec or "").startswith(("mp4a", "aac"))
                ]
                if aac:
                    return max(aac, key=_rank)
            elif target_container == "webm":
                opus = [f for f in audio_pool if "opus" in (f.acodec or "")]
                if opus:
                    return max(opus, key=_rank)
            return max(audio_pool, key=_rank)

        def _has_audio(fmt: StreamFormat) -> bool:
            return bool(fmt.acodec) and fmt.acodec != "none"

        def _pick_best(pool: list[StreamFormat]) -> StreamFormat | None:
            """Melhor stream de vídeo de um conjunto (resolução > bitrate)."""
            if not pool:
                return None
            return max(pool, key=lambda f: (f.height or 0, f.tbr or 0, f.filesize or 0))

        def _pick_best_audio(pool: list[StreamFormat]) -> StreamFormat | None:
            """Melhor faixa de áudio de um conjunto (bitrate de áudio)."""
            if not pool:
                return None
            return max(pool, key=lambda f: (f.abr or f.tbr or 0))

        # ---------------------------------------------------------------- #
        # 1) Selectores explícitos
        # ---------------------------------------------------------------- #
        def _resolve(value: str | None, kind: str) -> StreamFormat | None:
            """Resolve um id explícito (ignora o valor mágico ``"auto"``)."""
            if not value or value.lower() == "auto":
                return None
            chosen = by_id.get(value)
            if chosen is None:
                raise NoFormatFoundError(
                    f"Formato de {kind} '{value}' não encontrado",
                    details={"available": sorted(by_id.keys())[:50]},
                )
            return chosen

        explicit_video = _resolve(video_format_id, "vídeo")
        explicit_audio = _resolve(audio_format_id, "áudio")

        # O ``format_id`` legado continua a funcionar (equivale ao `-f` do
        # motor nativo) quando nenhum selector explícito foi fornecido.
        if format_id and explicit_video is None and explicit_audio is None:
            legacy = _resolve(format_id, "")
            if legacy is not None:
                if legacy.media_type is MediaType.AUDIO:
                    explicit_audio = legacy
                else:
                    explicit_video = legacy

        # ---------------------------------------------------------------- #
        # 2) Modo AUDIO — só áudio
        # ---------------------------------------------------------------- #
        if mode is DownloadMode.AUDIO:
            audio = explicit_audio or _pick_best_audio(_apply_limit(audio_pool))
            if audio is None:
                raise NoFormatFoundError("Nenhuma faixa de áudio disponível")
            subtitles = self.select_subtitles(info, subtitle_lang)
            return Selection(
                primary=audio,
                audio=audio,
                subtitles=subtitles,
                notes=self._selection_notes(audio, None, subtitles),
            )

        # ---------------------------------------------------------------- #
        # 3) Escolha do vídeo
        # ---------------------------------------------------------------- #
        if explicit_video is not None:
            video = explicit_video
        elif mode in (DownloadMode.BOTH, DownloadMode.AUTO):
            # Preferimos um stream *só de vídeo* para poder anexar a
            # melhor faixa de áudio (formato de combinação `bv*+ba`).
            limited_video = _apply_limit(video_pool)
            video_only = [f for f in limited_video if not _has_audio(f)]
            video = _pick_best(video_only) or _pick_best(limited_video)
            notes.append("Vídeo escolhido automaticamente (melhor resolução)")
        else:
            # VIDEO / ORIGINAL: um stream único já com áudio é mais simples.
            limited_video = _apply_limit(video_pool)
            limited_muxed = [f for f in limited_video if f in muxed_pool]
            video = _pick_best(limited_muxed) or _pick_best(limited_video)

        if video is None:
            raise NoFormatFoundError("Nenhum stream de vídeo disponível")

        # ---------------------------------------------------------------- #
        # 4) Escolha do áudio
        # ---------------------------------------------------------------- #
        final_audio: StreamFormat | None = None
        needs_merge = False

        if explicit_audio is not None:
            final_audio = explicit_audio
            if explicit_video is not None:
                # O utilizador escolheu este áudio explicitamente: substitui
                # o áudio que o vídeo trazia.
                needs_merge = merge and mode is not DownloadMode.ORIGINAL
                notes.append(
                    f"Vídeo: {explicit_video.label} + áudio: {explicit_audio.label}"
                )
        elif not _has_audio(video):
            final_audio = _best_audio(container)
            if final_audio is not None and merge and mode is not DownloadMode.ORIGINAL:
                needs_merge = final_audio.id != video.id
                notes.append(f"Áudio anexado automaticamente: {final_audio.label}")
            elif final_audio is not None:
                final_audio = None
        else:
            notes.append("O vídeo escolhido já inclui áudio")

        # ---------------------------------------------------------------- #
        # 5) Legendas
        # ---------------------------------------------------------------- #
        subtitles = self.select_subtitles(info, subtitle_lang)
        notes.extend(self._selection_notes(video, final_audio, subtitles))

        return Selection(
            primary=video,
            video=video,
            audio=final_audio,
            needs_merge=needs_merge,
            subtitles=subtitles,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Legendas
    # ------------------------------------------------------------------ #
    def select_subtitles(
        self, info: MediaInfo, subtitle_lang: str | None
    ) -> list[SubtitleTrack]:
        """Escolhe as faixas de legenda conforme o pedido.

        Valores aceites para ``subtitle_lang``:

        * ``None`` / ``"none"`` — não descarregar legendas;
        * ``"auto"`` — o programa escolhe (idioma preferido do utilizador,
          manual antes de automática);
        * o identificador exacto de uma faixa (``grupo:idioma:nome``);
        * um código de idioma (``"pt"``, ``"en"``, ...) — todas as faixas
          desse idioma;
        * ``"all"`` — todas as faixas disponíveis.
        """
        if not subtitle_lang:
            return []
        value = subtitle_lang.strip()
        if value.lower() in ("none", "no", "false", "off"):
            return []

        tracks: list[tuple[str, SubtitleTrack]] = []
        for group, items in (info.subtitles or {}).items():
            for track in items:
                if track.url:
                    tracks.append((group, track))
        if not tracks:
            return []

        low = value.lower()
        if low == "all":
            return [track for _, track in tracks]
        if low == "auto":
            return self._best_subtitle(tracks)

        # Identificador exacto (o que a interface mostra e devolve).
        for group, track in tracks:
            if track.track_id(group) == value:
                return [track]

        # Código de idioma (ou o nome do grupo de legendas).
        selected = [
            track
            for group, track in tracks
            if track.language.lower() == low or group.lower() == low
        ]
        return selected

    @staticmethod
    def _best_subtitle(tracks: list[tuple[str, SubtitleTrack]]) -> list[SubtitleTrack]:
        """Escolhe a melhor faixa de legenda de forma automática.

        Preferência: idioma do utilizador (português), depois inglês; dentro
        do mesmo idioma, as faixas manuais ganham às automáticas.
        """
        language_rank = ("pt", "pt-pt", "pt-br", "en", "en-us", "en-gb", "es", "fr", "de", "it")

        def _score(item: tuple[str, SubtitleTrack]) -> tuple[int, int]:
            _group, track = item
            lang = (track.language or "").lower()
            try:
                lang_rank = language_rank.index(lang)
            except ValueError:
                lang_rank = len(language_rank)
            # Maior pontuação = melhor: primeiro o idioma preferido, depois
            # as faixas manuais (as automáticas pontuam menos).
            return (len(language_rank) - lang_rank, 0 if track.automatic else 1)

        # `max` devolve o primeiro elemento com pontuação máxima, o que
        # mantém a ordem original em caso de empate.
        return [max(tracks, key=_score)[1]]

    @staticmethod
    def _selection_notes(
        video: StreamFormat | None,
        audio: StreamFormat | None,
        subtitles: list[SubtitleTrack],
    ) -> list[str]:
        """Descrição curta do que foi escolhido (mostrada no painel)."""
        notes: list[str] = []
        if video is not None:
            notes.append(f"Vídeo: {video.label}")
        if audio is not None:
            notes.append(f"Áudio: {audio.label}")
        if subtitles:
            labels = ", ".join(
                f"{t.name or t.language}{' (auto)' if t.automatic else ''}" for t in subtitles
            )
            notes.append(f"Legendas: {labels}")
        return notes

    # ------------------------------------------------------------------ #
    # Execução
    # ------------------------------------------------------------------ #
    async def execute(self, job: Job, request) -> JobResult:
        """Executa o download completo de um job."""
        started = time.time()

        # --- 1) Extracção -------------------------------------------------
        await self.jobs.update_status(
            job, JobStatus.RUNNING, phase=JobPhase.EXTRACTING, message="A extrair metadados..."
        )
        cookie_jar = None
        if request.cookies_from_browser:
            try:
                cookie_jar = load_cookies_from_browser(request.cookies_from_browser, request.url)
            except (RuntimeError, ValueError) as exc:
                raise ExtractionError(str(exc)) from exc
        elif self.settings.active_cookie_file:
            try:
                cookie_jar = load_netscape_cookiejar(self.settings.active_cookie_file)
            except Exception as exc:
                log.warning("Ficheiro de cookies ativo ignorado: %s", exc)

        extract_kwargs: dict[str, Any] = {
            "headers": request.headers,
            "cookies": request.cookies,
        }
        if request.cookies_from_browser:
            extract_kwargs["cookies_from_browser"] = request.cookies_from_browser
        if cookie_jar is not None:
            extract_kwargs["cookie_jar"] = cookie_jar

        info = await self.extraction.extract(request.url, **extract_kwargs)
        job.title = info.title
        await self._publish(job, message=f"Metadados extraídos: {info.title}")

        # --- 2) Selecção --------------------------------------------------
        container = request.container or self.settings.default_container
        selection = self.select_formats(
            info,
            format_id=request.format_id,
            video_format_id=request.video_format_id,
            audio_format_id=request.audio_format_id,
            subtitle_lang=request.subtitle_lang,
            mode=request.mode,
            max_height=request.max_height,
            merge=bool(getattr(request, "merge", True)),
            container=container,
        )
        await self._publish(
            job,
            message=f"Formato seleccionado: {selection.primary.label}",
        )

        # --- 3) Planos ----------------------------------------------------
        async with HttpClient(
            self.settings,
            headers=request.headers,
            cookies=request.cookies,
            cookie_jar=cookie_jar,
            cookie_scope_url=request.url,
        ) as http:
            builder = PlanBuilder(http)
            downloader = ChunkDownloader(http, self.settings, self.storage)
            build_headers = self._build_download_headers(request)

            video_path: Path | None = None
            audio_path: Path | None = None

            async def progress(
                downloaded: int, total: int | None, seg_index: int, seg_total: int
            ) -> None:
                percent = (downloaded / total * 100) if total else 0.0
                await self._publish(
                    job,
                    percent=percent,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    segment_index=seg_index,
                    segment_total=seg_total,
                )

            if selection.needs_merge and selection.video and selection.audio:
                await self.jobs.update_status(
                    job, JobStatus.RUNNING, phase=JobPhase.DOWNLOADING, message="A descarregar vídeo..."
                )
                video_path, info = await self._download_format(
                    job,
                    request,
                    info,
                    selection.video,
                    suffix="video",
                    builder=builder,
                    downloader=downloader,
                    progress=progress,
                    headers=build_headers,
                )

                await self.jobs.update_status(
                    job, JobStatus.RUNNING, phase=JobPhase.DOWNLOADING, message="A descarregar áudio..."
                )
                audio_path, info = await self._download_format(
                    job,
                    request,
                    info,
                    selection.audio,
                    suffix="audio",
                    builder=builder,
                    downloader=downloader,
                    progress=progress,
                    headers=build_headers,
                )
            else:
                await self.jobs.update_status(
                    job, JobStatus.RUNNING, phase=JobPhase.DOWNLOADING, message="A descarregar..."
                )
                video_path, info = await self._download_format(
                    job,
                    request,
                    info,
                    selection.primary,
                    suffix="",
                    builder=builder,
                    downloader=downloader,
                    progress=progress,
                    headers=build_headers,
                )

            # --- 3.5) Legendas ---------------------------------------------
            subtitle_files: list[tuple[Path, str]] = []
            if selection.subtitles:
                await self.jobs.update_status(
                    job,
                    JobStatus.RUNNING,
                    phase=JobPhase.DOWNLOADING,
                    message="A descarregar legendas...",
                )
                subtitle_files = await self._download_subtitles(
                    job,
                    selection.subtitles,
                    http=http,
                    info=info,
                    headers=build_headers,
                )

        # --- 4) Pós-processamento -----------------------------------------
        await self.jobs.update_status(
            job, JobStatus.RUNNING, phase=JobPhase.MERGING, message="A processar..."
        )
        output = await self.postprocess.process(
            mode=request.mode,
            video_path=video_path,
            audio_path=audio_path,
            output_path=self._output_path(info, container),
            container=container,
            audio_format=request.audio_format,
            extract_audio=request.extract_audio,
            audio_codec=self._merge_audio_codec(selection, container),
            subtitles=subtitle_files,
        )

        # --- 5) Resultado --------------------------------------------------
        await self.jobs.update_status(
            job, JobStatus.RUNNING, phase=JobPhase.FINALIZING, message="A finalizar..."
        )
        job.filename = output.path.name

        for warning in output.warnings:
            log.warning("Job %s: %s", job.id, warning)

        size = output.path.stat().st_size if output.path.exists() else None
        result = JobResult(
            job_id=job.id,
            status=JobStatus.COMPLETED,
            filename=output.path.name,
            path=str(output.path),
            size=size,
            mime_type=output.mime_type,
            download_url=f"/api/download/{job.id}",
            thumbnail=info.thumbnail,
            title=info.title,
            duration=info.duration,
            warnings=list(output.warnings),
        )
        await self._publish(
            job,
            percent=100.0,
            message=output.warnings[0] if output.warnings else "Concluído",
        )
        log.info("Job %s concluído em %.1fs (%s)", job.id, time.time() - started, output.path.name)
        return result

    # ------------------------------------------------------------------ #
    # Download com renovação de URL
    # ------------------------------------------------------------------ #
    def _build_download_headers(self, request) -> dict[str, str]:
        headers = dict(getattr(request, "headers", {}) or {})
        if getattr(request, "url", None) and not headers.get("Referer"):
            headers["Referer"] = request.url
            origin = request.url.split("/", 3)
            if len(origin) >= 3:
                headers["Origin"] = f"{origin[0]}//{origin[2]}"
        return headers

    async def _download_format(
        self,
        job: Job,
        request,
        info: MediaInfo,
        fmt: StreamFormat,
        *,
        suffix: str,
        builder: PlanBuilder,
        downloader: ChunkDownloader,
        progress,
        headers: dict[str, str] | None = None,
    ) -> tuple[Path, MediaInfo]:
        """Descarrega um formato, renovando a URL assinada em caso de 403.

        O callback ``renew`` reextrai os metadados sem cache e devolve um
        plano atualizado, permitindo continuar do ponto onde o download parou.
        """
        current_info = info
        current_fmt = fmt
        filename = self._filename(info, fmt, suffix=suffix)
        request_headers = headers or self._build_download_headers(request)
        renewals = 0
        lock = asyncio.Lock()

        async def renew() -> DownloadPlan | None:
            """Re-extrai os metadados e devolve um plano com URL fresca."""
            nonlocal current_info, current_fmt, renewals

            async with lock:
                if renewals >= self.settings.max_url_refreshes:
                    log.info("Job %s: sem mais renovações disponíveis", job.id)
                    return None
                renewals += 1
                log.info(
                    "Job %s: URL expirada (403), a re-extrair (renovação %d/%d)",
                    job.id,
                    renewals,
                    self.settings.max_url_refreshes,
                )
                await self._publish(
                    job,
                    message="URL expirada (403) — a renovar os metadados…",
                )
                try:
                    if request.cookies_from_browser:
                        refreshed_info = await self.extraction.extract(
                            request.url,
                            headers=request.headers,
                            cookies=request.cookies,
                            cookies_from_browser=request.cookies_from_browser,
                            use_cache=False,
                        )
                    else:
                        refreshed_info = await self.extraction.extract(
                            request.url,
                            headers=request.headers,
                            cookies=request.cookies,
                            use_cache=False,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Job %s: renovação falhou: %s", job.id, exc)
                    return None

                refreshed = self._find_format(refreshed_info, current_fmt.id)
                if refreshed is None:
                    log.warning(
                        "Job %s: o formato %s desapareceu do catálogo",
                        job.id,
                        current_fmt.id,
                    )
                    return None

                current_info = refreshed_info
                current_fmt = refreshed
                return await builder.build(
                    refreshed,
                    filename=filename,
                    headers=request_headers,
                )

        plan = await builder.build(
            current_fmt,
            filename=filename,
            headers=request_headers,
        )
        try:
            path = await downloader.download(
                plan,
                on_progress=progress,
                cancel_event=job.cancel_event,
                job_id=job.id,
                plan_provider=renew,
            )
            return path, current_info
        except MediaUrlExpiredError as exc:
            host = urlsplit(request.url).hostname or "o servidor"
            raise MediaUrlExpiredError(
                f"O servidor ({host}) recusou o pedido de transferência (HTTP 403 / Acesso Negado). "
                "Se o site estiver protegido por Cloudflare ou exigir autenticação, ativa a opção "
                "de cookies do browser nas Definições e tenta novamente.",
                details={"tentativas": renewals + 1},
            ) from exc

    @staticmethod
    def _find_format(info: MediaInfo, format_id: str) -> StreamFormat | None:
        """Procura um formato (pelo id) nos metadados recem-extraídos."""
        for fmt in info.formats:
            if fmt.id == format_id and not fmt.unavailable:
                return fmt
        return None

    # ------------------------------------------------------------------ #
    async def _download_subtitles(
        self,
        job: Job,
        tracks: list[SubtitleTrack],
        *,
        http: HttpClient,
        info: MediaInfo,
        headers: dict[str, str] | None = None,
    ) -> list[tuple[Path, str]]:
        """Descarrega as faixas de legenda e converte-as para SRT.

        Devolve a lista de pares ``(ficheiro, idioma)`` prontos para o mux.
        Uma faixa que falhe é simplesmente ignorada (com um aviso no log):
        o vídeo é entregue na mesma.
        """
        if not tracks:
            return []

        downloader = SubtitleDownloader(http, self.storage.job_dir(job.id))
        hint = sanitize_filename(info.title or "legenda")
        files: list[tuple[Path, str]] = []

        for track in tracks:
            await self._publish(
                job, message=f"A descarregar legenda ({track.language})..."
            )
            path = await downloader.fetch(track, name_hint=hint, headers=headers)
            if path is not None:
                files.append((path, track.language))
            else:
                log.warning(
                    "Job %s: legenda '%s' indisponível", job.id, track.language
                )

        if files:
            await self._publish(
                job, message=f"{len(files)} legenda(s) descarregada(s)"
            )
        return files

    # ------------------------------------------------------------------ #
    def _filename(self, info: MediaInfo, fmt: StreamFormat, *, suffix: str) -> str:
        stem = sanitize_filename(info.title or "media")
        if suffix:
            stem = f"{stem}_{suffix}"
        ext = fmt.container.value
        if ext in ("unknown", "ts"):
            ext = "mp4" if fmt.media_type is MediaType.VIDEO else "m4a"
        return f"{stem}.{ext}"

    def _output_path(self, info: MediaInfo, container: str) -> Path:
        """Caminho do ficheiro final (único e diferente das entradas)."""
        stem = sanitize_filename(info.title or "media")
        return self.storage.final_path(f"{stem}.{container}")

    @staticmethod
    def _merge_audio_codec(selection: Selection, container: str) -> str | None:
        """Codec a forçar no áudio durante o merge (ou ``None`` = copiar).

        Opus dentro de MP4 é válido mas muitos players não o suportam:
        nesse caso o áudio é transcodeado para AAC (o vídeo é copiado).
        """
        if container != "mp4" or selection.audio is None:
            return None
        acodec = (selection.audio.acodec or "").lower()
        if not acodec or acodec.startswith(("mp4a", "aac")):
            return None
        return "aac"

    async def _publish(self, job: Job, **kwargs) -> None:
        progress = job.progress
        event = ProgressEvent(
            job_id=job.id,
            status=job.status,
            phase=job.phase,
            percent=kwargs.get("percent", progress.percent if progress else 0.0),
            downloaded_bytes=kwargs.get("downloaded_bytes", progress.downloaded_bytes if progress else 0),
            total_bytes=kwargs.get("total_bytes", progress.total_bytes if progress else None),
            speed=progress.speed if progress else 0.0,
            eta=progress.eta if progress else None,
            segment_index=kwargs.get("segment_index", progress.segment_index if progress else 0),
            segment_total=kwargs.get("segment_total", progress.segment_total if progress else 0),
            message=kwargs.get("message"),
            filename=job.filename,
        )
        await self.jobs.publish(job, event)

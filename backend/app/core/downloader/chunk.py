"""Chunk downloader: transferências paralelas, retomáveis e tolerantes a falhas.

Estratégias:
  * **Progressive** — um único recurso; usa ``Range: bytes=N-`` para retomar
    a partir do ficheiro parcial já em disco.
  * **Segmented** — HLS/DASH: cada segmento é descarregado em paralelo para
    um ficheiro próprio (permite retomar) e depois concatenado.

Ambos os caminhos tratam:
  * ``403 Forbidden`` por token expirado (via ``token_provider`` do cliente HTTP);
  * ``429``/``5xx`` com backoff exponencial e jitter;
  * cancelamento cooperativo (``asyncio.Event``).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from ...config import Settings
from ..exceptions import (
    DownloadCancelledError,
    DownloadError,
    FileTooLargeError,
    HttpForbiddenError,
    MediaUrlExpiredError,
)
from ..http.client import HttpClient
from .crypto import AesKey, decrypt_segment, parse_iv
from .plan import DownloadPlan, SegmentPlan
from .storage import Storage

log = logging.getLogger(__name__)
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)", re.IGNORECASE)

#: Callback de progresso: ``(downloaded_bytes, total_bytes, seg_index, seg_total)``
ProgressCallback = Callable[[int, int | None, int, int], Awaitable[None]]


class RangeUnsupportedError(Exception):
    """O servidor ignorou o header ``Range`` (respondeu 200 em vez de 206)."""


class ProgressTracker:
    """Calcula velocidade média, ETA e percentagem."""

    def __init__(self, total_bytes: int | None) -> None:
        self.total_bytes = total_bytes
        self.downloaded = 0
        self.started_at = time.monotonic()
        self._last_sample = self.started_at
        self._last_bytes = 0
        self.speed = 0.0

    def update(self, downloaded: int, total_bytes: int | None = None) -> None:
        self.downloaded = downloaded
        if total_bytes is not None:
            self.total_bytes = total_bytes
        now = time.monotonic()
        elapsed = now - self._last_sample
        if elapsed >= 0.5:
            delta = downloaded - self._last_bytes
            self.speed = delta / elapsed
            self._last_sample = now
            self._last_bytes = downloaded

    @property
    def percent(self) -> float:
        if not self.total_bytes:
            return 0.0
        return min(100.0, (self.downloaded / self.total_bytes) * 100.0)

    @property
    def eta(self) -> float | None:
        if not self.total_bytes or self.speed <= 0:
            return None
        remaining = max(0, self.total_bytes - self.downloaded)
        return remaining / self.speed


#: Callback que devolve um plano novo (URL renovada) para o mesmo recurso.
#: É usado quando o servidor rejeita a URL assinada com HTTP 403 — o
#: Alguns servidores (ex.: YouTube, Pixabay CDN) vinculam as URLs de media ao
#: IP ou token de origem — uma rotação de IP/proxy/VPN invalida a URL a meio
#: do download e provoca HTTP 403. O mecanismo de renovação trata este caso.
PlanProvider = Callable[[], Awaitable["DownloadPlan | None"]]


class ChunkDownloader:
    """Descarrega um :class:`DownloadPlan` para disco."""

    def __init__(self, http: HttpClient, settings: Settings, storage: Storage) -> None:
        self.http = http
        self.settings = settings
        self.storage = storage

    # ------------------------------------------------------------------ #
    async def download(
        self,
        plan: DownloadPlan,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        job_id: str = "job",
        plan_provider: PlanProvider | None = None,
    ) -> Path:
        if plan.is_segmented:
            return await self._download_segmented(
                plan,
                on_progress=on_progress,
                cancel_event=cancel_event,
                job_id=job_id,
                plan_provider=plan_provider,
            )
        return await self._download_progressive(
            plan,
            on_progress=on_progress,
            cancel_event=cancel_event,
            job_id=job_id,
            plan_provider=plan_provider,
        )

    async def _renew_plan(
        self, plan: DownloadPlan, plan_provider: PlanProvider | None
    ) -> DownloadPlan | None:
        """Pede um plano novo ao provider (URL renovada)."""
        if plan_provider is None:
            return None
        try:
            renewed = await plan_provider()
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao renovar o plano: %s", exc)
            return None
        return renewed

    # ------------------------------------------------------------------ #
    # Progressivo
    # ------------------------------------------------------------------ #
    async def _download_progressive(
        self,
        plan: DownloadPlan,
        *,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        job_id: str = "job",
        plan_provider: PlanProvider | None = None,
    ) -> Path:
        target = self.storage.final_path(plan.filename)
        part = self.storage.part_path(target)

        if self._is_googlevideo(plan.url):
            return await self._download_googlevideo(
                plan,
                target=target,
                part=part,
                on_progress=on_progress,
                cancel_event=cancel_event,
                plan_provider=plan_provider,
            )

        # --- Caminho paralelo (varios pedidos Range) ----------------------
        # Quando o tamanho total é conhecido, usa-se este caminho; caso o
        # servidor não honre o Range, cai no stream único.
        if self.settings.parallel_download:
            total = plan.total_bytes
            if total is None:
                try:
                    total = await self.http.content_length(plan.url, headers=plan.headers)
                except Exception as exc:  # noqa: BLE001
                    log.debug("content-length indisponível: %s", exc)
                    total = None
            if total and total >= self.settings.parallel_min_size:
                try:
                    return await self._download_parallel(
                        plan,
                        total=total,
                        on_progress=on_progress,
                        cancel_event=cancel_event,
                        job_id=job_id,
                        plan_provider=plan_provider,
                    )
                except RangeUnsupportedError:
                    log.info("Servidor não honra o Range; a usar stream único")
                    self.storage.cleanup_job(job_id)
                except DownloadCancelledError:
                    raise
                except MediaUrlExpiredError:
                    # Sem renovação possível: o stream único tenta de novo.
                    log.warning("URL expirada no path paralelo; a usar stream único")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Download paralelo falhou (%s); a usar stream único", exc)
                    self.storage.cleanup_job(job_id)

        start = 0
        if self.settings.resume_enabled and part.exists():
            start = part.stat().st_size
            log.info("A retomar %s a partir de %d bytes", target.name, start)

        for attempt in range(self.settings.max_retries):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelado")

            try:
                # Some media CDNs require a Range header even when starting
                # from byte zero.
                response, iterator = await self.http.stream(
                    "GET",
                    plan.url,
                    headers=plan.headers,
                    range_start=start,
                )
            except HttpForbiddenError as exc:
                # A URL assinada deixou de valer (403). Se houver um provider
                # que sabe renová-la, continua-se o download a partir do
                # ponto onde parou.
                renewed = await self._renew_plan(plan, plan_provider)
                if renewed is not None:
                    log.info("URL de media expirada; plano renovado, a continuar")
                    plan = renewed
                    continue
                raise MediaUrlExpiredError(
                    "A URL de media expirou (403); é preciso renová-la",
                    details={"range_start": start},
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 5xx/timeout serão retentados
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(f"Falha ao descarregar {plan.url}: {exc}") from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue

            status = response.status_code
            total = self._total_from_headers(response.headers, plan.total_bytes)

            # Se pedimos Range mas o servidor devolveu 200 -> recomeçar.
            if start > 0 and status == 200:
                log.warning("Servidor ignorou o Range; a recomeçar do zero")
                start = 0
                part.unlink(missing_ok=True)

            mode = "ab" if start > 0 else "wb"
            written = start

            try:
                with part.open(mode) as handle:
                    async for chunk in iterator:
                        if cancel_event is not None and cancel_event.is_set():
                            handle.flush()
                            raise DownloadCancelledError("Download cancelado")
                        handle.write(chunk)
                        written += len(chunk)
                        if self._exceeds_limit(written):
                            raise FileTooLargeError(
                                "Ficheiro excede o tamanho máximo permitido",
                                details={"max_bytes": self.settings.max_file_size},
                            )
                        if on_progress is not None:
                            await on_progress(written, total, 0, 1)
            except DownloadCancelledError:
                raise
            except FileTooLargeError:
                part.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Erro durante transfer (%s): %s", target.name, exc)
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(f"Falha ao descarregar {plan.url}: {exc}") from exc
                start = part.stat().st_size if part.exists() else 0
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue
            finally:
                await response.aclose()

            part.replace(target)
            return target

        raise DownloadError(f"Falha ao descarregar {plan.url}")

    async def _download_googlevideo(
        self,
        plan: DownloadPlan,
        *,
        target: Path,
        part: Path,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        plan_provider: PlanProvider | None,
    ) -> Path:
        """Fetch Googlevideo with bounded sequential ranges (open-ended ranges get 403)."""
        total = plan.total_bytes
        if total is None:
            total = await self.http.content_length(plan.url, headers=plan.headers)
        if total is None or total <= 0:
            raise DownloadError("O servidor não devolveu o tamanho total do ficheiro de media.")
        if self._exceeds_limit(total):
            raise FileTooLargeError(
                "Ficheiro excede o tamanho máximo permitido",
                details={"max_bytes": self.settings.max_file_size, "size": total},
            )

        start = part.stat().st_size if self.settings.resume_enabled and part.exists() else 0
        if start > total:
            part.unlink(missing_ok=True)
            start = 0
        if start == total:
            part.replace(target)
            return target

        chunk_size = max(1, self.settings.chunk_size)
        current_plan = plan
        while start < total:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelado")

            completed_range = False
            for attempt in range(self.settings.max_retries):
                end = min(start + chunk_size, total) - 1
                request_headers = dict(current_plan.headers)
                request_headers["Accept-Encoding"] = "identity"

                try:
                    response, iterator = await self.http.stream(
                        "GET",
                        current_plan.url,
                        headers=request_headers,
                        range_start=start,
                        range_end=end,
                    )
                except HttpForbiddenError as exc:
                    renewed = await self._renew_plan(current_plan, plan_provider)
                    if renewed is None:
                        raise MediaUrlExpiredError(
                            "O servidor recusou o intervalo de media (HTTP 403); "
                            "não foi possível renovar o URL.",
                            details={"range_start": start, "range_end": end},
                        ) from exc
                    current_plan = renewed
                    total = current_plan.total_bytes or total
                    continue

                try:
                    if response.status_code != 206:
                        raise DownloadError(
                            f"O servidor respondeu HTTP {response.status_code} "
                            "a um pedido de intervalo (Range).",
                            details={"status": response.status_code, "range_start": start},
                        )

                    content_range = response.headers.get("content-range", "")
                    match = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
                    if not match or int(match.group(1)) != start:
                        raise DownloadError(
                            "O servidor devolveu um intervalo de bytes inesperado.",
                            details={"range_start": start, "content_range": content_range},
                        )
                    returned_end = int(match.group(2))
                    response_total = int(match.group(3))
                    if response_total != total or returned_end > end:
                        raise DownloadError(
                            "O servidor devolveu um tamanho ou intervalo inconsistente.",
                            details={
                                "expected_total": total,
                                "response_total": response_total,
                                "requested_end": end,
                                "returned_end": returned_end,
                            },
                        )

                    mode = "ab" if start else "wb"
                    received = 0
                    with part.open(mode) as handle:
                        async for block in iterator:
                            if cancel_event is not None and cancel_event.is_set():
                                handle.flush()
                                raise DownloadCancelledError("Download cancelado")
                            handle.write(block)
                            received += len(block)
                            if self._exceeds_limit(start + received):
                                raise FileTooLargeError(
                                    "Ficheiro excede o tamanho máximo permitido",
                                    details={"max_bytes": self.settings.max_file_size},
                                )
                            if on_progress is not None:
                                await on_progress(
                                    start + received,
                                    total,
                                    start // chunk_size + 1,
                                    -(-total // chunk_size),
                                )
                    expected = returned_end - start + 1
                    if received != expected:
                        start = part.stat().st_size
                        if attempt + 1 >= self.settings.max_retries:
                            raise DownloadError(
                                f"Bloco incompleto ({received}/{expected} bytes)",
                                details={"range_start": start, "range_end": returned_end},
                            )
                        await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                        continue
                    start += received
                    completed_range = True
                    break
                except (DownloadCancelledError, FileTooLargeError):
                    raise
                except Exception:
                    if attempt + 1 >= self.settings.max_retries:
                        raise
                    start = part.stat().st_size if part.exists() else start
                    await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                finally:
                    await response.aclose()

            if not completed_range:
                raise DownloadError(f"Falha ao descarregar o intervalo a partir de {start}")

        if part.stat().st_size != total:
            raise DownloadError(
                f"Ficheiro incompleto ({part.stat().st_size}/{total} bytes)",
            )
        part.replace(target)
        return target

    # ------------------------------------------------------------------ #
    # Paralelo (varios pedidos Range sobre o mesmo recurso)
    # ------------------------------------------------------------------ #
    async def _download_parallel(
        self,
        plan: DownloadPlan,
        *,
        total: int,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        job_id: str,
        plan_provider: PlanProvider | None = None,
    ) -> Path:
        """Descarrega em paralelo, renovando a URL se o CDN responder 403.

        Os chunks já transferidos ficam em disco e são reaproveitados
        quando o plano é renovado: o conteúdo é o mesmo, só muda a
        assinatura da URL.
        """
        current_plan = plan
        for _ in range(self.settings.max_retries):
            try:
                return await self._download_parallel_once(
                    current_plan,
                    total=total,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    job_id=job_id,
                )
            except MediaUrlExpiredError:
                renewed = await self._renew_plan(current_plan, plan_provider)
                if renewed is None:
                    raise
                log.info("URL de media expirada (paralelo); plano renewed")
                current_plan = renewed
        raise MediaUrlExpiredError("A URL de media expirou repetidamente (403)")

    async def _download_parallel_once(
        self,
        plan: DownloadPlan,
        *,
        total: int,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        job_id: str,
    ) -> Path:
        """Descarrega um ficheiro unico em varios pedidos Range em paralelo."""
        if total > self.settings.max_file_size:
            raise FileTooLargeError(
                "Ficheiro excede o tamanho máximo permitido",
                details={"max_bytes": self.settings.max_file_size, "size": total},
            )

        target = self.storage.final_path(plan.filename)
        # Sub-directorio proprio: video e audio do mesmo job nao colidem.
        prefix = hashlib.sha1(plan.filename.encode("utf-8")).hexdigest()[:12]
        parts_dir = self.storage.job_parts_dir(job_id) / prefix
        parts_dir.mkdir(parents=True, exist_ok=True)

        ranges = self._split_ranges(total)
        semaphore = asyncio.Semaphore(self.settings.download_concurrency)
        lock = asyncio.Lock()
        state = {"downloaded": 0, "done": 0}
        errors: list[BaseException] = []

        async def worker(index: int, start: int, end: int) -> None:
            expected = end - start + 1
            chunk_file = parts_dir / f"chunk-{index:05d}.part"

            # Retoma: chunk completo ja esta em disco.
            if (
                self.settings.resume_enabled
                and chunk_file.exists()
                and chunk_file.stat().st_size == expected
            ):
                async with lock:
                    state["downloaded"] += expected
                    state["done"] += 1
                    if on_progress is not None:
                        await on_progress(
                            state["downloaded"], total, state["done"], len(ranges)
                        )
                return

            async with semaphore:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError("Download cancelado")
                written = await self._fetch_range(
                    plan, start, end, chunk_file, cancel_event=cancel_event
                )
                if written != expected:
                    raise DownloadError(
                        f"Chunk {index} incompleto ({written}/{expected} bytes)"
                    )

            async with lock:
                state["downloaded"] += written
                state["done"] += 1
                if on_progress is not None:
                    await on_progress(
                        state["downloaded"], total, state["done"], len(ranges)
                    )

        tasks = [
            asyncio.create_task(worker(index, start, end))
            for index, (start, end) in enumerate(ranges)
        ]
        try:
            await asyncio.gather(*tasks)
        except DownloadCancelledError:
            for task in tasks:
                task.cancel()
            raise
        except RangeUnsupportedError:
            for task in tasks:
                task.cancel()
            raise
        except MediaUrlExpiredError:
            for task in tasks:
                task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            for task in tasks:
                task.cancel()
            log.warning("Download paralelo falhou: %s", exc)

        if errors:
            raise DownloadError(
                f"Falha ao descarregar em paralelo {plan.url}",
                details={"first_error": str(errors[0])},
            )

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelado")

        # Concatenacao por ordem.
        part_file = self.storage.part_path(target)
        with part_file.open("wb") as output:
            for index in range(len(ranges)):
                chunk_file = parts_dir / f"chunk-{index:05d}.part"
                if not chunk_file.exists():
                    raise DownloadError(f"Chunk em falta: {index}")
                with chunk_file.open("rb") as handle:
                    shutil_copy(handle, output)

        written_size = part_file.stat().st_size
        if written_size != total:
            part_file.unlink(missing_ok=True)
            raise DownloadError(
                f"Ficheiro incompleto ({written_size}/{total} bytes)",
            )

        part_file.replace(target)
        self.storage.cleanup_job(job_id)
        return target

    @staticmethod
    def _is_googlevideo(url: str) -> bool:
        """Detect YouTube/Google media CDN hosts (require bounded sequential ranges)."""
        host = (urlsplit(url).hostname or "").lower()
        return host == "googlevideo.com" or host.endswith(".googlevideo.com")

    async def _fetch_range(
        self,
        plan: DownloadPlan,
        start: int,
        end: int,
        target: Path,
        *,
        cancel_event: asyncio.Event | None,
    ) -> int:
        """Descarrega o intervalo ``[start, end]`` para ``target``."""
        for attempt in range(self.settings.max_retries):
            try:
                response, iterator = await self.http.stream(
                    "GET",
                    plan.url,
                    headers=plan.headers,
                    range_start=start,
                    range_end=end,
                )
            except HttpForbiddenError as exc:
                raise MediaUrlExpiredError(
                    "A URL de media expirou (403); é preciso renová-la",
                    details={"url": plan.url},
                ) from exc
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(
                        f"Falha ao descarregar {plan.url}: {exc}"
                    ) from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue

            try:
                if response.status_code == 200:
                    # O servidor ignorou o Range: o path paralelo nao serve.
                    raise RangeUnsupportedError(
                        "O servidor respondeu 200 (ignorou o header Range)"
                    )
                if response.status_code != 206:
                    raise DownloadError(
                        f"Resposta {response.status_code} ao pedir Range",
                        details={"status": response.status_code},
                    )

                content_range = response.headers.get("content-range", "")
                if content_range and not content_range.startswith(f"bytes {start}-"):
                    raise RangeUnsupportedError(
                        f"O servidor devolveu um intervalo inesperado: {content_range}"
                    )

                written = 0
                with target.open("wb") as handle:
                    async for chunk in iterator:
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelledError("Download cancelado")
                        handle.write(chunk)
                        written += len(chunk)
                return written
            except RangeUnsupportedError:
                raise
            except DownloadCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Erro no intervalo %d-%d: %s", start, end, exc)
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(
                        f"Falha ao descarregar {plan.url}: {exc}"
                    ) from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
            finally:
                await response.aclose()

        raise DownloadError(f"Falha ao descarregar {plan.url}")

    def _split_ranges(self, total: int) -> list[tuple[int, int]]:
        """Divide ``total`` bytes em intervalos (inclusivos) por ligacao."""
        min_chunk = max(1, self.settings.parallel_min_chunk_size)
        count = min(
            self.settings.download_concurrency,
            max(2, -(-total // min_chunk)),  # divisao inteira arredondada para cima
        )
        count = max(1, min(count, total))
        size = -(-total // count)  # ceil
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < total:
            end = min(start + size, total) - 1
            ranges.append((start, end))
            start = end + 1
        return ranges

    # ------------------------------------------------------------------ #
    # Segmentado (HLS / DASH)
    # ------------------------------------------------------------------ #
    async def _download_segmented(
        self,
        plan: DownloadPlan,
        *,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        job_id: str,
        plan_provider: PlanProvider | None = None,
    ) -> Path:
        """Descarrega um plano segmentado, renovando a URL em caso de 403."""
        current_plan = plan
        for _ in range(self.settings.max_retries):
            try:
                return await self._download_segmented_once(
                    current_plan,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    job_id=job_id,
                )
            except MediaUrlExpiredError:
                renewed = await self._renew_plan(current_plan, plan_provider)
                if renewed is None:
                    raise
                log.info("URL de media expirada (segmentado); plano renovado")
                current_plan = renewed
        raise MediaUrlExpiredError("A URL de media expirou repetidamente (403)")

    async def _download_segmented_once(
        self,
        plan: DownloadPlan,
        *,
        on_progress: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        job_id: str,
    ) -> Path:
        if len(plan.segments) > self.settings.max_segments:
            raise DownloadError(
                "Demasiados segmentos no plano",
                details={"segments": len(plan.segments), "max": self.settings.max_segments},
            )

        parts_dir = self.storage.job_parts_dir(job_id)
        total = await self._estimate_total(plan)

        downloaded = 0
        completed = 0
        lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(self.settings.download_concurrency)
        errors: list[BaseException] = []

        async def worker(segment: SegmentPlan) -> None:
            nonlocal downloaded, completed
            if cancel_event is not None and cancel_event.is_set():
                return
            async with semaphore:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    size = await self._fetch_segment(segment, parts_dir, plan.headers)
                except DownloadCancelledError:
                    raise
                except MediaUrlExpiredError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    log.error("Falha no segmento %d: %s", segment.index, exc)
                    return

                async with lock:
                    downloaded += size
                    completed += 1
                    if on_progress is not None:
                        await on_progress(downloaded, total, completed, len(plan.segments))

        tasks = [
            asyncio.create_task(worker(segment)) for segment in plan.segments
        ]
        try:
            await asyncio.gather(*tasks)
        except DownloadCancelledError:
            for task in tasks:
                task.cancel()
            raise
        except MediaUrlExpiredError:
            for task in tasks:
                task.cancel()
            raise

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelado")
        if errors:
            raise DownloadError(
                f"Falha ao descarregar {len(errors)} segmento(s)",
                details={"first_error": str(errors[0])},
            )

        # Concatenação (init segments primeiro, uma única vez por URL)
        target = self.storage.final_path(plan.filename)
        written_init: set[str] = set()
        with target.open("wb") as output:
            for segment in plan.segments:
                init_file = self._init_file(parts_dir, segment)
                if init_file.exists() and init_file.name not in written_init:
                    written_init.add(init_file.name)
                    with init_file.open("rb") as handle:
                        shutil_copy(handle, output)
                part_file = self._part_file(parts_dir, segment)
                if not part_file.exists():
                    raise DownloadError(f"Segmento em falta: {segment.index}")
                with part_file.open("rb") as handle:
                    shutil_copy(handle, output)

        self.storage.cleanup_job(job_id)
        return target

    # ------------------------------------------------------------------ #
    async def _fetch_segment(
        self,
        segment: SegmentPlan,
        parts_dir: Path,
        headers: dict[str, str],
    ) -> int:
        """Descarrega (e desencripta) um segmento para ``parts_dir``."""
        part_file = self._part_file(parts_dir, segment)

        # Segmento de inicialização (DASH) — partilhado por vários segmentos.
        if segment.init_url:
            init_file = self._init_file(parts_dir, segment)
            if not init_file.exists():
                await self._fetch_single(
                    segment.init_url,
                    init_file,
                    headers,
                    byte_range=segment.init_byte_range,
                )

        # Retoma: se o ficheiro já existe com o tamanho esperado, salta o download.
        expected = await self._remote_size(segment.url, headers)
        if (
            self.settings.resume_enabled
            and expected is not None
            and part_file.exists()
            and part_file.stat().st_size == expected
        ):
            return expected

        for attempt in range(self.settings.max_retries):
            try:
                # Range sempre presente: URLs com `rqh=1` exigem-no.
                response, iterator = await self.http.stream(
                    "GET", segment.url, headers=headers, range_start=0
                )
            except HttpForbiddenError as exc:
                raise MediaUrlExpiredError(
                    "A URL de media expirou (403); é preciso renová-la",
                    details={"segment_index": segment.index},
                ) from exc
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(f"Falha no segmento {segment.index}: {exc}") from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue

            buffer = bytearray()
            try:
                async for chunk in iterator:
                    buffer.extend(chunk)
                    if self._exceeds_limit(len(buffer)):
                        raise FileTooLargeError("Segmento excede o limite de tamanho")
            except DownloadCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(f"Falha no segmento {segment.index}: {exc}") from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue
            finally:
                await response.aclose()

            data = bytes(buffer)
            if segment.aes_key_hex:
                aes = AesKey.from_material(
                    bytes.fromhex(segment.aes_key_hex),
                    parse_iv(segment.aes_iv_hex),
                    segment.index,
                )
                data = decrypt_segment(data, aes)

            part_file.write_bytes(data)
            return len(data)

        raise DownloadError(f"Falha no segmento {segment.index}")

    async def _fetch_single(
        self,
        url: str,
        target: Path,
        headers: dict[str, str],
        *,
        byte_range: tuple[int, int] | None = None,
    ) -> int:
        """Descarrega um recurso com byte range (usado para init segments)."""
        for attempt in range(self.settings.max_retries):
            try:
                response, iterator = await self.http.stream(
                    "GET", url, headers=headers, range_start=0
                )
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self.settings.max_retries:
                    raise DownloadError(f"Falha em {url}: {exc}") from exc
                await asyncio.sleep(min(self.settings.retry_backoff_base**attempt, 30))
                continue

            data = bytearray()
            try:
                async for chunk in iterator:
                    data.extend(chunk)
            finally:
                await response.aclose()

            if byte_range:
                start, length = byte_range
                data = data[start : start + length]

            target.write_bytes(bytes(data))
            return len(data)

        raise DownloadError(f"Falha em {url}")

    # ------------------------------------------------------------------ #
    async def _remote_size(self, url: str, headers: dict[str, str]) -> int | None:
        try:
            return await self.http.content_length(url, headers=headers)
        except Exception:  # noqa: BLE001
            return None

    async def _estimate_total(self, plan: DownloadPlan) -> int | None:
        """Estima o tamanho total a partir de HEAD de alguns segmentos."""
        if not plan.segments:
            return None
        sample = plan.segments[:5] if len(plan.segments) > 5 else plan.segments
        sizes: list[int] = []
        for segment in sample:
            size = await self._remote_size(segment.url, plan.headers)
            if size:
                sizes.append(size)
        if not sizes:
            return None
        average = sum(sizes) / len(sizes)
        return int(average * len(plan.segments))

    @staticmethod
    def _part_file(parts_dir: Path, segment: SegmentPlan) -> Path:
        name = f"{segment.index:06d}.part" if segment.index >= 0 else "init.part"
        return parts_dir / name

    @staticmethod
    def _init_file(parts_dir: Path, segment: SegmentPlan) -> Path:
        """Nome derivado do URL -> segmentos partilham o mesmo init file."""
        digest = hashlib.sha1((segment.init_url or "").encode("utf-8")).hexdigest()[:12]
        return parts_dir / f"init-{digest}.part"

    @staticmethod
    def _total_from_headers(headers: dict[str, str], fallback: int | None) -> int | None:
        content_range = headers.get("content-range")
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        length = headers.get("content-length")
        if length and length.isdigit():
            return int(length)
        return fallback

    def _exceeds_limit(self, written: int) -> bool:
        return written > self.settings.max_file_size


def shutil_copy(source, destination, buffer_size: int = 1 << 20) -> int:
    """Copia um stream binário em blocos (evita carregar tudo em memória)."""
    copied = 0
    while True:
        buffer = source.read(buffer_size)
        if not buffer:
            break
        destination.write(buffer)
        copied += len(buffer)
    return copied

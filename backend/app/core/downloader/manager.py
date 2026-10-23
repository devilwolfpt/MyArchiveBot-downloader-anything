"""Gestão de jobs de download com progresso em tempo real.

Um :class:`Job` agrega:
  * estado (queued/running/completed/failed/cancelled);
  * a última :class:`ProgressEvent` conhecida;
  * um buffer de eventos recentes (para clientes que lidem com atraso);
  * um ``asyncio.Event`` de cancelamento;
  * uma lista de subscritores (WebSockets) que recebem cada evento.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..exceptions import JobCancelledError, JobNotFoundError
from ..models import JobPhase, JobResult, JobStatus, ProgressEvent

log = logging.getLogger(__name__)

EVENT_BUFFER_SIZE = 50


@dataclass
class Job:
    """Estado interno de um job de download."""

    id: str
    url: str
    status: JobStatus = JobStatus.QUEUED
    phase: JobPhase = JobPhase.IDLE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: ProgressEvent | None = None
    result: JobResult | None = None
    error: dict | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: list[asyncio.Queue[ProgressEvent]] = field(default_factory=list)
    events: deque[ProgressEvent] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER_SIZE))
    task: asyncio.Task | None = None
    title: str | None = None
    filename: str | None = None
    _manager: "JobManager | None" = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status.value,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "filename": self.filename,
            "error": self.error,
            "result": self.result.model_dump() if self.result else None,
            "progress": self.progress.model_dump() if self.progress else None,
        }


class JobManager:
    """Registo de jobs + fan-out de eventos de progresso."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_jobs: int = 200,
        max_concurrent: int = 4,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl = ttl_seconds
        self._max_jobs = max_jobs
        self._max_concurrent = max(1, max_concurrent)
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None
        self._sem_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    def _get_semaphore(self) -> asyncio.Semaphore:
        """Semáforo de concorrência, (re)criado se o loop de eventos mudar.

        Criado de forma preguiçosa para que um ``JobManager`` possa ser
        construído fora de um loop (testes) sem prender a um loop morto.
        """
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._sem_loop is not loop:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
            self._sem_loop = loop
        return self._semaphore

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    # ------------------------------------------------------------------ #
    async def create(self, url: str, *, title: str | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex, url=url, title=title)
        job._manager = self
        async with self._lock:
            self._jobs[job.id] = job
            self._evict_if_needed()
        log.info("Job criado %s (%s)", job.id, url)
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job não encontrado: {job_id}")
        return job

    def find(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    # ------------------------------------------------------------------ #
    async def publish(self, job: Job, event: ProgressEvent) -> None:
        """Publica um evento a todos os subscritores do job."""
        job.progress = event
        job.updated_at = time.time()
        job.events.append(event)
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                # Subscritor lento: descarta o mais antigo.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    pass

    async def update_status(
        self,
        job: Job,
        status: JobStatus,
        *,
        phase: JobPhase | None = None,
        message: str | None = None,
        error: dict | None = None,
    ) -> None:
        job.status = status
        if phase is not None:
            job.phase = phase
        if error is not None:
            job.error = error
        event = ProgressEvent(
            job_id=job.id,
            status=status,
            phase=job.phase,
            percent=job.progress.percent if job.progress else (100.0 if status is JobStatus.COMPLETED else 0.0),
            downloaded_bytes=job.progress.downloaded_bytes if job.progress else 0,
            total_bytes=job.progress.total_bytes if job.progress else None,
            speed=job.progress.speed if job.progress else 0.0,
            eta=job.progress.eta if job.progress else None,
            segment_index=job.progress.segment_index if job.progress else 0,
            segment_total=job.progress.segment_total if job.progress else 0,
            message=message,
            filename=job.filename,
        )
        await self.publish(job, event)

    # ------------------------------------------------------------------ #
    async def subscribe(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        """Gera uma stream de eventos para um job (WebSocket)."""
        job = self.get(job_id)
        queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=200)
        job.subscribers.append(queue)
        try:
            # Replay do buffer para o cliente não perder o início.
            for event in list(job.events):
                yield event
            if job.is_terminal:
                return
            while True:
                event = await queue.get()
                yield event
                if event.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                    return
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.is_terminal:
            return job
        job.cancel_event.set()
        if job.task is not None and not job.task.done():
            job.task.cancel()
        await self.update_status(
            job, JobStatus.CANCELLED, phase=JobPhase.DONE, message="Cancelado pelo utilizador"
        )
        return job

    async def run(self, job: Job, coro_factory) -> JobResult | None:
        """Executa a corrotina de um job e garante o estado terminal.

        Publica sempre um evento final (completed/failed/cancelled) para que
        os subscritores WebSocket terminem em vez de ficarem à espera.

        O número de jobs a executar em simultâneo é limitado por
        ``max_concurrent``; os restantes ficam em estado ``queued`` até haver
        vaga.
        """
        # Registra a task corrente para que :meth:`cancel` consiga
        # interromper o job mesmo que o chamador não a associe ao job.
        current = asyncio.current_task()
        previous_task = job.task
        if current is not None:
            job.task = current
        try:
            semaphore = self._get_semaphore()
            if semaphore.locked():
                await self.update_status(
                    job,
                    JobStatus.QUEUED,
                    phase=JobPhase.QUEUED,
                    message="A aguardar vaga na fila de downloads",
                )
            async with semaphore:
                if job.cancel_event.is_set():
                    raise JobCancelledError("Cancelado enquanto aguardava fila")
                result = await coro_factory(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.phase = JobPhase.DONE
            await self.publish(job, self._terminal_event(job, "Cancelado"))
            raise
        except JobCancelledError as exc:
            job.status = JobStatus.CANCELLED
            job.phase = JobPhase.DONE
            job.error = exc.to_dict()["error"]
            await self.publish(job, self._terminal_event(job, "Cancelado"))
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s falhou", job.id)
            job.status = JobStatus.FAILED
            job.phase = JobPhase.DONE
            job.error = {
                "code": getattr(exc, "code", "internal_error"),
                "message": str(exc),
                "details": getattr(exc, "details", {}),
            }
            await self.publish(job, self._terminal_event(job, str(exc)))
            return None
        finally:
            # Restaura a referência da task (o chamador pode ter definido a sua).
            job.task = previous_task

        job.result = result
        job.status = JobStatus.COMPLETED
        job.phase = JobPhase.DONE
        job.updated_at = time.time()
        await self.publish(job, self._terminal_event(job, "Concluído", percent=100.0))
        return result

    def _terminal_event(
        self, job: Job, message: str, *, percent: float | None = None
    ) -> ProgressEvent:
        progress = job.progress
        return ProgressEvent(
            job_id=job.id,
            status=job.status,
            phase=job.phase,
            percent=percent if percent is not None else (progress.percent if progress else 0.0),
            downloaded_bytes=progress.downloaded_bytes if progress else 0,
            total_bytes=progress.total_bytes if progress else None,
            speed=0.0,
            eta=None,
            segment_index=progress.segment_index if progress else 0,
            segment_total=progress.segment_total if progress else 0,
            message=message,
            filename=job.filename,
        )

    # ------------------------------------------------------------------ #
    def _evict_if_needed(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        ordered = sorted(self._jobs.values(), key=lambda j: j.updated_at)
        for job in ordered[: len(self._jobs) - self._max_jobs]:
            if job.is_terminal:
                self._jobs.pop(job.id, None)

    def purge_expired(self) -> int:
        cutoff = time.time() - self._ttl
        removed = 0
        for job_id, job in list(self._jobs.items()):
            if job.is_terminal and job.updated_at < cutoff:
                self._jobs.pop(job_id, None)
                removed += 1
        return removed

    async def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                job.cancel_event.set()
                job.task.cancel()
                try:
                    await job.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._jobs.clear()


async def run_job(job: Job, coro_factory) -> None:
    """Wrapper funcional (usa :meth:`JobManager.run` quando disponível)."""
    manager = getattr(job, "_manager", None)
    if manager is not None:
        await manager.run(job, coro_factory)
        return
    await coro_factory(job)

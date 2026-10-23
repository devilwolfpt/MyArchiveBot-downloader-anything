"""Núcleo do downloader: planos, chunks, storage e gestão de jobs."""
from .chunk import ChunkDownloader, ProgressTracker
from .crypto import AesKey, decrypt_segment, parse_iv
from .manager import Job, JobManager, run_job
from .plan import DownloadPlan, PlanBuilder, SegmentPlan, sanitize_filename
from .storage import Storage

__all__ = [
    "AesKey",
    "ChunkDownloader",
    "DownloadPlan",
    "Job",
    "JobManager",
    "PlanBuilder",
    "ProgressTracker",
    "SegmentPlan",
    "Storage",
    "decrypt_segment",
    "parse_iv",
    "run_job",
    "sanitize_filename",
]

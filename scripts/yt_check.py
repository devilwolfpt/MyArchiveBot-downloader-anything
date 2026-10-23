"""Verificação ponta-a-ponta contra o YouTube (uso manual).

Descarrega um formato real e valida o ficheiro final com o ffprobe:
streams presentes, duracao e se o MP4 e progressivo (nao fragmentado).

Uso:
    python scripts/yt_check.py                       # 144p (rapido)
    python scripts/yt_check.py https://youtu.be/...  # outro video
    python scripts/yt_check.py --format 137          # 1080p
    python scripts/yt_check.py --mode both          # video + audio
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.core.downloader.manager import JobManager  # noqa: E402
from app.core.downloader.storage import Storage  # noqa: E402
from app.core.models import DownloadMode, DownloadRequest  # noqa: E402
from app.core.postprocess.mp4 import is_fragmented_mp4  # noqa: E402
from app.services.extraction import ExtractionService  # noqa: E402
from app.services.pipeline import DownloadService  # noqa: E402

DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def probe(path: Path) -> dict:
    """Metadados do ficheiro final via ffprobe."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {"erro": proc.stderr[:300]}
    data = json.loads(proc.stdout)
    return {
        "formato": data.get("format", {}).get("format_name"),
        "duracao": round(float(data.get("format", {}).get("duration") or 0), 2),
        "tamanho": int(data.get("format", {}).get("size") or 0),
        "streams": [
            f"{s.get('codec_type')}:{s.get('codec_name')}"
            + (f" {s.get('width')}x{s.get('height')}" if s.get("width") else "")
            for s in data.get("streams", [])
        ],
    }


async def run(url: str, mode: str, format_id: str | None) -> int:
    data_dir = ROOT / ".yt" / "data"
    settings = Settings(
        data_dir=data_dir,
        downloads_dir=data_dir / "downloads",
        cache_dir=data_dir / "cache",
        temp_dir=data_dir / "tmp",
        allow_private_urls=True,
        cache_enabled=False,
        max_file_size=2 * 1024**3,
    )
    settings.ensure_dirs()

    jobs = JobManager()
    extraction = ExtractionService(settings)
    service = DownloadService(settings, jobs, extraction)

    request = DownloadRequest(
        url=url,
        mode=DownloadMode(mode),
        format_id=format_id,
        audio_format="mp3",
    )

    job = await jobs.create(url)
    started = time.time()
    result = await jobs.run(job, lambda j: service.execute(j, request))
    elapsed = time.time() - started

    if result is None:
        print(f"  FALHOU: {job.error}")
        return 1

    path = Path(result.path)
    size = path.stat().st_size
    print(f"  ficheiro  : {path.name}")
    print(f"  tamanho   : {size:,} bytes ({size / 1024**2:.2f} MiB)")
    print(f"  tempo     : {elapsed:.1f}s ({size / 1024 / max(elapsed, 0.001):.0f} KiB/s)")
    print(f"  avisos    : {result.warnings or '(nenhum)'}")
    print(f"  ffprobe   : {json.dumps(probe(path), ensure_ascii=False)}")

    if path.suffix.lower() == ".mp4":
        fragmentado = is_fragmented_mp4(path)
        print(f"  fMP4      : {'SIM (problema!)' if fragmentado else 'nao (progressivo, ok)'}")
        if fragmentado:
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--format", dest="format_id", default=None)
    parser.add_argument("--mode", default="video", choices=["video", "both", "audio"])
    args = parser.parse_args()

    print(f"\n=== {args.mode} {args.format_id or '(auto)'} | {args.url} ===")
    return asyncio.run(run(args.url, args.mode, args.format_id))


if __name__ == "__main__":
    raise SystemExit(main())

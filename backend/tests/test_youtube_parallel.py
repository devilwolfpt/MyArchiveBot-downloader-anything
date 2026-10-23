from __future__ import annotations

import httpx

from app.core.downloader.chunk import ChunkDownloader
from app.core.downloader.plan import DownloadPlan
from app.core.downloader.storage import Storage
from app.core.http.client import HttpClient
from conftest import run


def test_youtube_download_uses_bounded_sequential_ranges(settings) -> None:
    settings.parallel_download = True
    settings.chunk_size = 2
    requests: list[httpx.Request] = []
    content = b"video"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers["range"]
        if range_header.endswith("-"):
            return httpx.Response(403)
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            content=content[start : end + 1],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
                "Content-Length": str(end - start + 1),
            },
        )

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            downloader = ChunkDownloader(
                http,
                settings,
                Storage(settings.downloads_dir, settings.temp_dir),
            )
            plan = DownloadPlan(
                kind="progressive",
                url="https://rr1---sn.googlevideo.com/videoplayback",
                filename="youtube.mp4",
                total_bytes=len(content),
            )
            return await downloader.download(plan, job_id="youtube-bounded-ranges")

    result = run(scenario())

    assert result.read_bytes() == content
    assert [request.headers["range"] for request in requests] == [
        "bytes=0-1",
        "bytes=2-3",
        "bytes=4-4",
    ]
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)


def test_youtube_bounded_ranges_renew_url_after_midstream_403(settings) -> None:
    settings.chunk_size = 2
    requests: list[httpx.Request] = []
    content = b"video"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers["range"]
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        if request.url.host == "old.googlevideo.com" and start >= 2:
            return httpx.Response(403)
        return httpx.Response(
            206,
            content=content[start : end + 1],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
                "Content-Length": str(end - start + 1),
            },
        )

    async def scenario():
        async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
            downloader = ChunkDownloader(
                http,
                settings,
                Storage(settings.downloads_dir, settings.temp_dir),
            )
            original = DownloadPlan(
                kind="progressive",
                url="https://old.googlevideo.com/videoplayback",
                filename="youtube-renewed.mp4",
                total_bytes=len(content),
            )
            renewed = DownloadPlan(
                kind="progressive",
                url="https://new.googlevideo.com/videoplayback",
                filename="youtube-renewed.mp4",
                total_bytes=len(content),
            )

            async def renew():
                return renewed

            return await downloader.download(
                original,
                job_id="youtube-renewed",
                plan_provider=renew,
            )

    result = run(scenario())

    assert result.read_bytes() == content
    assert [request.url.host for request in requests[:2]] == [
        "old.googlevideo.com",
        "old.googlevideo.com",
    ]
    assert [request.headers["range"] for request in requests] == [
        "bytes=0-1",
        "bytes=2-3",
        "bytes=2-3",
        "bytes=4-4",
    ]
    assert [request.url.host for request in requests[2:]] == [
        "new.googlevideo.com",
        "new.googlevideo.com",
    ]

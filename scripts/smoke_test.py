"""Smoke test: sobe um servidor real (uvicorn) e percorre o fluxo HTTP+WS."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import websockets

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

MEDIA = b"Z" * 120_000


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(MEDIA)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/video.mp4"):
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
                body = MEDIA[start:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(MEDIA)-1}/{len(MEDIA)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(MEDIA)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(MEDIA)
            return
        self.send_error(404)


def start_media_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


async def main() -> int:
    httpd, media_port = start_media_server()
    media_url = f"http://127.0.0.1:{media_port}/video.mp4"

    import uvicorn

    from app.config import Settings

    settings = Settings(
        data_dir=Path("./.smoke/data"),
        downloads_dir=Path("./.smoke/data/downloads"),
        cache_dir=Path("./.smoke/data/cache"),
        temp_dir=Path("./.smoke/data/tmp"),
        allow_private_urls=True,
        cache_enabled=False,
        rate_limit_per_minute=1000,
        debug=True,
    )
    settings.ensure_dirs()

    from app.config import get_settings

    get_settings.cache_clear()
    import app.config as app_config

    app_config.get_settings = lambda: settings  # type: ignore[assignment]

    from app.main import create_app

    app = create_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    base = "http://127.0.0.1:8765"
    for _ in range(60):
        try:
            async with httpx.AsyncClient() as c:
                if (await c.get(f"{base}/api/health")).status_code == 200:
                    break
        except Exception:
            pass
        await asyncio.sleep(0.25)
    else:
        print("FAIL: servidor não arrancou")
        server.should_exit = True
        await task
        return 1

    failures = []

    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        # 1. Health
        r = await client.get("/api/health")
        health = r.json()
        print(f"1. health              -> {r.status_code} | extractors={len(health['extractors'])}")

        # 2. Extract
        r = await client.post("/api/extract-info", json={"url": media_url})
        if r.status_code != 200:
            failures.append(f"extract-info: {r.status_code} {r.text[:300]}")
            print(f"2. extract-info        -> FAIL {r.status_code} {r.text[:300]}")
        else:
            info = r.json()
            print(f"2. extract-info        -> {r.status_code} | title={info['title']!r} formats={len(info['formats'])}")
            fmt = info["formats"][0]

            # 3. Create job
            r = await client.post(
                "/api/download",
                json={"url": media_url, "format_id": fmt["id"], "mode": "video"},
            )
            if r.status_code != 202:
                failures.append(f"download: {r.status_code} {r.text[:300]}")
                print(f"3. download            -> FAIL {r.status_code}")
            else:
                job_id = r.json()["job_id"]
                print(f"3. download            -> 202 | job={job_id}")

                # 4. WebSocket progress
                events = []
                try:
                    async with websockets.connect(f"ws://127.0.0.1:8765/api/ws/jobs/{job_id}") as ws:
                        while True:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            evt = json.loads(raw)
                            events.append(evt)
                            if evt.get("status") in ("completed", "failed", "cancelled"):
                                break
                except Exception as exc:
                    failures.append(f"ws: {exc}")

                percents = [e.get("percent", 0) for e in events]
                print(f"4. websocket           -> {len(events)} eventos | percent {percents[:1]}..{percents[-1:]} | status={events[-1].get('status') if events else '?'}")
                if not events:
                    failures.append("ws: sem eventos")
                elif events[-1].get("status") != "completed":
                    failures.append(f"ws: estado final {events[-1].get('status')}")

                # 5. Poll do job
                r = await client.get(f"/api/jobs/{job_id}")
                job = r.json()
                print(f"5. poll job            -> {r.status_code} | status={job['status']} size={job.get('result', {}).get('size')}")

                # 6. Download do ficheiro (com Range)
                r = await client.get(f"/api/download/{job_id}", headers={"Range": "bytes=0-99"})
                head = r.content
                print(f"6. download ficheiro   -> {r.status_code} | range={r.headers.get('content-range')} bytes={len(head)}")
                if r.status_code != 206 or len(head) != 100:
                    failures.append(f"download file: {r.status_code} len={len(head)}")

                r = await client.get(f"/api/download/{job_id}")
                total = r.content
                print(f"7. download completo   -> {r.status_code} | {len(total)} bytes | igual ao original: {total == MEDIA}")
                if total != MEDIA:
                    failures.append(f"conteúdo diferente: {len(total)} != {len(MEDIA)}")

                # 8. Job inexistente
                r = await client.get("/api/jobs/nao-existe")
                print(f"8. job inexistente     -> {r.status_code}")
                if r.status_code != 404:
                    failures.append(f"job inexistente: {r.status_code}")

                # 9. Contrato novo de selecao (auto + video/audio/legendas)
                r = await client.post(
                    "/api/download",
                    json={
                        "url": media_url,
                        "mode": "auto",
                        "video_format_id": fmt["id"],
                        "audio_format_id": None,
                        "subtitle_lang": "auto",
                    },
                )
                if r.status_code != 202:
                    failures.append(f"download auto: {r.status_code} {r.text[:300]}")
                    print(f"9. download auto       -> FAIL {r.status_code}")
                else:
                    auto_job = r.json()["job_id"]
                    try:
                        async with websockets.connect(
                            f"ws://127.0.0.1:8765/api/ws/jobs/{auto_job}"
                        ) as ws:
                            while True:
                                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                                if evt.get("status") in ("completed", "failed", "cancelled"):
                                    break
                    except Exception as exc:
                        failures.append(f"ws auto: {exc}")
                    r = await client.get(f"/api/jobs/{auto_job}")
                    auto_state = r.json()
                    auto_status = auto_state.get("status")
                    print(f"10. download auto      -> {auto_status} | tamanho={auto_state.get('result', {}).get('size')}")
                    if auto_status != "completed":
                        failures.append(f"download auto: estado {auto_status}")

    server.should_exit = True
    await task
    httpd.shutdown()

    print()
    if failures:
        print("FALHAS:")
        for f in failures:
            print("  -", f)
        return 1
    print("SMOKE TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""FastAPI server behind the `listen` command.

Lifecycle / tab-close rule (per product spec):
  * The **Exit** button (POST /api/exit) is the only thing that stops a *live*
    recording: it saves, finalizes, and shuts the server down.
  * Closing the browser tab while **recording** -> server + capture keep running
    (reopening the URL reconnects to the same session).
  * Closing the tab while **idle** -> the app shuts down after a short grace
    period (so a reload doesn't kill it).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from am_i_audible import __version__
from am_i_audible.core.controller import CaptureController

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TELEMETRY_HZ = 30
IDLE_SHUTDOWN_GRACE_S = 2.0


class AppState:
    def __init__(self):
        self.controller = CaptureController()
        self.clients: set[WebSocket] = set()
        self.server: uvicorn.Server | None = None
        self._shutdown_task: asyncio.Task | None = None

    def request_shutdown(self) -> None:
        if self.server:
            self.server.should_exit = True

    async def on_client_gone(self) -> None:
        """Apply the tab-close rule when a websocket disconnects."""
        if self.clients or self.controller.is_recording:
            return  # someone still watching, or a live recording -> stay up
        # idle + no clients: shut down after a grace period (survives reloads)
        await asyncio.sleep(IDLE_SHUTDOWN_GRACE_S)
        if not self.clients and not self.controller.is_recording:
            log.info("idle and no clients -> shutting down")
            self.request_shutdown()


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="am-I-audible", version=__version__)
    ctrl = state.controller

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status():
        return ctrl.status()

    @app.post("/api/start")
    async def start(body: dict | None = None):
        body = body or {}
        return ctrl.start(
            record_mic=body.get("recordMic", True),
            record_system=body.get("recordSystem", True),
            label=body.get("label"),
        )

    @app.post("/api/swap-mic")
    async def swap_mic(body: dict):
        return ctrl.swap_mic(body["target"])

    @app.post("/api/stop")
    async def stop(body: dict | None = None):
        return ctrl.stop((body or {}).get("name"))

    @app.post("/api/exit")
    async def exit_app(body: dict | None = None):
        result = ctrl.stop((body or {}).get("name"))
        state.request_shutdown()
        return result

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        state.clients.add(websocket)
        try:
            await websocket.send_json({"type": "status", "data": ctrl.status()})
            while True:
                await websocket.send_json(
                    {"type": "telemetry", "data": ctrl.telemetry()})
                await asyncio.sleep(1 / TELEMETRY_HZ)
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(websocket)
            asyncio.create_task(state.on_client_gone())

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    state = AppState()
    app = create_app(state)

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    state.server = uvicorn.Server(config)

    def open_browser():
        # tiny delay so the server is accepting connections first
        import time
        time.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"\n  am-I-audible {__version__}  →  {url}\n  (opening your browser; close the tab when idle to quit)\n")
    state.server.run()
    # ensure any in-flight recording is torn down on exit
    if state.controller.is_recording:
        state.controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

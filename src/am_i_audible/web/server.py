"""FastAPI server behind the `listen` command.

Lifecycle / tab-close rule:
  * The **Exit** button (POST /api/exit) is the only thing that stops a *live*
    recording: it saves, finalizes, and shuts the server down.
  * Closing the tab while **recording** -> server + capture keep running.
  * Closing the tab while **idle** -> shut down after a short grace period.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from am_i_audible import __version__, config
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

    def request_shutdown(self) -> None:
        if self.server:
            self.server.should_exit = True

    async def on_client_gone(self) -> None:
        if self.clients or self.controller.is_recording:
            return
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
        return ctrl.start(record_mic=body.get("recordMic", True),
                          record_system=body.get("recordSystem", True),
                          label=body.get("label"),
                          mode=body.get("mode", "live"))

    @app.post("/api/pause")
    async def pause():
        return ctrl.pause()

    @app.post("/api/resume")
    async def resume():
        return ctrl.resume()

    @app.post("/api/swap-mic")
    async def swap_mic(body: dict):
        return ctrl.swap_mic(body["target"])

    @app.post("/api/gain")
    async def gain(body: dict):
        return ctrl.set_gain(body["name"], float(body["value"]))

    @app.post("/api/marker")
    async def marker(body: dict | None = None):
        return ctrl.add_marker((body or {}).get("label"))

    @app.get("/api/settings")
    async def get_settings():
        return ctrl.settings

    @app.post("/api/settings")
    async def set_settings(body: dict):
        return ctrl.update_settings(body)

    @app.post("/api/stop")
    async def stop(body: dict | None = None):
        return ctrl.stop((body or {}).get("name"))

    @app.post("/api/exit")
    async def exit_app(body: dict | None = None):
        result = ctrl.stop((body or {}).get("name"))
        state.request_shutdown()
        return result

    @app.get("/api/sessions")
    async def sessions():
        return ctrl.list_sessions()

    @app.post("/api/transcribe")
    async def transcribe_session(body: dict):
        return await asyncio.to_thread(ctrl.transcribe_file, body["name"])

    @app.get("/api/transcript/{name}")
    async def transcript(name: str):
        p = _safe_session_path(name) / "transcript.md"
        if not p.exists():
            raise HTTPException(404, "no transcript")
        return PlainTextResponse(p.read_text())

    @app.get("/api/recording/{name}/{filename}")
    async def recording(name: str, filename: str):
        p = _safe_session_path(name) / filename
        if p.suffix != ".wav" or not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p, media_type="audio/wav")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        state.clients.add(websocket)
        try:
            await websocket.send_json({"type": "status", "data": ctrl.status()})
            while True:
                await websocket.send_json({"type": "telemetry", "data": ctrl.telemetry()})
                await asyncio.sleep(1 / TELEMETRY_HZ)
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(websocket)
            asyncio.create_task(state.on_client_gone())

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _safe_session_path(name: str) -> Path:
    """Resolve a session dir, refusing path traversal outside RECORDINGS_ROOT."""
    root = config.RECORDINGS_ROOT.resolve()
    p = (root / name).resolve()
    if root not in p.parents and p != root:
        raise HTTPException(400, "invalid path")
    return p


# --------------------------------------------------------------------------- #
# .desktop launcher                                                           #
# --------------------------------------------------------------------------- #
def install_desktop(extra_args: str = "") -> Path:
    apps = Path.home() / ".local/share/applications"
    apps.mkdir(parents=True, exist_ok=True)
    icon = STATIC_DIR / "icon.svg"
    entry = apps / "am-i-audible.desktop"
    listen_bin = Path.home() / ".local/bin/listen"
    exec_cmd = str(listen_bin) if listen_bin.exists() else "listen"
    if extra_args:
        exec_cmd = f"{exec_cmd} {extra_args}"
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=am-I-audible\n"
        "GenericName=Meeting Recorder\n"
        "Comment=Record & transcribe meetings (mic + system audio)\n"
        f"Exec={exec_cmd}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Recorder;\n"
        "Keywords=record;transcribe;meeting;audio;\n"
    )
    entry.chmod(0o755)
    return entry


def enable_gpu() -> int:
    """Install the NVIDIA cuBLAS/cuDNN wheels so faster-whisper can use the GPU."""
    import subprocess
    import sys
    print("Installing nvidia-cublas-cu12 + nvidia-cudnn-cu12 (large download)…")
    rc = subprocess.call([sys.executable, "-m", "pip", "install",
                          "nvidia-cublas-cu12", "nvidia-cudnn-cu12"])
    if rc == 0:
        print("Done. Run `listen --gpu-check` to confirm CUDA works.")
    return rc


def gpu_check() -> int:
    from am_i_audible.audio import transcribe
    if not transcribe.available():
        print("faster-whisper not installed (pip install faster-whisper)")
        return 1
    eng = transcribe.TranscriptionEngine(model_size="tiny", device="cuda")
    if eng.start():
        print(f"✓ CUDA works — transcription will use: {eng.active_device}")
        return 0
    print(f"✗ CUDA unavailable, will use CPU.\n  {eng.error}")
    print("  Try: listen --enable-gpu")
    return 0


# --------------------------------------------------------------------------- #
# entry point                                                                 #
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _try_native_window(url: str) -> bool:
    """Open a dedicated desktop window via pywebview. False if unavailable."""
    try:
        import webview  # pywebview
    except Exception:
        return False
    try:
        webview.create_window("am-I-audible", url, width=1180, height=760)
        webview.start()
        return True
    except Exception as exc:
        log.warning("native window failed (%s); falling back to browser", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="listen", description="am-I-audible web UI")
    parser.add_argument("--port", type=int, default=0, help="port (default: a free one)")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    parser.add_argument("--window", action="store_true",
                        help="open in a native desktop window (needs pywebview)")
    parser.add_argument("--autostart", action="store_true",
                        help="start recording immediately on launch")
    parser.add_argument("--install-desktop", action="store_true",
                        help="install an app-drawer launcher and exit")
    parser.add_argument("--enable-gpu", action="store_true",
                        help="install NVIDIA CUDA libs for GPU transcription and exit")
    parser.add_argument("--gpu-check", action="store_true",
                        help="report whether GPU transcription is available and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.enable_gpu:
        return enable_gpu()
    if args.gpu_check:
        return gpu_check()
    if args.install_desktop:
        extra = " ".join(a for a, on in
                         (("--window", args.window), ("--autostart", args.autostart)) if on)
        entry = install_desktop(extra)
        print(f"Installed launcher: {entry}\nLook for “am-I-audible” in your app drawer.")
        return 0

    state = AppState()
    app = create_app(state)
    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}"
    state.server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    print(f"\n  am-I-audible {__version__}  →  {url}\n"
          f"  (Exit & save in the UI stops a recording; closing an idle tab quits)\n")

    if args.autostart:
        threading.Timer(1.0, lambda: state.controller.start()).start()

    if args.window:
        # server in a background thread; native window owns the main thread
        t = threading.Thread(target=state.server.run, daemon=True)
        t.start()
        if _try_native_window(url):
            state.request_shutdown()
            t.join(timeout=3)
            if state.controller.is_recording:
                state.controller.stop()
            return 0
        # fell back: keep server running, open a browser instead
        if not args.no_browser:
            webbrowser.open(url)
        t.join()
    else:
        if not args.no_browser and not os.environ.get("AMIA_NO_BROWSER"):
            threading.Thread(
                target=lambda: (__import__("time").sleep(0.6), webbrowser.open(url)),
                daemon=True).start()
        state.server.run()

    if state.controller.is_recording:
        state.controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

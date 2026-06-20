"""Stateful capture controller driving the web UI.

Wraps :class:`AudioRouter` + :class:`DualTrackRecorder` behind a small,
thread-safe, call-from-anywhere API the FastAPI server uses:

    start(...) -> begin a session   status() -> dict for the UI
    stop(name) -> finalize + rename telemetry() -> live levels + waveform points
    swap_mic(target) -> gapless mic device switch

Unlike the terminal ``RecordingSession`` (which owns a blocking meter loop), the
controller is event-driven: the server calls it from request handlers and polls
``telemetry()`` over a WebSocket.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from am_i_audible import config
from am_i_audible.audio.recorder import DualTrackRecorder
from am_i_audible.audio.router import AudioRouter

log = logging.getLogger(__name__)


class CaptureController:
    def __init__(self):
        self._lock = threading.RLock()
        self._router: AudioRouter | None = None
        self._recorder: DualTrackRecorder | None = None
        self._out_dir: Path | None = None
        self._state = "idle"  # idle | recording

    # -- queries ----------------------------------------------------------- #
    @property
    def is_recording(self) -> bool:
        return self._state == "recording"

    def status(self) -> dict:
        with self._lock:
            mics = []
            current_mic = None
            if self._router:
                try:
                    mics = self._router.list_microphones()
                    current_mic = self._router.current_mic
                except Exception:  # backend hiccup shouldn't crash the UI
                    pass
            return {
                "state": self._state,
                "backend": self._router.backend_name if self._router else None,
                "currentMic": current_mic,
                "microphones": mics,
                "seconds": self._recorder.seconds if self._recorder else 0.0,
                "outDir": str(self._out_dir) if self._out_dir else None,
                "tracks": [t.name for t in self._recorder.tracks] if self._recorder else [],
            }

    def telemetry(self) -> dict:
        with self._lock:
            if not self._recorder:
                return {"state": self._state, "seconds": 0.0, "tracks": {}}
            return {
                "state": self._state,
                "seconds": self._recorder.seconds,
                "tracks": {
                    t.name: {
                        "level": t.level,
                        "peak": t.peak,
                        "env": t.drain_envelope(),
                    }
                    for t in self._recorder.tracks
                },
            }

    # -- commands ---------------------------------------------------------- #
    def start(self, *, record_mic: bool = True, record_system: bool = True,
              label: str | None = None) -> dict:
        with self._lock:
            if self.is_recording:
                return self.status()
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self._out_dir = config.RECORDINGS_ROOT / (
                f"{stamp}_{label}" if label else stamp)
            self._router = AudioRouter()
            routes = self._router.setup()
            self._recorder = DualTrackRecorder(
                mic_monitor=routes.mic_monitor,
                system_monitor=routes.system_monitor,
                out_dir=self._out_dir,
                record_mic=record_mic,
                record_system=record_system,
            )
            self._recorder.start()
            self._state = "recording"
            log.info("controller: recording -> %s", self._out_dir)
            return self.status()

    def swap_mic(self, target: str) -> dict:
        with self._lock:
            if self._router and self.is_recording:
                self._router.swap_mic(target)
            return self.status()

    def stop(self, save_name: str | None = None) -> dict:
        with self._lock:
            if not self.is_recording:
                return {"saved": None, **self.status()}
            if self._recorder:
                self._recorder.stop()
            if self._router:
                self._router.teardown()
            saved_dir = self._finalize_name(save_name)
            self._state = "idle"
            self._recorder = None
            self._router = None
            self._out_dir = None
            log.info("controller: stopped -> %s", saved_dir)
            return {"saved": str(saved_dir) if saved_dir else None, **self.status()}

    # -- helpers ----------------------------------------------------------- #
    def _finalize_name(self, save_name: str | None) -> Path | None:
        """Rename the session folder to a user-supplied name, if given/safe."""
        if not self._out_dir or not self._out_dir.exists():
            return self._out_dir
        if not save_name:
            return self._out_dir
        safe = "".join(c for c in save_name if c.isalnum() or c in " -_.").strip()
        safe = safe.replace(" ", "_")
        if not safe:
            return self._out_dir
        target = self._out_dir.parent / safe
        if target.exists():  # avoid clobbering: append the timestamp
            target = self._out_dir.parent / f"{safe}_{self._out_dir.name}"
        self._out_dir.rename(target)
        return target

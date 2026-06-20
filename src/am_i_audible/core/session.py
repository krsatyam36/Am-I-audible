"""Recording session: wire router + recorder + meters into one lifecycle.

    router.setup()  -> two stable monitor sources
    recorder.start() -> dual-track WAV capture from those monitors
    meter loop       -> live VU + timer until the user stops
    [s] hot-swap mic -> router.swap_mic() (capture never breaks)
    stop             -> recorder.stop(), router.teardown(), print summary
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from am_i_audible import config
from am_i_audible.audio.recorder import DualTrackRecorder
from am_i_audible.audio.router import AudioRouter
from am_i_audible.ui.meters import MeterDisplay

log = logging.getLogger(__name__)


def _session_dir(label: str | None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"{stamp}_{label}" if label else stamp
    return config.RECORDINGS_ROOT / name


def _stdin_commands(q: "queue.Queue[str]", stop: threading.Event) -> None:
    """Forward single-letter line commands ([s]/[q]) onto a queue."""
    for line in sys.stdin:
        if stop.is_set():
            break
        cmd = line.strip().lower()
        if cmd:
            q.put(cmd[0])


class RecordingSession:
    def __init__(self, label: str | None = None,
                 record_mic: bool = True, record_system: bool = True,
                 duration: float | None = None):
        self.label = label
        self.record_mic = record_mic
        self.record_system = record_system
        self.duration = duration
        self.out_dir = _session_dir(label)

    def run(self) -> Path:
        router = AudioRouter()
        routes = router.setup()
        recorder = DualTrackRecorder(
            mic_monitor=routes.mic_monitor,
            system_monitor=routes.system_monitor,
            out_dir=self.out_dir,
            record_mic=self.record_mic,
            record_system=self.record_system,
        )
        recorder.start()

        stop = threading.Event()
        cmd_q: "queue.Queue[str]" = queue.Queue()
        interactive = sys.stdin.isatty()
        if interactive:
            threading.Thread(target=_stdin_commands, args=(cmd_q, stop),
                            daemon=True).start()

        period = 1.0 / config.METER_REFRESH_HZ
        try:
            with MeterDisplay(recorder, router.current_mic or "?") as meter:
                while not stop.is_set():
                    meter.update()
                    if self.duration and recorder.seconds >= self.duration:
                        break
                    if (err := recorder.first_error()) is not None:
                        meter.status = f"capture error: {err}"
                        meter.update()
                        break
                    try:
                        cmd = cmd_q.get(timeout=period)
                    except queue.Empty:
                        continue
                    if cmd == "q":
                        break
                    if cmd == "s":
                        self._swap_mic(router, meter)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            recorder.stop()
            router.teardown()

        self._print_summary()
        return self.out_dir

    def _swap_mic(self, router: AudioRouter, meter: MeterDisplay) -> None:
        mics = router.list_microphones()
        others = [m for m in mics if m != router.current_mic]
        if not others:
            meter.status = "no other microphone available to swap to"
            return
        target = others[0]
        router.swap_mic(target)
        meter.update(mic_source=target)
        meter.status = f"swapped mic -> {target}"

    def _print_summary(self) -> None:
        print(f"\nSaved recording to: {self.out_dir}")
        for f in sorted(self.out_dir.glob("*.wav")):
            kb = f.stat().st_size / 1024
            print(f"  {f.name}  ({kb:,.0f} KB)")

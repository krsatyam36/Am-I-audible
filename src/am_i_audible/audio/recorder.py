"""Dual-track capture: one ``pw-record`` per monitor source, streamed to WAV.

Each track runs ``pw-record --target <monitor> ... -`` which emits continuous
raw s16 mono PCM on stdout (verified continuous and gap-free on this stack). A
reader thread drains stdout in ~100 ms chunks and, for every chunk:

  * appends it to the track's WAV file via ``soundfile`` (flushed each chunk so
    a crash loses at most one chunk), and
  * computes an RMS level the UI reads for VU meters.

Capturing from the stable ``*.monitor`` sources (fed by the router's loopbacks)
means a microphone hot-swap never interrupts these streams -- the heart of the
gapless requirement.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from am_i_audible import config

log = logging.getLogger(__name__)

_FULL_SCALE = 32768.0  # s16


class CaptureError(RuntimeError):
    pass


class TrackCapture:
    """Capture a single monitor source to one WAV file on a background thread."""

    def __init__(self, name: str, target: str, out_path: Path):
        self.name = name
        self.target = target
        self.out_path = out_path
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._file: sf.SoundFile | None = None
        self._stop = threading.Event()
        self._level = 0.0          # latest RMS, 0..1
        self._frames_written = 0
        self._error: Exception | None = None

    # -- public API -------------------------------------------------------- #
    @property
    def level(self) -> float:
        return self._level

    @property
    def seconds(self) -> float:
        return self._frames_written / config.SAMPLE_RATE

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = sf.SoundFile(
            self.out_path, mode="w",
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            subtype="PCM_16",
        )
        self._proc = subprocess.Popen(
            [
                "pw-record", "--target", self.target,
                "--rate", str(config.SAMPLE_RATE),
                "--channels", str(config.CHANNELS),
                "--format", "s16",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._thread = threading.Thread(
            target=self._pump, name=f"capture-{self.name}", daemon=True)
        self._thread.start()
        log.info("capture[%s]: recording %s -> %s",
                 self.name, self.target, self.out_path)

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._thread:
            self._thread.join(timeout=5)
        if self._file:
            self._file.close()  # finalises the WAV header
            self._file = None
        log.info("capture[%s]: stopped (%.1fs written)", self.name, self.seconds)

    # -- worker ------------------------------------------------------------ #
    def _pump(self) -> None:
        chunk_bytes = config.CHUNK_FRAMES * config.BYTES_PER_FRAME
        assert self._proc and self._proc.stdout and self._file
        try:
            while not self._stop.is_set():
                buf = self._proc.stdout.read(chunk_bytes)
                if not buf:
                    break  # pw-record exited
                samples = np.frombuffer(buf, dtype=np.int16)
                self._file.write(samples)
                self._file.flush()
                self._frames_written += samples.size
                self._level = float(
                    np.sqrt(np.mean((samples.astype(np.float32) / _FULL_SCALE) ** 2))
                ) if samples.size else 0.0
        except Exception as exc:  # surface to the session without killing it
            self._error = exc
            log.error("capture[%s] failed: %s", self.name, exc)


class DualTrackRecorder:
    """Starts/stops both track captures together."""

    def __init__(self, mic_monitor: str, system_monitor: str, out_dir: Path,
                 record_mic: bool = True, record_system: bool = True):
        self.out_dir = out_dir
        self.tracks: list[TrackCapture] = []
        if record_mic:
            self.tracks.append(TrackCapture(
                "mic", mic_monitor, out_dir / config.MIC_TRACK_FILENAME))
        if record_system:
            self.tracks.append(TrackCapture(
                "system", system_monitor, out_dir / config.SYSTEM_TRACK_FILENAME))
        if not self.tracks:
            raise CaptureError("nothing to record: mic and system both disabled")

    def start(self) -> None:
        for t in self.tracks:
            t.start()

    def stop(self) -> None:
        for t in self.tracks:
            t.stop()

    @property
    def seconds(self) -> float:
        return max((t.seconds for t in self.tracks), default=0.0)

    def first_error(self) -> Exception | None:
        return next((t.error for t in self.tracks if t.error), None)

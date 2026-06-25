"""Dual-track capture: one capture process per monitor source, streamed to WAV.

Each track runs a backend-supplied capture command (``parec --device`` on the
pactl backend, ``pw-record --target`` on the pipewire-native one) that emits
continuous raw s16 mono PCM on stdout. The pactl distinction matters: a
null-sink ``*.monitor`` is a PulseAudio source name that ``pw-record`` cannot
resolve -- it falls back to the default mic, making every track identical -- so
the backend chooses the tool that resolves its own monitor names. A reader
thread drains stdout in ~100 ms chunks and, for every chunk:

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
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf

from am_i_audible import config

log = logging.getLogger(__name__)

_FULL_SCALE = 32768.0  # s16
# Waveform envelope resolution: one peak per ~10 ms window.
_ENV_WINDOW_FRAMES = 480
_ENV_MAXLEN = 6000  # ~60 s of backlog if nothing drains


class CaptureError(RuntimeError):
    pass


def _default_capture_argv(target: str) -> list[str]:
    """Fallback capture command (pw-record) for callers without a backend.

    Note: pw-record cannot resolve pactl null-sink ``*.monitor`` source names,
    so real sessions pass the backend-supplied argv via the router instead.
    """
    return [
        "pw-record", "--target", target,
        "--rate", str(config.SAMPLE_RATE),
        "--channels", str(config.CHANNELS),
        "--format", "s16",
        "-",
    ]


class TrackCapture:
    """Capture a single monitor source to one WAV file on a background thread."""

    def __init__(self, name: str, target: str, out_path: Path,
                 capture_argv=None):
        self.name = name
        self.target = target
        self.out_path = out_path
        # How to launch the capture process for this monitor. Backend-supplied
        # (parec on pactl, pw-record on pipewire-native); falls back to
        # pw-record for callers that don't pass one.
        self._capture_argv = capture_argv or _default_capture_argv
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._file: sf.SoundFile | None = None
        self._stop = threading.Event()
        self._level = 0.0          # latest RMS, 0..1
        self._peak = 0.0           # latest window peak, 0..1
        self._frames_written = 0
        self._error: Exception | None = None
        self._gain = 1.0           # software gain multiplier
        self._paused = False
        self._tap = None           # optional callable(name, int16 ndarray)
        # Waveform envelope: peak-per-window points awaiting the UI to drain.
        self._env: deque[float] = deque(maxlen=_ENV_MAXLEN)
        self._env_lock = threading.Lock()

    # -- public API -------------------------------------------------------- #
    @property
    def level(self) -> float:
        return self._level

    @property
    def peak(self) -> float:
        return self._peak

    @property
    def gain(self) -> float:
        return self._gain

    def set_gain(self, value: float) -> None:
        self._gain = max(0.0, min(8.0, float(value)))

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def set_tap(self, tap) -> None:
        self._tap = tap

    @property
    def seconds(self) -> float:
        return self._frames_written / config.SAMPLE_RATE

    def drain_envelope(self) -> list[float]:
        """Return and clear the peak-per-window points buffered since last call."""
        with self._env_lock:
            points = list(self._env)
            self._env.clear()
        return points

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
            self._capture_argv(self.target),
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
                if self._paused:
                    # keep draining the pipe (no backpressure) but write nothing
                    self._level = 0.0
                    continue
                samples = np.frombuffer(buf, dtype=np.int16)
                if self._gain != 1.0 and samples.size:
                    samples = np.clip(
                        samples.astype(np.float32) * self._gain, -32768, 32767
                    ).astype(np.int16)
                self._file.write(samples)
                self._file.flush()
                self._frames_written += samples.size
                if samples.size:
                    norm = samples.astype(np.float32) / _FULL_SCALE
                    self._level = float(np.sqrt(np.mean(norm ** 2)))
                    self._peak = float(np.max(np.abs(norm)))
                    self._append_envelope(norm)
                    if self._tap is not None:
                        try:
                            self._tap(self.name, samples)
                        except Exception:  # a bad tap must never break capture
                            pass
        except Exception as exc:  # surface to the session without killing it
            self._error = exc
            log.error("capture[%s] failed: %s", self.name, exc)

    def _append_envelope(self, norm: np.ndarray) -> None:
        """Append one peak value per ~10 ms window of this chunk."""
        n = norm.size // _ENV_WINDOW_FRAMES
        if n:
            windows = norm[: n * _ENV_WINDOW_FRAMES].reshape(n, _ENV_WINDOW_FRAMES)
            peaks = np.max(np.abs(windows), axis=1)
            with self._env_lock:
                self._env.extend(float(p) for p in peaks)


class DualTrackRecorder:
    """Starts/stops both track captures together."""

    def __init__(self, mic_monitor: str, system_monitor: str, out_dir: Path,
                 record_mic: bool = True, record_system: bool = True,
                 capture_argv=None):
        self.out_dir = out_dir
        self.tracks: list[TrackCapture] = []
        if record_mic:
            self.tracks.append(TrackCapture(
                "mic", mic_monitor, out_dir / config.MIC_TRACK_FILENAME,
                capture_argv))
        if record_system:
            self.tracks.append(TrackCapture(
                "system", system_monitor, out_dir / config.SYSTEM_TRACK_FILENAME,
                capture_argv))
        if not self.tracks:
            raise CaptureError("nothing to record: mic and system both disabled")

    def start(self) -> None:
        for t in self.tracks:
            t.start()

    def stop(self) -> None:
        for t in self.tracks:
            t.stop()

    def set_paused(self, paused: bool) -> None:
        for t in self.tracks:
            t.set_paused(paused)

    def set_gain(self, name: str, value: float) -> None:
        for t in self.tracks:
            if t.name == name:
                t.set_gain(value)

    def set_tap(self, tap) -> None:
        for t in self.tracks:
            t.set_tap(tap)

    def track(self, name: str) -> "TrackCapture | None":
        return next((t for t in self.tracks if t.name == name), None)

    @property
    def seconds(self) -> float:
        return max((t.seconds for t in self.tracks), default=0.0)

    def first_error(self) -> Exception | None:
        return next((t.error for t in self.tracks if t.error), None)

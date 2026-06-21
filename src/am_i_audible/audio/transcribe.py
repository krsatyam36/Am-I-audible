"""Near-real-time + batch transcription, engine-agnostic.

The streaming engine taps each recording track's PCM, mixes the active tracks
into one mono timeline, and transcribes fixed windows via a pluggable
:class:`~am_i_audible.audio.engines.Recognizer`. Whisper needs context, so this
is *near*-live (a window behind). Whichever STT backend is selected, the offset
and speaker-label bookkeeping here is the same.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np

from am_i_audible import config
from am_i_audible.audio import engines
from am_i_audible.audio.engines import to_16k as _to_16k  # noqa: F401  (tests/back-compat)

log = logging.getLogger(__name__)

# Track filename / track name -> human speaker label (diarization-lite).
SPEAKER_LABELS = {"mic.wav": "You", "system.wav": "Others"}


def available(engine: str = "whisper") -> bool:
    cls = engines.REGISTRY.get(engine, engines.WhisperRecognizer)
    return cls.available()


class _Track:
    __slots__ = ("buf", "prev_text", "consumed", "label")

    def __init__(self, label: str):
        self.buf: deque = deque()
        self.prev_text = ""
        self.consumed = 0
        self.label = label


class TranscriptionEngine:
    """One shared recognizer; per-track labeled buffers (You / Others).

    A single model instance serves every track (no 2× VRAM), and each track's
    audio is transcribed in its own ~window-second slices with its own speaker
    label, rolling context, and timeline offset.
    """

    def __init__(self, labels: dict[str, str], on_segment=None,
                 model_size: str | None = None, device: str | None = None,
                 language: str | None = None, window_seconds: float | None = None,
                 engine: str = "whisper"):
        self.on_segment = on_segment
        self.engine = engine
        self.model_size = model_size
        self.device = device or config.STT_DEVICE
        self.language = language if language is not None else config.STT_LANGUAGE
        self.window = int((window_seconds or config.STT_WINDOW_SECONDS) * config.SAMPLE_RATE)
        self.segments: list[dict] = []
        self.error: str | None = None
        self.active_device: str | None = None
        self._rec = None
        self._tracks: dict[str, _Track] = {n: _Track(lbl) for n, lbl in labels.items()}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> bool:
        cls = engines.REGISTRY.get(self.engine, engines.WhisperRecognizer)
        if not cls.available():
            self.error = f"{cls.title} not installed ({cls.install_hint})"
            log.warning(self.error)
            return False
        try:
            self._rec = engines.get_recognizer(
                self.engine, self.model_size, self.device, self.language)
            self._rec.load()
            self.active_device = self._rec.active_device
        except Exception as exc:
            self.error = f"could not load {cls.title}: {exc}"
            log.error(self.error)
            return False
        self._thread = threading.Thread(target=self._worker, name="stt", daemon=True)
        self._thread.start()
        log.info("STT started (engine=%s device=%s)", self.engine, self.active_device)
        return True

    def feed(self, name: str, samples: np.ndarray) -> None:
        if self._rec is None:
            return
        with self._lock:
            t = self._tracks.get(name)
            if t is not None:
                t.buf.append(samples.copy())

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)
        return self.segments

    # -- worker ------------------------------------------------------------ #
    @staticmethod
    def _avail(t: _Track) -> int:
        return sum(a.size for a in t.buf)

    @staticmethod
    def _pull(t: _Track, frames: int) -> np.ndarray:
        got, chunks = 0, []
        while got < frames and t.buf:
            a = t.buf.popleft()
            if got + a.size <= frames:
                chunks.append(a); got += a.size
            else:
                need = frames - got
                chunks.append(a[:need]); t.buf.appendleft(a[need:]); got = frames
        return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)

    def _worker(self) -> None:
        while not self._stop.is_set():
            did = False
            for t in self._tracks.values():
                with self._lock:
                    audio = self._pull(t, self.window) if self._avail(t) >= self.window else None
                if audio is not None:
                    self._run(t, audio); did = True
            if not did:
                self._stop.wait(0.2)
        # flush remainders per track
        for t in self._tracks.values():
            with self._lock:
                rem = self._avail(t)
                tail = self._pull(t, rem) if rem > config.SAMPLE_RATE // 2 else None
            if tail is not None:
                self._run(t, tail)

    def _run(self, t: _Track, audio_int_scale: np.ndarray) -> None:
        offset = t.consumed / config.SAMPLE_RATE
        t.consumed += audio_int_scale.size
        audio = _to_16k(np.clip(audio_int_scale / 32768.0, -1.0, 1.0).astype(np.float32))
        try:
            for s in self._rec.transcribe(audio, self.language, t.prev_text):
                text = s["text"].strip()
                if not text:
                    continue
                t.prev_text = (t.prev_text + " " + text)[-256:]
                item = {"start": round(offset + s["start"], 2),
                        "end": round(offset + s["end"], 2),
                        "text": text, "speaker": t.label}
                self.segments.append(item)
                if self.on_segment:
                    self.on_segment(item)
        except Exception as exc:
            log.error("STT transcribe failed: %s", exc)


# --------------------------------------------------------------------------- #
# batch (history re-transcription / finalize re-pass)                          #
# --------------------------------------------------------------------------- #
def _transcribe_one(path, rec, language, label) -> list[dict]:
    import soundfile as sf
    data, _sr = sf.read(str(path), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    audio = _to_16k(np.clip(data, -1.0, 1.0).astype(np.float32))
    out = []
    for s in rec.transcribe(audio, language, None):
        t = s["text"].strip()
        if t:
            out.append({"start": round(s["start"], 2), "end": round(s["end"], 2),
                        "text": t, "speaker": label})
    return out


def transcribe_files(paths: list, model_size: str | None = None, device: str | None = None,
                     language: str | None = None, engine: str = "whisper") -> list[dict]:
    rec = engines.get_recognizer(engine, model_size, device or "auto", language)
    rec.load()
    out = []
    for p in paths:
        out += _transcribe_one(p, rec, language, "")
    out.sort(key=lambda s: s["start"])
    return out


def transcribe_labeled(track_paths: dict, model_size: str | None = None,
                       device: str | None = None, language: str | None = None,
                       engine: str = "whisper") -> list[dict]:
    """Transcribe each track separately with a speaker label, merged by time."""
    rec = engines.get_recognizer(engine, model_size, device or "auto", language)
    rec.load()
    out = []
    for label, path in track_paths.items():
        out += _transcribe_one(path, rec, language, label)
    out.sort(key=lambda s: s["start"])
    return out


def segments_to_markdown(segments: list[dict], title: str) -> str:
    def ts(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    lines = [f"# {title}", "", f"_Transcribed by am-I-audible · {len(segments)} segments_", ""]
    for s in segments:
        spk = s.get("speaker")
        prefix = f"**{spk}** " if spk else ""
        lines.append(f"`[{ts(s['start'])}]` {prefix}{s['text']}")
    lines.append("")
    return "\n".join(lines)

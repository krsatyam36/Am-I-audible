"""Near-real-time transcription via faster-whisper.

Capture and transcription are decoupled: the recorder taps each track's PCM into
this engine, which mixes the active tracks into one mono timeline, and a worker
thread transcribes fixed windows (default 5 s) with VAD filtering, emitting
timestamped segments. Whisper needs a few seconds of context, so this is
*near*-live (a window behind), not instantaneous.

faster-whisper is an optional dependency: if it isn't installed, :func:`available`
returns False and the engine becomes a no-op, so the recorder still works.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np

from am_i_audible import config

log = logging.getLogger(__name__)
_FULL_SCALE = 32768.0
_WHISPER_SR = 16_000  # faster-whisper assumes 16 kHz for ndarray input


def _to_16k(mono_48k: np.ndarray) -> np.ndarray:
    """Downsample 48 kHz mono float32 -> 16 kHz (decimate by 3 with averaging)."""
    n = mono_48k.size // 3
    if n == 0:
        return mono_48k.astype(np.float32)
    return mono_48k[: n * 3].reshape(n, 3).mean(axis=1).astype(np.float32)


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


class TranscriptionEngine:
    def __init__(self, on_segment=None, model_size: str | None = None,
                 device: str | None = None, language: str | None = None,
                 window_seconds: float | None = None):
        self.on_segment = on_segment
        self.model_size = model_size or config.STT_MODEL
        self.device = device or config.STT_DEVICE
        self.language = language if language is not None else config.STT_LANGUAGE
        self.window = int((window_seconds or config.STT_WINDOW_SECONDS) * config.SAMPLE_RATE)
        self.segments: list[dict] = []
        self.error: str | None = None
        self.active_device: str | None = None
        self._model = None
        self._bufs: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consumed_frames = 0

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> bool:
        if not available():
            self.error = "faster-whisper not installed (pip install faster-whisper)"
            log.warning(self.error)
            return False
        try:
            self._model = self._load_model()
        except Exception as exc:
            self.error = f"could not load STT model: {exc}"
            log.error(self.error)
            return False
        self._thread = threading.Thread(target=self._worker, name="stt", daemon=True)
        self._thread.start()
        log.info("STT engine started (model=%s device=%s)", self.model_size, self.active_device)
        return True

    def _load_model(self):
        from faster_whisper import WhisperModel
        want = self.device
        attempts = []
        if want in ("auto", "cuda"):
            attempts.append(("cuda", "int8_float16"))
        if want != "cuda":
            attempts.append(("cpu", "int8"))
        last = None
        probe = np.zeros(_WHISPER_SR, dtype=np.float32)  # 1 s of silence
        for dev, ct in attempts:
            try:
                m = WhisperModel(self.model_size, device=dev, compute_type=ct)
                # A CUDA model can *construct* without the cuBLAS/cuDNN runtime and
                # only fail at inference — so validate with a dummy encode here.
                m.transcribe(probe, beam_size=1)
                self.active_device = dev
                return m
            except Exception as exc:
                last = exc
                log.info("STT: %s/%s unusable (%s) — trying next", dev, ct, exc)
        raise last  # type: ignore[misc]

    def feed(self, name: str, samples: np.ndarray) -> None:
        """Recorder tap: buffer int16 mono samples for one track."""
        if self._model is None:
            return
        with self._lock:
            self._bufs.setdefault(name, deque()).append(samples.copy())

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return self.segments

    # -- worker ------------------------------------------------------------ #
    def _available_frames(self) -> int:
        if not self._bufs:
            return 0
        return min(sum(a.size for a in buf) for buf in self._bufs.values())

    def _pull(self, frames: int) -> np.ndarray:
        """Pull and mix `frames` samples from every active track (summed)."""
        mix = np.zeros(frames, dtype=np.float32)
        for buf in self._bufs.values():
            got = 0
            chunks = []
            while got < frames and buf:
                a = buf.popleft()
                if got + a.size <= frames:
                    chunks.append(a); got += a.size
                else:
                    need = frames - got
                    chunks.append(a[:need])
                    buf.appendleft(a[need:]); got = frames
            if chunks:
                seg = np.concatenate(chunks).astype(np.float32)
                mix[: seg.size] += seg
        return mix

    def _worker(self) -> None:
        while not self._stop.is_set() or self._available_frames() >= self.window:
            with self._lock:
                ready = self._available_frames() >= self.window
                audio = self._pull(self.window) if ready else None
            if audio is None:
                if self._stop.is_set():
                    break
                self._stop.wait(0.2)
                continue
            self._transcribe(audio)
        # flush any remainder on stop
        with self._lock:
            rem = self._available_frames()
            tail = self._pull(rem) if rem > config.SAMPLE_RATE // 2 else None
        if tail is not None:
            self._transcribe(tail)

    def _transcribe(self, audio_int_scale: np.ndarray) -> None:
        offset = self._consumed_frames / config.SAMPLE_RATE
        self._consumed_frames += audio_int_scale.size
        audio = np.clip(audio_int_scale / _FULL_SCALE, -1.0, 1.0).astype(np.float32)
        audio = _to_16k(audio)
        try:
            segs, _ = self._model.transcribe(
                audio, language=self.language, vad_filter=True, beam_size=1)
            for s in segs:
                text = s.text.strip()
                if not text:
                    continue
                item = {"start": round(offset + s.start, 2),
                        "end": round(offset + s.end, 2), "text": text}
                self.segments.append(item)
                if self.on_segment:
                    self.on_segment(item)
        except Exception as exc:
            log.error("STT transcribe failed: %s", exc)


def transcribe_files(paths: list, model_size: str | None = None,
                     device: str | None = None, language: str | None = None) -> list[dict]:
    """Batch-transcribe one or more WAV files (mixed) -> list of segments.

    Used for re-transcribing past recordings from the session history.
    """
    if not available():
        raise RuntimeError("faster-whisper not installed")
    import soundfile as sf
    from faster_whisper import WhisperModel

    mix = None
    for p in paths:
        data, _sr = sf.read(str(p), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        mix = data if mix is None else (
            mix[: len(data)] + data[: len(mix)] if len(mix) != len(data)
            else mix + data)
    if mix is None:
        return []
    audio = _to_16k(np.clip(mix, -1.0, 1.0).astype(np.float32))

    eng = TranscriptionEngine(model_size=model_size, device=device, language=language)
    model = eng._load_model()
    segs, _ = model.transcribe(audio, language=eng.language, vad_filter=True, beam_size=1)
    out = []
    for s in segs:
        t = s.text.strip()
        if t:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": t})
    return out


def segments_to_markdown(segments: list[dict], title: str) -> str:
    """Render segments as a clean, LLM-ready Markdown transcript."""
    def ts(sec: float) -> str:
        sec = int(sec)
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    lines = [f"# {title}", "", f"_Transcribed by am-I-audible · {len(segments)} segments_", ""]
    for s in segments:
        lines.append(f"**[{ts(s['start'])}]** {s['text']}")
    lines.append("")
    return "\n".join(lines)

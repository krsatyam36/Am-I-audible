"""Pluggable STT engines behind one interface.

Every backend implements :class:`Recognizer`:

    available() -> bool          # are its libraries importable?
    load()                       # load the model (sets active_device)
    transcribe(audio16k, ...)    # 16 kHz mono float32 -> [{start,end,text}]

The streaming/batch machinery in ``transcribe.py`` is engine-agnostic: it
resamples to 16 kHz, calls ``transcribe`` per window/file, and adds offsets +
speaker labels. Engines that lack sub-segment timestamps return a single
segment spanning the audio; the caller offsets it.

Adapters degrade gracefully — an engine whose library/model isn't installed
reports ``available() == False`` and the UI shows an install hint.
"""

from __future__ import annotations

import logging
import tempfile

import numpy as np

log = logging.getLogger(__name__)

WHISPER_SR = 16_000
_FULL_SCALE = 32768.0
_cuda_preloaded = False


def to_16k(mono_48k: np.ndarray) -> np.ndarray:
    """Resample 48 kHz mono float32 -> 16 kHz (soxr if available, else decimate)."""
    x = mono_48k.astype(np.float32)
    try:
        import soxr
        return soxr.resample(x, 48_000, WHISPER_SR).astype(np.float32)
    except Exception:
        n = x.size // 3
        return x if n == 0 else x[: n * 3].reshape(n, 3).mean(axis=1).astype(np.float32)


def preload_cuda_libs() -> None:
    """Preload pip-wheel nvidia cuBLAS/cuDNN so ctranslate2 finds them."""
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    import ctypes
    import glob
    import site
    for base in list(site.getsitepackages()) + [site.getusersitepackages()]:
        for pat in ("nvidia/cublas/lib/*.so*", "nvidia/cudnn/lib/*.so*"):
            for lib in sorted(glob.glob(f"{base}/{pat}")):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def _has_cuda() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        # ctranslate2-only path: assume cuda may exist; load() will fall back.
        return True


class Recognizer:
    key = "base"
    title = "Base"
    install_hint = ""

    def __init__(self, model: str, device: str = "auto", language: str | None = None):
        self.model_name = model
        self.device = device
        self.language = language
        self.active_device: str | None = None

    @classmethod
    def available(cls) -> bool:
        return False

    def load(self) -> None:
        raise NotImplementedError

    def transcribe(self, audio16k: np.ndarray, language: str | None,
                   initial_prompt: str | None) -> list[dict]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Whisper via faster-whisper (CTranslate2)                                     #
# --------------------------------------------------------------------------- #
class WhisperRecognizer(Recognizer):
    key = "whisper"
    title = "Whisper (faster-whisper)"
    install_hint = "pip install faster-whisper"
    MODELS = ["tiny", "base", "small", "medium",
              "distil-large-v3", "large-v3-turbo", "large-v3"]

    @classmethod
    def available(cls) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        from faster_whisper import WhisperModel
        attempts = []
        if self.device in ("auto", "cuda"):
            preload_cuda_libs()
            attempts.append(("cuda", "int8_float16"))
        if self.device != "cuda":
            attempts.append(("cpu", "int8"))
        probe = np.zeros(WHISPER_SR, dtype=np.float32)
        last = None
        for dev, ct in attempts:
            try:
                m = WhisperModel(self.model_name, device=dev, compute_type=ct)
                m.transcribe(probe, beam_size=1)  # validate inference (cuBLAS etc.)
                self._model = m
                self.active_device = dev
                return
            except Exception as exc:
                last = exc
                log.info("whisper %s/%s unusable (%s)", dev, ct, exc)
        raise last  # type: ignore[misc]

    def transcribe(self, audio16k, language, initial_prompt):
        segs, _ = self._model.transcribe(
            audio16k, language=language, vad_filter=True, beam_size=5,
            condition_on_previous_text=True, initial_prompt=initial_prompt or None)
        return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                for s in segs if s.text.strip()]


# --------------------------------------------------------------------------- #
# whisper.cpp via pywhispercpp                                                 #
# --------------------------------------------------------------------------- #
class WhisperCppRecognizer(Recognizer):
    key = "whispercpp"
    title = "whisper.cpp (GGUF)"
    install_hint = "pip install pywhispercpp"
    MODELS = ["base", "small", "medium", "large-v3-turbo", "large-v3"]

    @classmethod
    def available(cls) -> bool:
        try:
            import pywhispercpp  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        from pywhispercpp.model import Model
        # quiet the noisy whisper.cpp C-library logging
        self._model = Model(self.model_name, redirect_whispercpp_logs_to=False,
                            print_progress=False, print_realtime=False)
        self.active_device = "cpu"

    def transcribe(self, audio16k, language, initial_prompt):
        kw = {"print_progress": False}
        if language:
            kw["language"] = language
        segs = self._model.transcribe(audio16k, **kw)
        out = []
        for s in segs:
            text = s.text.strip()
            if text:  # pywhispercpp t0/t1 are centiseconds
                out.append({"start": s.t0 / 100.0, "end": s.t1 / 100.0, "text": text})
        return out


# --------------------------------------------------------------------------- #
# NVIDIA Parakeet via NeMo                                                     #
# --------------------------------------------------------------------------- #
class ParakeetRecognizer(Recognizer):
    key = "parakeet"
    title = "Parakeet TDT (NVIDIA)"
    install_hint = 'pip install "nemo_toolkit[asr]"'
    MODELS = ["nvidia/parakeet-tdt-0.6b-v2", "nvidia/parakeet-tdt-1.1b"]

    @classmethod
    def available(cls) -> bool:
        try:
            import nemo.collections.asr  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        import nemo.collections.asr as nemo_asr
        # NeMo's from_pretrained otherwise places the model on the GPU and OOMs on
        # small (4 GB) cards during checkpoint load; and Parakeet TDT 0.6B doesn't
        # fit inference in 4 GB anyway. Load on CPU for reliability. (Override
        # with $AMIA_STT_DEVICE=cuda on a larger GPU.)
        want_cuda = self.device == "cuda"
        self._model = nemo_asr.models.ASRModel.from_pretrained(
            self.model_name, map_location=("cuda" if want_cuda else "cpu"))
        self._model.eval()
        self.active_device = "cuda" if want_cuda else "cpu"

    def transcribe(self, audio16k, language, initial_prompt):
        import soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
            sf.write(f.name, audio16k, WHISPER_SR)
            hyps = self._model.transcribe([f.name])
        text = hyps[0]
        text = getattr(text, "text", text)  # NeMo returns Hypothesis or str
        text = str(text).strip()
        if not text:
            return []
        return [{"start": 0.0, "end": len(audio16k) / WHISPER_SR, "text": text}]


# --------------------------------------------------------------------------- #
# Moonshine (ONNX)                                                            #
# --------------------------------------------------------------------------- #
class MoonshineRecognizer(Recognizer):
    key = "moonshine"
    title = "Moonshine (edge)"
    install_hint = "pip install useful-moonshine-onnx"
    MODELS = ["moonshine/base", "moonshine/tiny"]

    @classmethod
    def _mod(cls):
        try:
            import moonshine_onnx as m
            return m
        except Exception:
            import moonshine as m  # type: ignore
            return m

    @classmethod
    def available(cls) -> bool:
        try:
            cls._mod()
            return True
        except Exception:
            return False

    def load(self) -> None:
        self._m = self._mod()
        self.active_device = "cpu"

    def transcribe(self, audio16k, language, initial_prompt):
        text = self._m.transcribe(audio16k, self.model_name)
        if isinstance(text, (list, tuple)):
            text = " ".join(text)
        text = str(text).strip()
        if not text:
            return []
        return [{"start": 0.0, "end": len(audio16k) / WHISPER_SR, "text": text}]


REGISTRY: dict[str, type[Recognizer]] = {
    WhisperRecognizer.key: WhisperRecognizer,
    WhisperCppRecognizer.key: WhisperCppRecognizer,
    ParakeetRecognizer.key: ParakeetRecognizer,
    MoonshineRecognizer.key: MoonshineRecognizer,
}

DEFAULT_MODEL = {
    "whisper": "large-v3-turbo",
    "whispercpp": "large-v3-turbo",
    "parakeet": "nvidia/parakeet-tdt-0.6b-v2",
    "moonshine": "moonshine/base",
}

# Human-readable labels for the model picker (id -> description).
MODEL_LABELS = {
    "tiny": "Tiny — 39M · fastest, least accurate",
    "base": "Base — 74M · fast, basic",
    "small": "Small — 244M · balanced",
    "medium": "Medium — 769M · accurate",
    "distil-large-v3": "Distil-Large-v3 — fast, English, ~large-v3 quality",
    "large-v3-turbo": "Large-v3 Turbo — 809M · best speed + accuracy (GPU)",
    "large-v3": "Large-v3 — 1.5B · most accurate, slowest",
    "nvidia/parakeet-tdt-0.6b-v2": "Parakeet TDT 0.6B — NVIDIA, fast, top English",
    "nvidia/parakeet-tdt-1.1b": "Parakeet TDT 1.1B — NVIDIA, larger, most accurate English",
    "moonshine/base": "Moonshine Base — tiny & fast (English)",
    "moonshine/tiny": "Moonshine Tiny — smallest, fastest (English)",
}


def model_label(model_id: str) -> str:
    return MODEL_LABELS.get(model_id, model_id)


def get_recognizer(engine: str, model: str | None, device: str = "auto",
                   language: str | None = None) -> Recognizer:
    cls = REGISTRY.get(engine, WhisperRecognizer)
    return cls(model or DEFAULT_MODEL.get(engine, "large-v3-turbo"), device, language)


def list_engines() -> list[dict]:
    """Describe every engine for the UI (availability + model choices)."""
    return [{
        "key": c.key, "title": c.title, "available": c.available(),
        "installHint": c.install_hint,
        "models": [{"id": m, "label": model_label(m)} for m in getattr(c, "MODELS", [])],
        "defaultModel": DEFAULT_MODEL.get(c.key),
    } for c in REGISTRY.values()]

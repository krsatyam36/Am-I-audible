"""Optional multi-speaker diarization via pyannote.audio.

Dual-track capture already separates *you* (mic) from *everyone else* (system).
This optional layer goes further: it splits the *system* track into individual
speakers ("Others · Speaker 1/2…") for multi-party meetings.

It is heavyweight (pyannote + torch) and gated behind a Hugging Face token, so
it's fully optional: :func:`available` returns False unless everything is present,
and callers fall back to plain track labels.

Enable with:
    pip install "pyannote.audio>=3.1"
    export HUGGINGFACE_TOKEN=hf_xxx   # accept the model's terms on huggingface.co
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_MODEL = "pyannote/speaker-diarization-3.1"


def token() -> str | None:
    return os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")


def available() -> bool:
    if not token():
        return False
    try:
        import pyannote.audio  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def diarize_wav(path) -> list[dict]:
    """Return speaker turns: [{start, end, speaker}] for one WAV file."""
    from pyannote.audio import Pipeline
    pipe = Pipeline.from_pretrained(_MODEL, use_auth_token=token())
    diar = pipe(str(path))
    turns = []
    for turn, _, speaker in diar.itertracks(yield_label=True):
        turns.append({"start": float(turn.start), "end": float(turn.end),
                      "speaker": str(speaker)})
    return turns


def relabel(segments: list[dict], turns: list[dict], base_label: str) -> None:
    """Relabel `segments` in place by their max-overlap diarization turn."""
    for seg in segments:
        best, best_ov = None, 0.0
        for t in turns:
            ov = max(0.0, min(seg["end"], t["end"]) - max(seg["start"], t["start"]))
            if ov > best_ov:
                best_ov, best = ov, t["speaker"]
        if best is not None:
            n = best.split("_")[-1]
            seg["speaker"] = f"{base_label} · Speaker {n}"

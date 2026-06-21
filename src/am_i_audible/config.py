"""Shared constants and defaults for am-I-audible v0.1.0.

Kept dependency-free so every layer (routing, capture, UI, future GUI) can
import it without pulling in heavyweight modules.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Capture format -------------------------------------------------------
# 48 kHz is PipeWire's native graph rate, so no resampling happens on the hot
# path. Lossless 16-bit mono per track; we resample to 16 kHz only at STT time
# (v0.2.0), which keeps the archival audio pristine.
SAMPLE_RATE = 48_000
CHANNELS = 1
SAMPLE_FORMAT = "int16"  # 16-bit PCM WAV

# --- Virtual sink identity ------------------------------------------------
# Two independent null sinks so mic and system stay as separate tracks
# (dual-track capture) and each can be re-routed without breaking the other's
# monitor -- the basis of gapless mic hot-swap.
SINK_MIC = "am_i_audible_mic"
SINK_SYSTEM = "am_i_audible_sys"
SINK_MIC_DESCRIPTION = "am-I-audible (microphone)"
SINK_SYSTEM_DESCRIPTION = "am-I-audible (system audio)"

# Prefix used to recognise / sweep our own objects in the audio graph.
OBJECT_PREFIX = "am_i_audible"

# Loopback latency target (ms). Low enough to stay responsive, high enough to
# avoid xruns on a busy desktop; tuned per backend if needed.
LOOPBACK_LATENCY_MS = 100

# --- Capture plumbing -----------------------------------------------------
# ~100 ms chunk at 48 kHz mono s16. Small enough for responsive VU meters,
# large enough to keep syscalls/overhead negligible.
CHUNK_FRAMES = 4_800
BYTES_PER_FRAME = 2  # s16 mono

# --- Output layout --------------------------------------------------------
# Personal default: keep everything inside the project so files are easy to
# reach. Override with $AMIA_RECORDINGS_DIR. Both trees are git-ignored.
RECORDINGS_ROOT = Path(
    os.environ.get("AMIA_RECORDINGS_DIR", "~/Projects/Am-I-audible/recorded-audio")
).expanduser()
TRANSCRIPTS_ROOT = RECORDINGS_ROOT / "generated_transcripts"  # used from v0.2.0

MIC_TRACK_FILENAME = "mic.wav"
SYSTEM_TRACK_FILENAME = "system.wav"

# --- UI -------------------------------------------------------------------
METER_REFRESH_HZ = 12

# --- Transcription (v0.2.0 real-time STT) ---------------------------------
# Default to large-v3: top accuracy, GPU-friendly, and already cached here.
# (large-v3-turbo is faster but its download is large; pick it in Settings once
# it's fully downloaded.) Override with $AMIA_STT_MODEL.
STT_MODEL = os.environ.get("AMIA_STT_MODEL", "large-v3")
STT_DEVICE = os.environ.get("AMIA_STT_DEVICE", "auto")  # auto | cuda | cpu
STT_LANGUAGE = os.environ.get("AMIA_STT_LANGUAGE") or None  # None = autodetect
# Seconds of audio per transcription pass. Smaller = lower latency, worse
# accuracy (Whisper needs context). 5s is a sane near-live default.
STT_WINDOW_SECONDS = float(os.environ.get("AMIA_STT_WINDOW", "5"))

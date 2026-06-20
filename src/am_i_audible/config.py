"""Shared constants and defaults for am-I-audible v0.1.0.

Kept dependency-free so every layer (routing, capture, UI, future GUI) can
import it without pulling in heavyweight modules.
"""

from __future__ import annotations

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

# --- Output layout --------------------------------------------------------
RECORDINGS_ROOT = Path.home() / "Recordings" / "am-i-audible"
MIC_TRACK_FILENAME = "mic.wav"
SYSTEM_TRACK_FILENAME = "system.wav"

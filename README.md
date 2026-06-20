<div align="center">

# Am I Audible?

**Linux-native dual-track audio capture — system + mic, zero dropouts, gapless hot-swap**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![PipeWire](https://img.shields.io/badge/PipeWire-1.0+-FF8800?style=flat&logo=pipewire&logoColor=white)](https://pipewire.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://makeapullrequest.com)

</div>

---

## Overview

Ever been asked *"Am I audible?"* on a call? This tool answers that — by capturing your **microphone** and **system audio** as two independent pristine WAV tracks, keeping them separate so you can transcribe, mix, or analyze them later.

Designed for Linux PipeWire (with a PulseAudio fallback), it creates virtual null sinks and routes audio through them with gapless hot-swap support — switching your mic mid-session without losing a single sample.

## Current Status

**v0.1.0 — Audio routing layer complete.** The foundation is built and tested:
- Virtual sink creation (system + mic on separate tracks)
- Audio routing via PipeWire-native (`pw-loopback` + `pw-link`) or PulseAudio (`pactl`)
- Gapless microphone hot-swap (new route created before old one is destroyed)
- Bulletproof cleanup (context manager + `atexit` + SIGINT/SIGTERM handlers)
- Backend auto-detection (prefers `pactl`, falls back to PipeWire)

**Coming next:** `recorder.py` (incremental WAV capture), `meters.py` (VU meters), `session.py` (session management), and `cli.py` (terminal UI).

## System Prerequisites

- **Linux** with **PipeWire** (most modern distros)
- `pw-loopback` and `pw-link` (part of `pipewire-pulse` / `wireplumber`)
- Python 3.10+
- Optional but recommended: `pulseaudio-utils` (`sudo apt install pulseaudio-utils`) for the simpler `pactl` backend

## Installation

```bash
# Clone the repo
git clone https://github.com/<your-org>/am-i-audible.git
cd am-i-audible

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (audio capture; STT deferred to v0.2.0)
pip install -r requirements.txt
```

## Quick Test — Audio Routing

The routing layer can be tested standalone — it only uses stdlib, zero pip dependencies needed:

```bash
PYTHONPATH=src python3 -m am_i_audible.audio.router
```

This creates both virtual sinks (`am_i_audible_mic` and `am_i_audible_sys`), routes your default mic and system audio into them, and prints the monitor source names. In another terminal:

```bash
wpctl status                              # see the sinks
pw-record --target am_i_audible_sys.monitor /tmp/sys.wav   # capture system audio
pw-record --target am_i_audible_mic.monitor /tmp/mic.wav   # capture microphone
```

Press **Enter** in the first terminal to tear down — all sinks are destroyed cleanly.

## Output Layout

Recordings are stored in `~/Recordings/am-i-audible/`:

```
~/Recordings/am-i-audible/
├── mic.wav           # microphone track (48 kHz, 16-bit mono)
└── system.wav        # system audio track (48 kHz, 16-bit mono)
```

## Architecture

```
Physical mic ──loopback──► [null sink am_i_audible_mic] ──► .monitor ──► mic.wav
Default out  ──loopback──► [null sink am_i_audible_sys]  ──► .monitor ──► system.wav
```

- **Backend-agnostic**: All `pactl`/`pw-*` commands live in `backends.py` behind a single `Handle` abstraction and one mockable `_run()` seam.
- **Gapless hot-swap**: `swap_mic()` creates the new mic loopback before destroying the old one — the mic sink's monitor never goes silent.
- **Bulletproof cleanup**: Context manager + `atexit` + SIGINT/SIGTERM handlers ensure no stray sinks survive a crash or Ctrl-C.

## Project Structure

```
am-i-audible/
├── pyproject.toml              # Package metadata + console entry point
├── requirements.txt            # Capture-only deps
├── src/
│   └── am_i_audible/
│       ├── __init__.py         # Version
│       ├── __main__.py         # Entry point (points to router test harness for now)
│       ├── config.py           # Sample rate, sink names, output paths
│       └── audio/
│           ├── __init__.py
│           ├── backends.py     # PactlBackend, PipeWireBackend, detect_backend()
│           └── router.py       # AudioRouter: setup / swap_mic / teardown
├── tests/                      # (scaffolded)
├── docs/                       # (scaffolded)
└── .gitignore
```

## License

MIT

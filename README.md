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

**v0.1.0 — Dual-track capture, complete and working end-to-end.**
- Virtual sink creation (system + mic on **separate** tracks)
- Audio routing via PipeWire-native (`pw-loopback` + `pw-link`) or PulseAudio (`pactl`), auto-detected
- Gapless microphone hot-swap (new route created before old one is destroyed)
- Dual-track recording to 48 kHz/16-bit mono WAV, one `pw-record` per track streamed to disk (flushed per chunk; kernel-buffered, no dropouts)
- Live terminal **VU meters + elapsed timer** (the real-time "am I audible?" check)
- Bulletproof cleanup (context manager + `atexit` + SIGINT/SIGTERM handlers)

**Coming next (v0.2.0):** local STT with `faster-whisper` → timestamped Markdown. **v0.3.0:** `pyannote` speaker diarization.

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

## Usage

```bash
PYTHONPATH=src python3 -m am_i_audible record            # record until [q] / Ctrl-C
PYTHONPATH=src python3 -m am_i_audible record --label standup
PYTHONPATH=src python3 -m am_i_audible record --duration 1800   # stop after 30 min
PYTHONPATH=src python3 -m am_i_audible record --mic-only         # or --system-only
PYTHONPATH=src python3 -m am_i_audible devices           # show backend + audio sources
```

While recording, the terminal shows a live VU meter per track plus an elapsed timer:

```
● REC 00:12:43   mic: alsa_input.pci-0000_05_00.6.analog-stereo

    mic  ████████████░░░░░░░░░░░░░░░░░░ -18.4 dB
 system  ██████████████████░░░░░░░░░░░░  -6.2 dB

[s] swap mic   [q]/Ctrl-C stop
```

Press **`s`** then Enter to hot-swap the microphone mid-recording (e.g. internal → headset) **without losing a single sample** — the recorder reads from a stable virtual-sink monitor, so only the upstream route changes. Press **`q`** or Ctrl-C to stop; both WAV headers are finalised and a summary is printed.

> Want to test routing in isolation (stdlib only, no recording)? Run
> `PYTHONPATH=src python3 -m am_i_audible.audio.router` and verify with
> `wpctl status` / `pw-record` in another terminal.

## Output Layout

Each session is saved to its own timestamped folder under `recorded-audio/`
(override with `$AMIA_RECORDINGS_DIR`):

```
recorded-audio/
├── 2026-06-21_0930_standup/
│   ├── mic.wav        # microphone track (48 kHz, 16-bit mono)
│   └── system.wav     # system audio track (48 kHz, 16-bit mono)
└── generated_transcripts/   # populated from v0.2.0
```

Both `recorded-audio/` and `generated_transcripts/` are git-ignored — recordings stay local.

## Architecture

```
Physical mic ──loopback──► [null sink am_i_audible_mic] ──► .monitor ──► pw-record ──► mic.wav
Default out  ──loopback──► [null sink am_i_audible_sys]  ──► .monitor ──► pw-record ──► system.wav
```

Capture and processing are deliberately **decoupled**: v0.1.0 records pristine lossless audio with almost no CPU, and STT/diarization (v0.2.0+) run later as a GPU batch pass over the saved files — so a recording never drops a frame, and you can re-transcribe anytime with a bigger model.

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
│       ├── __main__.py         # `python -m am_i_audible`
│       ├── cli.py              # argparse CLI: record / devices
│       ├── config.py           # Sample rate, sink names, output paths
│       ├── audio/
│       │   ├── backends.py     # PactlBackend, PipeWireBackend, detect_backend()
│       │   ├── router.py       # AudioRouter: setup / swap_mic / teardown
│       │   └── recorder.py     # DualTrackRecorder: pw-record → WAV + RMS levels
│       ├── ui/
│       │   └── meters.py       # Live VU meters + timer (rich)
│       └── core/
│           └── session.py      # Recording lifecycle: start/stop/swap
├── tests/test_router.py        # Router unit tests (stdlib unittest)
├── docs/
└── .gitignore
```

## License

MIT

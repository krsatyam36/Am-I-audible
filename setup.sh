#!/usr/bin/env bash
# am-I-audible one-command installer.
#   git clone … && cd Am-I-audible && ./setup.sh && listen
#
# Does ALL the heavy lifting up front: system packages, the global `listen`
# command, transcription deps, GPU libs (if an NVIDIA GPU is present), and it
# pre-downloads the Whisper models — so the first `listen` just works.
#
# Flags:  --no-stt      skip transcription (capture only)
#         --no-gpu      don't install CUDA libs even if a GPU is present
#         --gpu         force-install CUDA libs
#         --no-desktop  don't add the app-drawer launcher
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_STT=1; GPU=auto; DESKTOP=1
for a in "$@"; do case "$a" in
  --no-stt) WITH_STT=0 ;; --gpu) GPU=1 ;; --no-gpu) GPU=0 ;;
  --no-desktop) DESKTOP=0 ;;
  *) echo "unknown flag: $a"; exit 2 ;;
esac; done

say() { printf "\n\033[1;36m==>\033[0m %s\n" "$1"; }

# 1. System packages (PipeWire tools + libs the Python wheels need at runtime).
if command -v apt-get >/dev/null 2>&1; then
  say "Installing system packages (sudo may prompt)…"
  sudo apt-get update -y
  sudo apt-get install -y \
    pipewire pipewire-pulse pipewire-bin wireplumber \
    libportaudio2 libsndfile1 libnotify-bin ffmpeg pipx pulseaudio-utils
else
  say "Non-apt system detected — install these with your package manager, then re-run:"
  echo "   pipewire pipewire-pulse wireplumber libportaudio2 libsndfile1 libnotify-bin ffmpeg pipx"
fi

# 2. Make pipx apps reachable.
export PATH="$HOME/.local/bin:$PATH"
pipx ensurepath >/dev/null 2>&1 || true

# 3. Install the global `listen` command from this checkout.
say "Installing the 'listen' app…"
pipx install -e "$HERE" --force

# 4. Transcription engine.
if [ "$WITH_STT" = "1" ]; then
  say "Adding transcription (faster-whisper + soxr)…"
  pipx inject am-i-audible faster-whisper soxr
fi

# 5. GPU acceleration (NVIDIA) — auto-detected.
if [ "$GPU" = "auto" ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then GPU=1; else GPU=0; fi
fi
if [ "$GPU" = "1" ] && [ "$WITH_STT" = "1" ]; then
  say "NVIDIA GPU detected — adding CUDA libs (large download)…"
  pipx inject am-i-audible nvidia-cublas-cu12 nvidia-cudnn-cu12 || \
    echo "   (CUDA libs failed; transcription will run on CPU)"
fi

# 6. Pre-download the models so the first recording is instant.
if [ "$WITH_STT" = "1" ]; then
  say "Pre-downloading speech models (one-time, a few minutes)…"
  listen --prefetch || echo "   (model prefetch skipped/failed; will download on first use)"
fi

# 7. App-drawer launcher.
if [ "$DESKTOP" = "1" ]; then
  listen --install-desktop || true
fi

say "Done ✓"
echo "   Run:  listen        (or find 'am-I-audible' in your app drawer)"
echo "   Check setup:  listen --doctor"

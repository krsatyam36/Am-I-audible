#!/usr/bin/env bash
# Build a Debian/Ubuntu .deb for am-I-audible into dist/.
#
#   bash packaging/deb/build.sh
#
# The package declares all system dependencies (apt resolves them), installs the
# app into /opt/am-i-audible (its own venv), exposes `listen` on PATH, ships an
# app-drawer entry, and on install: builds the venv, optionally adds CUDA libs
# (if an NVIDIA GPU is present), and pre-downloads the speech models into
# /opt/am-i-audible/models so the very first launch works offline.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' "$ROOT/src/am_i_audible/__init__.py")"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
PKG="am-i-audible_${VERSION}_${ARCH}"
STAGE="$ROOT/dist/$PKG"

echo "==> Building $PKG.deb"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/am-i-audible/app" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps"

# --- app source (installed into a venv by postinst) ---
cp -r "$ROOT/src" "$ROOT/pyproject.toml" "$ROOT/README.md" "$STAGE/opt/am-i-audible/app/"

# --- /usr/bin/listen wrapper (points HF cache at the shared model dir) ---
cat > "$STAGE/usr/bin/listen" <<'EOF'
#!/bin/sh
export HF_HOME=/opt/am-i-audible/models
exec /opt/am-i-audible/venv/bin/listen "$@"
EOF
chmod 755 "$STAGE/usr/bin/listen"

# --- desktop entry + icon ---
cp "$ROOT/src/am_i_audible/web/static/icon.svg" \
   "$STAGE/usr/share/icons/hicolor/scalable/apps/am-i-audible.svg"
cat > "$STAGE/usr/share/applications/am-i-audible.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=am-I-audible
GenericName=Meeting Recorder
Comment=Record & transcribe meetings (mic + system audio)
Exec=listen
Icon=am-i-audible
Terminal=false
Categories=AudioVideo;Audio;Recorder;
Keywords=record;transcribe;meeting;audio;
EOF

# --- control ---
cat > "$STAGE/DEBIAN/control" <<EOF
Package: am-i-audible
Version: $VERSION
Section: sound
Priority: optional
Architecture: $ARCH
Depends: pipewire, pipewire-pulse, wireplumber, libportaudio2, libsndfile1,
 libnotify-bin, ffmpeg, python3 (>= 3.10), python3-venv, python3-pip
Recommends: pulseaudio-utils
Maintainer: krsatyam36 <noreply@users.noreply.github.com>
Description: Linux dual-track meeting recorder + offline transcription
 Records microphone and system audio as separate tracks with gapless mic
 hot-swap, and transcribes locally (faster-whisper) with speaker labels.
EOF

# --- postinst: build venv, GPU libs, prefetch models ---
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
APP=/opt/am-i-audible
echo "Setting up am-I-audible (one-time)…"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip >/dev/null
"$APP/venv/bin/pip" install "$APP/app[stt]"
# GPU libraries if an NVIDIA GPU is present
if command -v nvidia-smi >/dev/null 2>&1; then
  "$APP/venv/bin/pip" install nvidia-cublas-cu12 nvidia-cudnn-cu12 || true
fi
# Pre-download models into the shared cache so first launch is offline-instant
mkdir -p "$APP/models"
HF_HOME="$APP/models" "$APP/venv/bin/listen" --prefetch || true
chmod -R a+rX "$APP/models" || true
update-desktop-database -q 2>/dev/null || true
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
echo "am-I-audible installed. Launch it from your app drawer or run: listen"
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

# --- prerm: remove the generated venv/models ---
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
rm -rf /opt/am-i-audible/venv /opt/am-i-audible/models || true
EOF
chmod 755 "$STAGE/DEBIAN/prerm"

mkdir -p "$ROOT/dist"
dpkg-deb --build --root-owner-group "$STAGE" "$ROOT/dist/$PKG.deb"
echo "==> Built dist/$PKG.deb"
echo "    Install with:  sudo apt install ./dist/$PKG.deb"

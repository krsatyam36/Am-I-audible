#!/usr/bin/env bash
# Build a portable AppImage for am-I-audible into dist/.
#
#   bash packaging/appimage/build.sh
#
# Bundles a relocatable Python + the app. PipeWire is used from the host (always
# present on modern Linux). Models are NOT bundled (they'd bloat the image to
# ~2 GB) — on first launch AppRun downloads them once into ~/.cache/am-i-audible
# with a progress message, then every run is offline.
#
# Requires: appimagetool on PATH, internet (to fetch a standalone Python + deps).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' "$ROOT/src/am_i_audible/__init__.py")"
APPDIR="$ROOT/dist/AppDir"
PYVER="3.12"
# indygreg python-build-standalone (relocatable CPython)
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250115/cpython-3.12.8+20250115-x86_64-unknown-linux-gnu-install_only.tar.gz"

command -v appimagetool >/dev/null || { echo "appimagetool not found on PATH"; exit 1; }

echo "==> Building AppImage $VERSION"
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr"

echo "==> Fetching relocatable Python…"
curl -fsSL "$PY_URL" -o /tmp/amia-py.tar.gz
tar -xzf /tmp/amia-py.tar.gz -C "$APPDIR/usr" --strip-components=1

echo "==> Installing app + transcription into the bundle…"
"$APPDIR/usr/bin/python3" -m pip install --upgrade pip >/dev/null
"$APPDIR/usr/bin/python3" -m pip install "$ROOT[stt]"

# --- AppRun: per-user model cache + one-time first-run prefetch ---
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export HF_HOME="${HF_HOME:-$HOME/.cache/am-i-audible/models}"
mkdir -p "$HF_HOME"
if [ ! -f "$HF_HOME/.prefetched" ]; then
  echo "First run: downloading speech models once (a few minutes)…"
  "$HERE/usr/bin/listen" --prefetch && touch "$HF_HOME/.prefetched"
fi
exec "$HERE/usr/bin/listen" --window "$@"
EOF
chmod 755 "$APPDIR/AppRun"

cp "$ROOT/src/am_i_audible/web/static/icon.svg" "$APPDIR/am-i-audible.svg"
cat > "$APPDIR/am-i-audible.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=am-I-audible
Exec=AppRun
Icon=am-i-audible
Categories=AudioVideo;Audio;Recorder;
Terminal=false
EOF

mkdir -p "$ROOT/dist"
ARCH=x86_64 appimagetool "$APPDIR" "$ROOT/dist/am-I-audible-${VERSION}-x86_64.AppImage"
echo "==> Built dist/am-I-audible-${VERSION}-x86_64.AppImage"

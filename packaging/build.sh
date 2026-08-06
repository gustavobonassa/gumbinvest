#!/usr/bin/env bash
# Builds the macOS desktop app: the Python server (PyInstaller) wrapped by the
# Electron shell (electron-builder). Mirror of build.ps1 for Windows.
#
#   bash packaging/build.sh
#
# Output: desktop-shell/out/GumbInvest-<version>-arm64.dmg
set -euo pipefail

PACKAGING="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$PACKAGING")"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
SHELL_DIR="$ROOT/desktop-shell"
BUILD_VENV="$PACKAGING/.venv-build"

echo "== 1/5 Frontend build =="
cd "$FRONTEND"
[ -d node_modules ] || npm ci
npm run build

echo "== 2/5 Build venv + icons =="
[ -d "$BUILD_VENV" ] || python3 -m venv "$BUILD_VENV"
PY="$BUILD_VENV/bin/python"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$BACKEND/requirements-build.txt"
"$PY" "$PACKAGING/make_icon.py"

echo "== 3/5 PyInstaller (server) =="
"$PY" -m PyInstaller \
  --noconfirm \
  --distpath "$PACKAGING/dist" \
  --workpath "$PACKAGING/build" \
  "$PACKAGING/GumbInvest.spec"

echo "== 4/5 Electron shell dependencies =="
cd "$SHELL_DIR"
[ -d node_modules ] || npm install

echo "== 5/5 electron-builder =="
# No signing identity in CI: build unsigned rather than fail looking for one.
CSC_IDENTITY_AUTO_DISCOVERY=false npx electron-builder --mac

echo "Installer at $SHELL_DIR/out/"

#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.platform == "darwin", "Build on macOS"; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$PYTHON" tools/build_icon.py
"$PYTHON" -m PyInstaller --noconfirm --clean AgentMeter-macOS.spec
cp README.md THIRD_PARTY_NOTICES.md dist/
printf 'Built: %s/dist/Agent Meter.app\n' "$PWD"

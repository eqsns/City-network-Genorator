#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo '请在 Apple 芯片 Mac 的原生 arm64 终端运行，不支持 Windows 或 Rosetta。'
  exit 1
fi
PYTHON="${CNG_PYTHON:-python3.12}"
"$PYTHON" -c 'import sys,platform; assert sys.version_info[:2] == (3,12) and platform.machine() == "arm64", "需要原生 arm64 Python 3.12"'
if [ ! -x .venv-macos/bin/python ]; then
  "$PYTHON" -m venv .venv-macos
fi
.venv-macos/bin/python packaging/build_macos.py "$@"

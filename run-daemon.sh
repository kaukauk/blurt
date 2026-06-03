#!/usr/bin/env bash
# Launch the STT daemon with the bundled NVIDIA cuDNN/cuBLAS libs on the loader path.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
SITE="$VENV/lib/python3.12/site-packages"

# Make CTranslate2 find the pip-installed CUDA libraries.
NV_LIBS="$(find "$SITE/nvidia" -maxdepth 2 -name lib -type d 2>/dev/null | paste -sd: -)"
export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec "$VENV/bin/python" "$DIR/stt_daemon.py"

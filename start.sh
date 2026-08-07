#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
MODEL_WEIGHTS="$REPO_ROOT/kronos/weights/NeoQuasar__Kronos-base/model.safetensors"
TOKENIZER_WEIGHTS="$REPO_ROOT/kronos/weights/NeoQuasar__Kronos-Tokenizer-base/model.safetensors"
NO_TUI=0

for arg in "$@"; do
  if [[ "$arg" == "--no-tui" || "$arg" == "-NoTui" ]]; then
    NO_TUI=1
  fi
done

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Creating .venv..."
  python3 -m venv .venv
fi

echo "Installing/updating local package..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.0"
"$VENV_PYTHON" -m pip install -e .

if [[ ! -f "$MODEL_WEIGHTS" || ! -f "$TOKENIZER_WEIGHTS" ]]; then
  echo "Downloading Kronos weights..."
  "$VENV_PYTHON" scripts/download_kronos_assets.py
fi

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. You can paste API keys in the TUI Settings tab."
fi

if [[ "$NO_TUI" == "0" ]]; then
  echo "Opening bot TUI..."
  "$VENV_PYTHON" -m kronosobuchalka tui --config configs/bybit_demo_1x.example.json
else
  echo "Setup complete. TUI skipped because --no-tui was passed."
fi

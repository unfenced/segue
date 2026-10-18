#!/bin/bash
# Double-click to open Segue in your browser. Close this window to stop it.
# Everything uv sets up stays inside this folder (in .venv); nothing else on the computer changes.
cd "$(dirname "$0")" || exit 1
export UV_CACHE_DIR="$PWD/.venv/uv-cache"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "Segue needs uv, which wasn't found. See https://docs.astral.sh/uv/ to install it."
  read -r -p "Press Enter to close."
  exit 1
fi
"$UV" run --quiet segue ui

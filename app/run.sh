#!/bin/sh
# Launch ALS Collect from source (creates the venv on first run).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${ALS_COLLECT_VENV:-$HOME/.venvs/als-collect}"
if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for v in 3.13 3.12 3.14; do
    [ -x "/opt/homebrew/opt/python@$v/bin/python$v" ] && PY="/opt/homebrew/opt/python@$v/bin/python$v" && break
  done
  [ -n "$PY" ] || PY=python3
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$HERE/requirements.txt"
fi
exec "$VENV/bin/python" "$HERE/als_app.py" "$@"

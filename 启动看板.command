#!/bin/zsh
set -e
cd "$(dirname "$0")"
if curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:8090/api/sync/status" >/dev/null 2>&1; then
  open "http://127.0.0.1:8090"
  exit 0
fi
PYTHON_BIN="/opt/homebrew/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
(sleep 1.5; open "http://127.0.0.1:8090") &
exec "$PYTHON_BIN" app.py

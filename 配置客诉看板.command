#!/bin/zsh
set -e
cd "$(dirname "$0")"
PYTHON_BIN="/opt/homebrew/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
"$PYTHON_BIN" configure_complaint_audit.py
read -k 1 "?配置完成，按任意键关闭窗口。"

#!/usr/bin/env python3
"""从本机私有环境配置启动看板服务，供 macOS 登录启动项调用。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


CONFIG_PATH = Path.home() / ".config" / "daily-ticket-dashboard" / "complaint-audit.env"
ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    if not CONFIG_PATH.exists():
        return
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key.replace("_", "").isalnum():
            os.environ[key] = value


def main() -> None:
    load_env()
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, str(ROOT / "app.py"), "--host", "0.0.0.0", "--port", "8090"])


if __name__ == "__main__":
    main()

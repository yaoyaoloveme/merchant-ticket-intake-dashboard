#!/usr/bin/env python3
"""交互式配置客诉看板口令，并安装 macOS 登录启动服务。"""

from __future__ import annotations

import argparse
import getpass
import os
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "daily-ticket-dashboard"
CONFIG_PATH = CONFIG_DIR / "complaint-audit.env"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.codex.daily-ticket-dashboard.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "daily-ticket-dashboard.log"
LABEL = "com.codex.daily-ticket-dashboard"


def write_private_config(password: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            f"COMPLAINT_AUDIT_PASSWORD_HASH={generate_password_hash(password)}",
            f"COMPLAINT_AUDIT_SESSION_SECRET={secrets.token_urlsafe(48)}",
            "TICKET_DASHBOARD_HOST=0.0.0.0",
            "",
        ]
    )
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def install_launch_agent() -> None:
    payload = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "run_dashboard_service.py")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
            "TZ": "Asia/Shanghai",
        },
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PLIST_PATH.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    temporary.replace(PLIST_PATH)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="配置客诉稽查看板本机访问口令和登录启动服务")
    parser.add_argument("--generate-initial", action="store_true", help="生成一次性初始口令并安装服务")
    args = parser.parse_args()
    print("配置内容只保存在本机私有目录，不写入代码仓库。")
    if args.generate_initial:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        password = "".join(secrets.choice(alphabet) for _ in range(16))
    else:
        password = getpass.getpass("请设置客诉看板统一访问口令：")
        confirmation = getpass.getpass("请再次输入：")
        if password != confirmation:
            raise SystemExit("两次输入不一致，未保存。")
    if len(password) < 10:
        raise SystemExit("口令至少需要10个字符。")
    write_private_config(password)
    install_launch_agent()
    print(f"已配置并启动：http://127.0.0.1:8090/complaint-audit")
    print(f"私有配置：{CONFIG_PATH}")
    if args.generate_initial:
        print(f"初始访问口令：{password}")


if __name__ == "__main__":
    main()

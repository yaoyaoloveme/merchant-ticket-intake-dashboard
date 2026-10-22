#!/usr/bin/env python3
"""Keep the public merchant dashboard current across Mac sleep and network loss."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PUBLISHER = Path(os.getenv("MERCHANT_DASHBOARD_PUBLISHER", ROOT / "publish_snapshot.py"))
PYTHON = os.getenv("MERCHANT_DASHBOARD_PYTHON", sys.executable)
STATE_PATH = Path(os.getenv(
    "MERCHANT_DASHBOARD_DAEMON_STATE",
    "/Users/macbook/.local/state/merchant-ticket-dashboard/publisher-state.json",
))
INTERVAL_SECONDS = int(os.getenv("MERCHANT_DASHBOARD_PUBLISH_INTERVAL", "300"))
WAKE_GAP_SECONDS = int(os.getenv("MERCHANT_DASHBOARD_WAKE_GAP", "60"))
PROBE_INTERVAL_SECONDS = int(os.getenv("MERCHANT_DASHBOARD_WAKE_PROBE", "5"))
MAX_RUN_SECONDS = int(os.getenv("MERCHANT_DASHBOARD_PUBLISH_TIMEOUT", "600"))
STOP_REQUESTED = False


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_state(**values) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    state.update(values)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_once(reason: str) -> int:
    started_at = timestamp()
    print(f"publisher daemon: starting {reason} publication at {started_at}", flush=True)
    write_state(last_attempt_at=started_at, last_reason=reason, running=True)
    process = subprocess.Popen(
        [PYTHON, str(PUBLISHER), "--sync", "--push"],
        cwd=ROOT,
        start_new_session=True,
    )
    deadline = time.monotonic() + MAX_RUN_SECONDS
    while True:
        try:
            return_code = process.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if STOP_REQUESTED:
                terminate_process_group(process)
                return_code = 143
                break
            if time.monotonic() >= deadline:
                terminate_process_group(process)
                return_code = 124
                print("publisher daemon: publication timed out; process group terminated", flush=True)
                break
    if return_code != 0:
        print(f"publisher daemon: publication exited {return_code}", flush=True)
    return return_code


def request_stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    last_wall = time.time()
    next_run = 0.0
    retry_at = 0.0
    failure_count = 0
    reason = "startup"

    while not STOP_REQUESTED:
        now = time.time()
        wall_gap = now - last_wall
        if wall_gap >= WAKE_GAP_SECONDS:
            next_run = 0.0
            retry_at = 0.0
            reason = "wake-catch-up"
            print(
                f"publisher daemon: wake detected after {round(wall_gap)}s; scheduling catch-up",
                flush=True,
            )

        if now >= retry_at and now >= next_run:
            result = run_once(reason)
            finished_at = time.time()
            if result == 0:
                failure_count = 0
                retry_at = 0.0
                next_run = finished_at + INTERVAL_SECONDS
                reason = "scheduled"
                write_state(
                    running=False,
                    last_exit_code=0,
                    last_success_at=timestamp(),
                    consecutive_failures=0,
                    next_attempt_at=datetime.fromtimestamp(next_run).astimezone().isoformat(timespec="seconds"),
                )
            else:
                failure_count += 1
                retry_delay = min(INTERVAL_SECONDS, 15 * (2 ** min(failure_count - 1, 5)))
                retry_at = finished_at + retry_delay
                next_run = retry_at
                reason = "retry"
                write_state(
                    running=False,
                    last_exit_code=result,
                    consecutive_failures=failure_count,
                    next_attempt_at=datetime.fromtimestamp(retry_at).astimezone().isoformat(timespec="seconds"),
                )
                print(f"publisher daemon: retrying in {retry_delay}s", flush=True)

        last_wall = time.time()
        time.sleep(PROBE_INTERVAL_SECONDS)

    write_state(running=False, stopped_at=timestamp())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

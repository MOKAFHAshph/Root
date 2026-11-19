#!/usr/bin/env python3
"""Run the Telegram bot and FastAPI Mini App together.

Both child services use the same .env and database. If either service exits,
the other is terminated so a supervisor can restart the complete application.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
import os
from pathlib import Path

from bot.process_lock import acquire_process_lock

ROOT = Path(__file__).resolve().parent


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 8
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parent_lock = acquire_process_lock("jaib-suite", ROOT)
    commands = ([sys.executable, "webapp_main.py"], [sys.executable, "run.py"])
    processes: list[subprocess.Popen] = []
    try:
        for command, service_name in zip(commands, ("MINI_APP", "TELEGRAM_BOT")):
            child_env = os.environ.copy()
            child_env["JAIB_PARENT_PID"] = str(os.getpid())
            child_env["JAIB_SERVICE_NAME"] = service_name
            processes.append(subprocess.Popen(command, cwd=ROOT, env=child_env))
    except Exception:
        _terminate(processes)
        parent_lock.release()
        raise

    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            _terminate(processes)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while not stopping:
            for process in processes:
                code = process.poll()
                if code is not None:
                    stopping = True
                    _terminate(processes)
                    return int(code)
            time.sleep(0.5)
    finally:
        _terminate(processes)
        parent_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

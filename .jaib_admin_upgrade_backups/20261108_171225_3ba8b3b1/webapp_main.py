#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from bot.process_lock import acquire_process_lock

_ROOT = Path(__file__).resolve().parent
_LOCK = acquire_process_lock("mini-app", _ROOT)

from bot.webapp.server import app


if __name__ == "__main__":
    try:
        uvicorn.run(
            app,
            host=os.getenv("WEBAPP_HOST", "0.0.0.0"),
            port=int(os.getenv("WEBAPP_PORT", "8080")),
            proxy_headers=True,
            forwarded_allow_ips=os.getenv("WEBAPP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        )
    finally:
        _LOCK.release()

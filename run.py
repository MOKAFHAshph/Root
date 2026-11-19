#!/usr/bin/env python3
"""تشغيل خدمة بوت Telegram مع منع النسخ المكررة."""
from pathlib import Path

from bot.process_lock import acquire_process_lock

_ROOT = Path(__file__).resolve().parent
_LOCK = acquire_process_lock("telegram-bot", _ROOT)

from bot.app import main


if __name__ == "__main__":
    try:
        main()
    finally:
        _LOCK.release()

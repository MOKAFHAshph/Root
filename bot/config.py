from __future__ import annotations

"""Environment settings, shared imports, paths, and logging bootstrap."""

import asyncio
import base64
import csv
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests
import urllib3
from Crypto.Cipher import AES
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Provider connections verify TLS certificates; custom Jaib CA can be configured.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


APP_VERSION = "20.0.0"
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
ADMIN_ID = int(env("ADMIN_ID", required=True))
DB_FILE = env("DB_FILE", "crypto_sales_bot.db")
LOG_FILE = env("LOG_FILE", "crypto_sales_bot.log")
APP_NAME = env("APP_NAME", "بوت بيع USDT")
ADMIN_CONTACT = env("ADMIN_CONTACT", "@your_username")
MAX_INPUT_ATTEMPTS = int(env("MAX_INPUT_ATTEMPTS", "3"))

DEFAULT_RATE = Decimal(env("DEFAULT_USDT_RATE_YER", "550"))
MIN_USDT_ORDER = Decimal(env("MIN_USDT_ORDER", "5"))
MAX_USDT_ORDER = Decimal(env("MAX_USDT_ORDER", "1000"))
SALES_ENABLED_DEFAULT = env("SALES_ENABLED_DEFAULT", "true").lower() == "true"
AUTO_PAYOUT_ENABLED_DEFAULT = env("AUTO_PAYOUT_ENABLED_DEFAULT", "true").lower() == "true"

# إعدادات المحفظة الجديدة في V15. القيم الموجودة في قاعدة البيانات تتقدم على
# هذه القيم الافتراضية، لذلك يمكن تعديلها لاحقًا من لوحة الإدارة دون إعادة نشر.
DISPLAY_DECIMALS_DEFAULT = min(8, max(0, int(env("DISPLAY_DECIMALS_DEFAULT", "4"))))
LIQUIDITY_ALERT_ENABLED_DEFAULT = env(
    "LIQUIDITY_ALERT_ENABLED_DEFAULT", "true"
).lower() == "true"
LIQUIDITY_ALERT_THRESHOLD_USDT_DEFAULT = max(
    Decimal("0"), Decimal(env("LIQUIDITY_ALERT_THRESHOLD_USDT", "20"))
)
LIQUIDITY_ALERT_RECOVERY_USDT_DEFAULT = max(
    LIQUIDITY_ALERT_THRESHOLD_USDT_DEFAULT,
    Decimal(env("LIQUIDITY_ALERT_RECOVERY_USDT", "25")),
)
LIQUIDITY_ALERT_COOLDOWN_MINUTES_DEFAULT = max(
    5, int(env("LIQUIDITY_ALERT_COOLDOWN_MINUTES", "360"))
)

# إعدادات مراقبة السحب وتشخيص Binance المشترك بين البوت وMini App.
WALLET_AUTO_PENDING_TTL_MINUTES = max(1, int(env("WALLET_AUTO_PENDING_TTL_MINUTES", "15")))
WALLET_MANUAL_PENDING_TTL_HOURS = max(1, int(env("WALLET_MANUAL_PENDING_TTL_HOURS", "24")))
WALLET_PROCESSING_RECOVERY_GRACE_MINUTES = max(1, int(env("WALLET_PROCESSING_RECOVERY_GRACE_MINUTES", "5")))
BINANCE_HEALTH_CHECK_INTERVAL_SECONDS = 30
BINANCE_EGRESS_IP_CHECK_ENABLED = env("BINANCE_EGRESS_IP_CHECK_ENABLED", "true").lower() == "true"
BINANCE_EGRESS_IP_CACHE_SECONDS = max(60, int(env("BINANCE_EGRESS_IP_CACHE_SECONDS", "600")))
BINANCE_EGRESS_IP_TIMEOUT_SECONDS = max(1, int(env("BINANCE_EGRESS_IP_TIMEOUT_SECONDS", "4")))
BINANCE_SHARED_CONFIG_CACHE_SECONDS = max(30, int(env("BINANCE_SHARED_CONFIG_CACHE_SECONDS", "120")))

PAYMENT_METHOD_LABEL = env("PAYMENT_METHOD_LABEL", "محفظة Jaib")
JAIB_DISPLAY_NAME = env("JAIB_DISPLAY_NAME", "Jaib")
JAIB_PRIMARY_PHONE = env("JAIB_PRIMARY_PHONE", "")
JAIB_SECONDARY_PHONE = env("JAIB_SECONDARY_PHONE", "")
JAIB_STRICT_EXACT_AMOUNT = env("JAIB_STRICT_EXACT_AMOUNT", "true").lower() == "true"

BINANCE_API_KEY = env("BINANCE_API_KEY", "")
BINANCE_API_SECRET = env("BINANCE_API_SECRET", "")
BINANCE_WITHDRAW_ENABLED = env("BINANCE_WITHDRAW_ENABLED", "false").lower() == "true"
BINANCE_COIN = env("BINANCE_COIN", "USDT")
AUTO_ONCHAIN_WITHDRAW = env("AUTO_ONCHAIN_WITHDRAW", "true").lower() == "true"
BINANCE_API_BASE = env("BINANCE_API_BASE", "https://api.binance.com")
BINANCE_RECV_WINDOW = int(env("BINANCE_RECV_WINDOW", "10000"))
BINANCE_WALLET_TYPE = int(env("BINANCE_WALLET_TYPE", "0"))
ALLOWED_NETWORKS_RAW = env("ALLOWED_NETWORKS", "BSC:BEP20")

JAIB_PHONE = env("JAIB_PHONE", "")
JAIB_PASSWORD = env("JAIB_PASSWORD", "")
JAIB_FCM_KEY_B64 = env("JAIB_FCM_KEY_B64", "")
JAIB_FCM_IV_B64 = env("JAIB_FCM_IV_B64", "")
JAIB_CLIENT_IV_B64 = env("JAIB_CLIENT_IV_B64", "")
JAIB_DEVICE_ID = env("JAIB_DEVICE_ID", "")
JAIB_SMS_CODE = env("JAIB_SMS_CODE", "")
JAIB_TKN_NOT = env("JAIB_TKN_NOT", "")
JAIB_INIT_VALUE = env("JAIB_INIT_VALUE", "")
JAIB_AUTH_HEADER = env("JAIB_AUTH_HEADER", "")
JAIB_BOOTSTRAP_SERVERS = [
    server.strip()
    for server in env(
        "JAIB_BOOTSTRAP_SERVERS",
        "https://www.w-jaib.com:2074,https://api.jaib.com.ye:1074,https://api3.jaib.com.ye:9974,https://api.e-jaib.com:2174,https://api.ahd.com.ye:6073,https://api.jaib.com.ye:6072",
    ).split(",")
    if server.strip()
]
# Preserve configured priority and supplement missing reference fallback hosts.
for _jaib_fallback in (
    "https://www.w-jaib.com:2074",
    "https://api.jaib.com.ye:1074",
    "https://api3.jaib.com.ye:9974",
    "https://api.e-jaib.com:2174",
    "https://api.ahd.com.ye:6073",
    "https://api.jaib.com.ye:6072",
):
    if _jaib_fallback not in JAIB_BOOTSTRAP_SERVERS:
        JAIB_BOOTSTRAP_SERVERS.append(_jaib_fallback)

JAIB_OP_QUERY_REF = int(env("JAIB_OP_QUERY_REF", "567"))
JAIB_INIT_TIMEOUT_SECONDS = max(5, int(env("JAIB_INIT_TIMEOUT_SECONDS", "10")))
JAIB_REQUEST_TIMEOUT_SECONDS = max(5, int(env("JAIB_REQUEST_TIMEOUT_SECONDS", "15")))
JAIB_VERIFY_RETRY_ATTEMPTS = min(5, max(1, int(env("JAIB_VERIFY_RETRY_ATTEMPTS", "3"))))
JAIB_VERIFY_RETRY_DELAY_SECONDS = max(0.0, float(env("JAIB_VERIFY_RETRY_DELAY_SECONDS", "3")))
JAIB_API_UTC_OFFSET_HOURS = float(env("JAIB_API_UTC_OFFSET_HOURS", "3"))
JAIB_HEALTH_CHECK_INTERVAL_SECONDS = max(
    30, int(env("JAIB_HEALTH_CHECK_INTERVAL_SECONDS", "60"))
)

# قنوات المشروع الثلاث مستقلة. تحفظ لوحة الإدارة القيم في قاعدة البيانات،
# وتبقى هذه المتغيرات قيم بدء آمنة للخادم الجديد أو بعد النقل.
CHANNEL_USERS = env("CHANNEL_USERS", "").strip()
CHANNEL_ADMIN = env("CHANNEL_ADMIN", "").strip()
CHANNEL_BACKUP = env("CHANNEL_BACKUP", "").strip()

BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

LOG_PATH = (BASE_DIR / LOG_FILE).resolve()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_LEVEL_DEFAULT = env("LOG_LEVEL", "INFO").upper()
DEBUG_MODE_DEFAULT = env("DEBUG_MODE", "false").lower() == "true"
LOG_MAX_BYTES_DEFAULT = max(1_048_576, int(env("LOG_MAX_BYTES", str(10 * 1024 * 1024))))
LOG_BACKUP_COUNT_DEFAULT = max(1, int(env("LOG_BACKUP_COUNT", "5")))


def _configured_secrets() -> tuple[str, ...]:
    candidates = {
        BOT_TOKEN,
        os.environ.get("TONCENTER_API_KEY", ""),
        BINANCE_API_KEY,
        BINANCE_API_SECRET,
        JAIB_PASSWORD,
        JAIB_AUTH_HEADER,
        JAIB_TKN_NOT,
        JAIB_FCM_KEY_B64,
        JAIB_FCM_IV_B64,
        JAIB_CLIENT_IV_B64,
    }
    # Some provider secrets are loaded in later compatibility layers. Read their
    # environment values here as well so they are redacted even before those
    # modules are imported.
    for variable in (
        "SETTINGS_ENCRYPTION_KEY",
        "JAIB_CLIENT_KEY",
        "JAIB_CLIENT_SECRET",
        "JAIB_RETURN_KEY",
        "JAIB_SMS_CODE",
        "JAIB_DEVICE_ID",
    ):
        candidates.add(os.getenv(variable, ""))
    return tuple(
        sorted((item for item in candidates if item and len(item) >= 6), key=len, reverse=True)
    )


_TELEGRAM_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}")
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|api[_-]?key|api[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|private[_-]?key|password|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def redact_sensitive_text(value: Any) -> str:
    text = str(value)
    for secret in _configured_secrets():
        text = text.replace(secret, "***REDACTED***")
    text = _TELEGRAM_TOKEN_PATTERN.sub("***REDACTED_TELEGRAM_TOKEN***", text)
    text = _BEARER_PATTERN.sub("Bearer ***REDACTED***", text)
    text = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***",
        text,
    )
    return text


class SensitiveDataFilter(logging.Filter):
    """Redact configured secrets from the record message before handlers emit it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_sensitive_text(record.getMessage())
            record.args = ()
        except Exception:
            # Logging must never fail the application.
            return True
        return True


class RedactingFormatter(logging.Formatter):
    """Redact secrets from the final line, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


_LOG_HANDLER_TAG = "bn_wallet_managed"


def _normalize_log_level(value: str) -> str:
    level = str(value or "INFO").upper()
    return level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"


def configure_logging(
    *,
    level: Optional[str] = None,
    debug: Optional[bool] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
) -> None:
    """Configure one rotating file handler and one console handler safely."""

    selected_level = "DEBUG" if debug else _normalize_log_level(level or LOG_LEVEL_DEFAULT)
    if debug is None and DEBUG_MODE_DEFAULT:
        selected_level = "DEBUG"
    selected_max = max(1_048_576, int(max_bytes or LOG_MAX_BYTES_DEFAULT))
    selected_backups = max(1, min(int(backup_count or LOG_BACKUP_COUNT_DEFAULT), 50))

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _LOG_HANDLER_TAG, False):
            try:
                handler.flush()
                handler.close()
            finally:
                root.removeHandler(handler)

    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = SensitiveDataFilter()

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=selected_max,
        backupCount=selected_backups,
        encoding="utf-8",
    )
    setattr(file_handler, _LOG_HANDLER_TAG, True)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, selected_level))
    file_handler.addFilter(redactor)

    console_handler = logging.StreamHandler()
    setattr(console_handler, _LOG_HANDLER_TAG, True)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(getattr(logging, selected_level))
    console_handler.addFilter(redactor)

    root.setLevel(getattr(logging, selected_level))
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Keep noisy HTTP internals useful only in explicit DEBUG mode.
    for logger_name in ("httpx", "httpcore", "telegram.ext.ExtBot"):
        logging.getLogger(logger_name).setLevel(logging.DEBUG if selected_level == "DEBUG" else logging.WARNING)


def flush_log_handlers() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


configure_logging()
log = logging.getLogger("bn_wallet")


from bot.runtime import export_globals as _export_globals
_export_globals(globals())

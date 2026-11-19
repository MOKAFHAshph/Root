"""Complete FastAPI backend for the Telegram Mini App.

The Mini App and Telegram bot share the same wallet, customer, reporting, and
payout services. User interface drafts are kept outside financial reports;
only final outcomes appear in statistics and user-facing histories.
"""
from __future__ import annotations

import asyncio
import logging
import os
import base64
import csv
import io
import hashlib
import hmac
from html import escape as html_escape
import json
import sqlite3
import time
import uuid
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

# Load the complete modular runtime without starting Telegram polling.
from bot.app import runtime

BOT_TOKEN = str(runtime.get("BOT_TOKEN") or "")
APP_NAME = str(runtime.get("APP_NAME") or "USDT Wallet")
ADMIN_ID = int(runtime.get("ADMIN_ID") or 0)
DBX = runtime.get("DBX")
env = runtime.get("env")
now_str = runtime.get("now_str")
q2 = runtime.get("q2")
q8 = runtime.get("q8")
format_display_amount = runtime.get("format_display_amount")
format_yer_amount = runtime.get("format_yer_amount")
display_decimals = runtime.get("display_decimals")
set_display_decimals = runtime.get("set_display_decimals")
wallet_decimal = runtime.get("wallet_decimal")
WalletUserError = runtime.get("WalletUserError")
WalletTechnicalError = runtime.get("WalletTechnicalError")
wallet_user_stats = runtime.get("wallet_user_stats")
wallet_system_stats = runtime.get("wallet_system_stats")
wallet_report_operations = runtime.get("wallet_report_operations")
wallet_prepare_deposit = runtime.get("wallet_prepare_deposit")
wallet_create_deposit = runtime.get("wallet_create_deposit")
wallet_get_deposit = runtime.get("wallet_get_deposit")
wallet_verify_deposit_reference_core = runtime.get("wallet_verify_deposit_reference_core")
wallet_network_routes = runtime.get("wallet_network_routes")
wallet_validate_destination = runtime.get("wallet_validate_destination")
wallet_provider_for_network = runtime.get("wallet_provider_for_network")
wallet_create_withdrawal = runtime.get("wallet_create_withdrawal")
wallet_get_withdrawal = runtime.get("wallet_get_withdrawal")
wallet_submit_withdrawal = runtime.get("wallet_submit_withdrawal")
wallet_complete_withdrawal = runtime.get("wallet_complete_withdrawal")
wallet_release_withdrawal = runtime.get("wallet_release_withdrawal")
wallet_credit_deposit = runtime.get("wallet_credit_deposit")
wallet_admin_adjust = runtime.get("wallet_admin_adjust")
wallet_balances = runtime.get("wallet_balances")
wallet_deposit_enabled = runtime.get("wallet_deposit_enabled")
wallet_withdraw_enabled = runtime.get("wallet_withdraw_enabled")
wallet_set_deposit_enabled = runtime.get("wallet_set_deposit_enabled")
wallet_set_withdraw_enabled = runtime.get("wallet_set_withdraw_enabled")
wallet_expire_safe_pending = runtime.get("wallet_expire_safe_pending")
flush_wallet_notification_outbox = runtime.get("flush_wallet_notification_outbox")
wallet_enqueue_user_notification = runtime.get("wallet_enqueue_user_notification")
record_runtime_identity = runtime.get("record_runtime_identity")
binance_auth_state = runtime.get("binance_auth_state")
binance_health_check = runtime.get("binance_health_check")
binance_claim_failure_notification = runtime.get("binance_claim_failure_notification")
is_binance_service_error = runtime.get("is_binance_service_error")
referral_summary = runtime.get("referral_summary")
generate_user_pdf_report = runtime.get("generate_user_pdf_report")
binance_live_limits = runtime.get("binance_live_limits")
get_public_liquidity = runtime.get("get_public_liquidity")
public_liquidity_enabled = runtime.get("public_liquidity_enabled")
set_public_liquidity_enabled = runtime.get("set_public_liquidity_enabled")
public_liquidity_reserve = runtime.get("public_liquidity_reserve")
set_public_liquidity_reserve = runtime.get("set_public_liquidity_reserve")
liquidity_alert_enabled = runtime.get("liquidity_alert_enabled")
set_liquidity_alert_enabled = runtime.get("set_liquidity_alert_enabled")
liquidity_alert_threshold = runtime.get("liquidity_alert_threshold")
set_liquidity_alert_threshold = runtime.get("set_liquidity_alert_threshold")
liquidity_alert_recovery = runtime.get("liquidity_alert_recovery")
set_liquidity_alert_recovery = runtime.get("set_liquidity_alert_recovery")
liquidity_alert_cooldown_minutes = runtime.get("liquidity_alert_cooldown_minutes")
set_liquidity_alert_cooldown_minutes = runtime.get("set_liquidity_alert_cooldown_minutes")
withdrawal_operation_limit_enabled = runtime.get("withdrawal_operation_limit_enabled")
set_withdrawal_operation_limit_enabled = runtime.get("set_withdrawal_operation_limit_enabled")
withdrawal_operation_limit = runtime.get("withdrawal_operation_limit")
set_withdrawal_operation_limit = runtime.get("set_withdrawal_operation_limit")
effective_withdrawal_ceiling = runtime.get("effective_withdrawal_ceiling")
validate_operation_limit = runtime.get("validate_operation_limit")
validate_live_liquidity = runtime.get("validate_live_liquidity")
complete_referral_after_activation = runtime.get("complete_referral_after_activation")
ensure_pending_customer = runtime.get("ensure_pending_customer")
_upsert_registration = runtime.get("_upsert_registration")
wallet_admin_contact_url = runtime.get("wallet_admin_contact_url")
list_broadcast_targets = runtime.get("list_broadcast_targets")
create_backup_file = runtime.get("create_backup_file")
list_required_channels = runtime.get("list_required_channels")
save_required_channel = runtime.get("save_required_channel")
delete_required_channel = runtime.get("delete_required_channel")
ALL_ADMIN_PERMISSIONS = tuple(runtime.get("ALL_ADMIN_PERMISSIONS"))
ADMIN_PERMISSION_LABELS = dict(runtime.get("ADMIN_PERMISSION_LABELS"))
LOG_PATH = Path(runtime.get("LOG_PATH"))
current_log_level = runtime.get("current_log_level")
debug_mode_enabled = runtime.get("debug_mode_enabled")
current_log_max_bytes = runtime.get("current_log_max_bytes")
current_log_backup_count = runtime.get("current_log_backup_count")
configure_logging = runtime.get("configure_logging")
truncate_active_log = runtime.get("_truncate_active_log")
flush_log_handlers = runtime.get("flush_log_handlers")
get_account_verification = runtime.get("get_account_verification")
ensure_account_verification = runtime.get("ensure_account_verification")
account_verification_public = runtime.get("account_verification_public")
save_verification_full_name = runtime.get("save_verification_full_name")
save_verification_primary_phone = runtime.get("save_verification_primary_phone")
save_verification_alt_phone = runtime.get("save_verification_alt_phone")
verify_account_payment = runtime.get("verify_account_payment")
submit_account_verification = runtime.get("submit_account_verification")
restart_account_verification = runtime.get("restart_account_verification")
ACCOUNT_VERIFICATION_RECIPIENT_PHONE = str(runtime.get("ACCOUNT_VERIFICATION_RECIPIENT_PHONE") or "")
ACCOUNT_VERIFICATION_RECIPIENT_NAME = str(runtime.get("ACCOUNT_VERIFICATION_RECIPIENT_NAME") or "")
NETWORK_ADDRESS_VIDEO_URL = str(runtime.get("NETWORK_ADDRESS_VIDEO_URL") or "")
BOT_USAGE_GUIDE_URL = str(runtime.get("BOT_USAGE_GUIDE_URL") or "")
verification_recipient_phone = runtime.get("verification_recipient_phone")
verification_recipient_name = runtime.get("verification_recipient_name")
network_address_video_url = runtime.get("network_address_video_url")
bot_usage_guide_url = runtime.get("bot_usage_guide_url")
customer_network_fee = runtime.get("customer_network_fee")
effective_network_fee = runtime.get("effective_network_fee")
maintenance_enabled = runtime.get("maintenance_enabled")
maintenance_reason = runtime.get("maintenance_reason")
set_maintenance = runtime.get("set_maintenance")
account_approval_mode = runtime.get("account_approval_mode")
account_auto_approval_delay_seconds = runtime.get("account_auto_approval_delay_seconds")
account_verification_max_yer = runtime.get("account_verification_max_yer")
set_unified_notification_channel = runtime.get("set_unified_notification_channel")
unified_notification_channel_id = runtime.get("unified_notification_channel_id")
users_channel_id = runtime.get("users_channel_id")
admin_channel_id = runtime.get("admin_channel_id")
project_backup_channel_id = runtime.get("project_backup_channel_id")
set_users_channel = runtime.get("set_users_channel")
set_admin_channel = runtime.get("set_admin_channel")
set_project_backup_channel = runtime.get("set_project_backup_channel")
jaib_health_state = runtime.get("jaib_health_state")
jaib_health_check = runtime.get("jaib_health_check")
list_managed_assets = runtime.get("list_managed_assets")
managed_asset_liquidity = runtime.get("managed_asset_liquidity")
upsert_managed_asset = runtime.get("upsert_managed_asset")
delete_managed_asset = runtime.get("delete_managed_asset")
list_managed_networks = runtime.get("list_managed_networks")
create_managed_network = runtime.get("create_managed_network")
update_managed_network = runtime.get("update_managed_network")
set_managed_network_enabled = runtime.get("set_managed_network_enabled")
delete_managed_network = runtime.get("delete_managed_network")
effective_customer_rate = runtime.get("effective_customer_rate")
customer_custom_rate = runtime.get("customer_custom_rate")
set_customer_custom_rate = runtime.get("set_customer_custom_rate")
set_managed_network_minimum_floor = runtime.get("set_managed_network_minimum_floor")
list_tutorial_buttons = runtime.get("list_tutorial_buttons")
create_tutorial_button = runtime.get("create_tutorial_button")
update_tutorial_button = runtime.get("update_tutorial_button")
delete_tutorial_button = runtime.get("delete_tutorial_button")
normalize_txid = runtime.get("normalize_txid")

STATIC_DIR = Path(__file__).resolve().parent / "static"
WEBAPP_AUTH_MAX_AGE_SECONDS = int(env("WEBAPP_AUTH_MAX_AGE_SECONDS", "3600"))
WEBAPP_ACTION_MAX_AGE_SECONDS = int(env("WEBAPP_ACTION_MAX_AGE_SECONDS", "900"))
JAIB_PRIMARY_PHONE = str(runtime.get("JAIB_PRIMARY_PHONE") or "")
JAIB_SECONDARY_PHONE = str(runtime.get("JAIB_SECONDARY_PHONE") or "")
JAIB_DISPLAY_NAME = str(runtime.get("JAIB_DISPLAY_NAME") or "Jaib")
JAIB_SECONDARY_BENEFICIARY_NAME = str(runtime.get("JAIB_SECONDARY_BENEFICIARY_NAME") or "عبدالرحمن علي")
PUBLIC_LIQUIDITY_LABEL = str(runtime.get("PUBLIC_LIQUIDITY_LABEL") or "السيولة المتوفرة حالياً")
ADMIN_CONTACT = str(runtime.get("ADMIN_CONTACT") or "")
WALLET_MIN_DEPOSIT_YER = wallet_decimal(runtime.get("WALLET_MIN_DEPOSIT_YER") or 0)
WALLET_MAX_WITHDRAW_USDT = wallet_decimal(runtime.get("WALLET_MAX_WITHDRAW_USDT") or 0)

app = FastAPI(title=f"{APP_NAME} Mini App", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
web_log = logging.getLogger("bn_wallet.webapp")


async def _webapp_runtime_heartbeat() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(record_runtime_identity, "MINI_APP")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            web_log.warning("Runtime heartbeat failed service=MINI_APP error=%s", type(exc).__name__)


@app.on_event("startup")
async def _webapp_runtime_startup() -> None:
    diagnostics = await asyncio.to_thread(record_runtime_identity, "MINI_APP", refresh_ip=True)
    health = await asyncio.to_thread(binance_health_check, force_ip=True)
    expired = await asyncio.to_thread(wallet_expire_safe_pending)
    web_log.info(
        "Runtime identity service=%s pid=%s parent_pid=%s cwd=%s env=%s db=%s key_fp=%s egress_ip=%s expired=%s",
        diagnostics["service"], diagnostics["pid"], diagnostics["parent_pid"],
        diagnostics["cwd"], diagnostics["env_path"], diagnostics["db_path"],
        diagnostics["key_fingerprint"], diagnostics["egress_ip"], len(expired),
    )
    web_log.info("Binance startup check service=MINI_APP ok=%s reason=%s", health.get("ok"), health.get("reason") or "")
    app.state.runtime_heartbeat_task = asyncio.create_task(_webapp_runtime_heartbeat())


@app.on_event("shutdown")
async def _webapp_runtime_shutdown() -> None:
    task = getattr(app.state, "runtime_heartbeat_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await asyncio.to_thread(record_runtime_identity, "MINI_APP", stopped=True)


# ---------------------------------------------------------------------------
# WebApp authentication and one-time signed actions
# ---------------------------------------------------------------------------

def validate_init_data(init_data: str) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Bot token is not configured")
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData is required")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing initData hash")
    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")
    auth_date = int(pairs.get("auth_date") or 0)
    if not auth_date or abs(int(time.time()) - auth_date) > WEBAPP_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Expired Telegram initData")
    try:
        user = json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload") from exc
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing")
    return user


def current_user(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    init_data: str | None = Query(default=None),
) -> dict[str, Any]:
    user = validate_init_data(x_telegram_init_data or init_data or "")
    if maintenance_enabled() and not DBX.is_admin(int(user["id"])) and request.url.path == "/api/bootstrap":
        return user
    if maintenance_enabled() and not DBX.is_admin(int(user["id"])):
        reason = maintenance_reason()
        raise HTTPException(
            status_code=503,
            detail="التطبيق في وضع الصيانة حاليًا." + (f" السبب: {reason}" if reason else ""),
        )
    return user


def _active_customer(user: dict[str, Any], *, allow_admin: bool = True) -> dict[str, Any]:
    telegram_id = int(user["id"])
    if allow_admin and DBX.is_admin(telegram_id):
        row = DBX.get_customer_by_telegram(telegram_id)
        return dict(row) if row else {
            "id": None,
            "telegram_id": telegram_id,
            "full_name": "مدير النظام",
            "telegram_username": user.get("username") or "",
            "phone_primary": "",
            "phone_alt": "",
            "status": "ADMIN",
        }
    row = DBX.get_customer_by_telegram(telegram_id)
    if not row or row["status"] != "ACTIVE":
        raise HTTPException(status_code=403, detail="الحساب غير نشط")
    return dict(row)


def _require_admin(user: dict[str, Any], permission: str | None = None) -> int:
    telegram_id = int(user["id"])
    if not DBX.is_admin(telegram_id):
        raise HTTPException(status_code=403, detail="هذه الخدمة للإدارة فقط")
    if permission and not DBX.has_permission(telegram_id, permission):
        raise HTTPException(status_code=403, detail="لا تملك الصلاحية المطلوبة")
    return telegram_id


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign_action(action: str, telegram_id: int, payload: dict[str, Any]) -> str:
    body = {
        "action": action,
        "telegram_id": int(telegram_id),
        "iat": int(time.time()),
        "nonce": uuid.uuid4().hex,
        "payload": payload,
    }
    encoded = _b64encode(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(BOT_TOKEN.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def _decode_action(token: str, action: str, telegram_id: int) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(BOT_TOKEN.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            raise ValueError("signature")
        body = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="رمز التأكيد غير صالح") from exc
    if body.get("action") != action or int(body.get("telegram_id") or 0) != int(telegram_id):
        raise HTTPException(status_code=400, detail="رمز التأكيد لا يخص هذه العملية")
    if int(time.time()) - int(body.get("iat") or 0) > WEBAPP_ACTION_MAX_AGE_SECONDS:
        raise HTTPException(status_code=400, detail="انتهت صلاحية رمز التأكيد")
    return body


def _ensure_webapp_schema() -> None:
    with DBX.connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS webapp_action_tokens (
                nonce TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                consumed_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_webapp_action_user "
            "ON webapp_action_tokens(telegram_user_id, action)"
        )
        con.commit()


def _consume_action(body: dict[str, Any]) -> None:
    try:
        with DBX.connect() as con:
            con.execute(
                "INSERT INTO webapp_action_tokens(nonce, telegram_user_id, action, consumed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(body["nonce"]),
                    int(body["telegram_id"]),
                    str(body["action"]),
                    now_str(),
                ),
            )
            con.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="تم تنفيذ هذا الطلب مسبقًا") from exc


_ensure_webapp_schema()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class DepositVerifyRequest(BaseModel):
    reference: str = Field(min_length=3, max_length=100)
    session_token: str | None = None
    deposit_id: int | None = None


class WithdrawalQuoteRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    network_code: str = Field(min_length=2, max_length=20)
    address: str = Field(min_length=8, max_length=200)
    memo: str = Field(default="", max_length=100)


class WithdrawalCreateRequest(BaseModel):
    quote_token: str


class CustomerStatusRequest(BaseModel):
    status: Literal["ACTIVE", "SUSPENDED", "PENDING"]


class CustomerTrustedRequest(BaseModel):
    trusted: bool


class CustomerExchangeRateRequest(BaseModel):
    rate: Decimal | None = Field(default=None, gt=0, le=1_000_000)


class BalanceAdjustmentRequest(BaseModel):
    amount: Decimal
    reason: str = Field(min_length=3, max_length=500)


class DepositDecisionRequest(BaseModel):
    reason: str = Field(default="تمت المراجعة من Mini App", min_length=3, max_length=500)


class WithdrawalReleaseRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class WithdrawalCompleteRequest(BaseModel):
    txid: str = Field(min_length=6, max_length=300)
    note: str = Field(default="تم التنفيذ يدويًا بعد المراجعة", min_length=3, max_length=500)


class SettingsRequest(BaseModel):
    values: dict[str, Any]


class BroadcastRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class CustomerCreateRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=150)
    phone_primary: str = Field(min_length=5, max_length=30)
    phone_alt: str = Field(default="", max_length=30)
    telegram_id: int | None = None
    telegram_username: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=1000)


class CustomerUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    phone_primary: str | None = Field(default=None, min_length=5, max_length=30)
    phone_alt: str | None = Field(default=None, max_length=30)
    telegram_id: int | None = None
    telegram_username: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)


class AdminCreateRequest(BaseModel):
    telegram_id: int
    display_name: str = Field(min_length=2, max_length=150)
    username: str = Field(default="", max_length=100)
    permissions: list[str] = Field(default_factory=list)


class AdminUpdateRequest(BaseModel):
    status: Literal["ACTIVE", "SUSPENDED"] | None = None
    permissions: list[str] | None = None


class RequiredChannelRequest(BaseModel):
    chat_id: str = Field(min_length=2, max_length=100)
    username: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=200)
    invite_link: str = Field(default="", max_length=500)


class BackupRestoreRequest(BaseModel):
    payload: dict[str, Any]


class LogSettingsRequest(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    debug: bool
    max_bytes: int = Field(ge=1_048_576, le=524_288_000)
    backup_count: int = Field(ge=1, le=50)


class OnboardingValueRequest(BaseModel):
    value: str = Field(min_length=1, max_length=200)


class OnboardingVerifyRequest(BaseModel):
    reference: str = Field(min_length=3, max_length=100)


class ManagedAssetRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=12)
    display_name: str = Field(min_length=2, max_length=100)
    enabled: bool = False


class ManagedNetworkRequest(BaseModel):
    asset_symbol: str = Field(default="USDT", min_length=2, max_length=12)
    network_code: str = Field(min_length=2, max_length=20)
    display_label: str = Field(min_length=2, max_length=100)
    address_regex: str = Field(min_length=1, max_length=500)
    wallet_address: str = Field(default="", max_length=300)
    fee_override: str = Field(default="", max_length=50)
    enabled: bool = False


class ManagedNetworkUpdateRequest(BaseModel):
    wallet_address: str | None = Field(default=None, max_length=300)
    fee_override: str | None = Field(default=None, max_length=50)
    minimum_floor: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool | None = None


class TutorialButtonCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    url: str = Field(min_length=8, max_length=1000)
    icon: Literal["📹", "🎬", "📖", "ℹ️"] = "📹"
    custom_emoji_id: str | None = Field(default=None, max_length=128)


class TutorialButtonUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=1000)
    icon: Literal["📹", "🎬", "📖", "ℹ️"] | None = None
    custom_emoji_id: str | None = Field(default=None, max_length=128)
    active: bool | None = None
    sort_order: int | None = Field(default=None, ge=-100_000, le=100_000)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _safe_error(exc: Exception) -> HTTPException:
    if WalletUserError and isinstance(exc, WalletUserError):
        return HTTPException(status_code=400, detail=str(exc))
    if is_binance_service_error(exc):
        return HTTPException(status_code=503, detail="السحب متوقف مؤقتًا بسبب اتصال Binance، وسيُفتح تلقائيًا فور نجاح الفحص.")
    return HTTPException(status_code=500, detail="حدث خطأ تقني. لم تُكرر العملية تلقائيًا.")


def _public_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": operation.get("created_at"),
        "type": operation.get("type"),
        "type_label": operation.get("type_label"),
        "reference": operation.get("reference"),
        "amount": str(operation.get("amount") or "0"),
        "currency": operation.get("currency") or "USDT",
        "status": operation.get("status"),
        "successful": bool(operation.get("successful")),
        "failure_code": operation.get("failure_code") or "",
        "txid": normalize_txid(operation.get("txid")),
        "network_code": operation.get("network_code") or "",
        "destination_address": operation.get("destination_address") or "",
    }


async def _ui_thread_call(func: Any, *args: Any, timeout: float = 6.0, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=timeout,
    )


async def _ui_liquidity_snapshot() -> dict[str, Any]:
    if not public_liquidity_enabled():
        return {
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available_display": "0",
            "available_known": False,
            "stale": False,
        }
    try:
        return await _ui_thread_call(get_public_liquidity, force_refresh=False, timeout=6)
    except Exception:
        return {
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available_display": "0",
            "available_known": False,
            "stale": False,
        }


async def _public_network_route(item: dict[str, Any]) -> dict[str, Any]:
    route = {key: value for key, value in dict(item).items() if key != "provider"}
    if item.get("provider") == "BINANCE":
        try:
            limits = await _ui_thread_call(
                binance_live_limits,
                route["code"],
                force_refresh=False,
                timeout=6,
            )
            actual_fee = effective_network_fee(route["code"], limits["fee"])
            route["minimum"] = q8(limits["minimum"])
            route["system_fee"] = q8(actual_fee)
            route["customer_fee"] = q8(customer_network_fee(actual_fee))
        except Exception:
            route["minimum"] = ""
            route["system_fee"] = ""
            route["customer_fee"] = ""
    return route


async def _bot_context() -> Any:
    from telegram import Bot

    bot = Bot(BOT_TOKEN)
    await bot.initialize()
    return bot


async def _close_bot(bot: Any) -> None:
    try:
        await bot.shutdown()
    except Exception:
        pass


async def _notify_admins(text: str) -> None:
    bot = await _bot_context()
    try:
        recipients: set[int | str] = {ADMIN_ID}
        channel = admin_channel_id()
        if channel is not None:
            recipients.add(channel)
        for row in DBX.list_admins():
            if row["status"] == "ACTIVE":
                recipients.add(int(row["telegram_id"]))
        for recipient in recipients:
            try:
                await bot.send_message(chat_id=recipient, text=text, parse_mode="HTML")
            except Exception:
                continue
    finally:
        await _close_bot(bot)


async def _notify_binance_failure_now(exc: BaseException, reference: str) -> None:
    if not is_binance_service_error(exc) or not binance_claim_failure_notification():
        return
    state = binance_auth_state()
    await _notify_admins(
        "<b>🚨 تم إيقاف سحب Binance مؤقتًا</b>\n\n"
        f"السبب: <code>{html_escape(str(exc))}</code>\n"
        f"IP: <code>{html_escape(str(state.get('egress_ip') or 'UNKNOWN'))}</code>\n"
        f"المرجع: <code>{html_escape(reference)}</code>\n\n"
        "سيتم الفحص كل 30 ثانية وفتح السحب فور نجاح الفحص."
    )


async def _submit_withdrawal_background(withdrawal_id: int, initiated_by_admin: bool = False) -> None:
    bot = await _bot_context()
    try:
        await wallet_submit_withdrawal(
            int(withdrawal_id),
            SimpleNamespace(bot=bot),
            target=None,
            initiated_by_admin=initiated_by_admin,
        )
    finally:
        await _close_bot(bot)


async def _flush_wallet_notifications_background(telegram_user_id: int | None = None) -> None:
    bot = await _bot_context()
    try:
        await flush_wallet_notification_outbox(
            bot,
            telegram_user_id=int(telegram_user_id) if telegram_user_id is not None else None,
            limit=100,
        )
    finally:
        await _close_bot(bot)


async def _notify_withdrawal_completed(withdrawal: dict[str, Any]) -> None:
    bot = await _bot_context()
    try:
        await bot.send_message(
            chat_id=int(withdrawal["telegram_user_id"]),
            text=(
                "<b>✅ تم تنفيذ السحب بنجاح</b>\n\n"
                f"🧾 <code>{html_escape(str(withdrawal['withdrawal_code']))}</code>\n"
                f"💵 <b>{format_display_amount(withdrawal['amount_usdt'])} USDT</b>\n"
                f"🌐 {html_escape(str(withdrawal['network_label']))}\n"
                f"🔗 TxID: <code>{html_escape(normalize_txid(withdrawal.get('txid')) or '—')}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass
    finally:
        await _close_bot(bot)


async def _broadcast_background(admin_id: int, text: str) -> None:
    bot = await _bot_context()
    sent = 0
    failed = 0
    targets = list_broadcast_targets()
    try:
        for telegram_id in targets:
            try:
                await bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.06)
    finally:
        await _close_bot(bot)
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "WEBAPP_BROADCAST_FINISHED",
        "broadcast",
        str(uuid.uuid4()),
        f"total={len(targets)}; sent={sent}; failed={failed}",
    )


_BANNER_DEFAULTS = {
    "mini_banner_enabled": "1",
    "mini_banner_animation_enabled": "1",
    "mini_banner_badge": "خدمة تلقائية",
    "mini_banner_title": "شراء USDT تلقائي عبر Jaib",
    "mini_banner_text": "تحقق فوري ومحفظة داخلية وسحب عبر الشبكات المتاحة.",
    "mini_banner_image_url": "",
    "mini_banner_link_url": "",
    "mini_banner_button_text": "ابدأ الآن",
    "mini_banner_width_percent": "100",
    "mini_banner_height_px": "150",
    "mini_banner_image_fit": "cover",
}


def _setting_value(key: str, default: str = "") -> str:
    value = DBX.get_setting(key)
    if value is None:
        value = default
    return str(value).strip()


def _setting_enabled(key: str, default: bool = False) -> bool:
    value = DBX.get_setting(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _banner_payload() -> dict[str, Any]:
    try:
        width_percent = max(80, min(100, int(_setting_value("mini_banner_width_percent", "100"))))
    except (TypeError, ValueError):
        width_percent = 100
    try:
        height_px = max(90, min(260, int(_setting_value("mini_banner_height_px", "150"))))
    except (TypeError, ValueError):
        height_px = 150
    image_fit = _setting_value("mini_banner_image_fit", "cover").lower()
    if image_fit not in {"cover", "contain"}:
        image_fit = "cover"
    return {
        "enabled": _setting_enabled("mini_banner_enabled", True),
        "animation_enabled": _setting_enabled("mini_banner_animation_enabled", True),
        "badge": _setting_value("mini_banner_badge", _BANNER_DEFAULTS["mini_banner_badge"])[:40],
        "title": _setting_value("mini_banner_title", _BANNER_DEFAULTS["mini_banner_title"])[:90],
        "text": _setting_value("mini_banner_text", _BANNER_DEFAULTS["mini_banner_text"])[:240],
        "image_url": _setting_value("mini_banner_image_url", "")[:500],
        "link_url": _setting_value("mini_banner_link_url", "")[:500],
        "button_text": _setting_value("mini_banner_button_text", _BANNER_DEFAULTS["mini_banner_button_text"])[:40],
        "width_percent": width_percent,
        "height_px": height_px,
        "image_fit": image_fit,
    }


def _valid_https_url(value: str) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Public page and user APIs
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/bootstrap")
async def bootstrap(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    telegram_id = int(user["id"])
    is_admin = DBX.is_admin(telegram_id)
    maintenance_active = bool(maintenance_enabled())
    row = DBX.get_customer_by_telegram(telegram_id)
    customer = dict(row) if row else None
    access_granted = bool(is_admin or ((customer and customer.get("status") == "ACTIVE") and not maintenance_active))
    contact_url = wallet_admin_contact_url()

    # البيانات العامة متاحة للتصفح حتى قبل التفعيل، بينما تبقى إجراءات الإيداع
    # والسحب محمية في مسارات API نفسها وفي واجهة التطبيق.
    stats = wallet_user_stats(telegram_id)
    route_items = wallet_network_routes()
    liquidity = await _ui_liquidity_snapshot()
    if access_granted:
        routes = await asyncio.gather(*[_public_network_route(dict(item)) for item in route_items])
    else:
        routes = []
        for item in route_items:
            route = {key: value for key, value in dict(item).items() if key != "provider"}
            route.setdefault("minimum", "")
            route.setdefault("system_fee", "")
            routes.append(route)

    base = {
        "app_name": APP_NAME,
        "presentation": runtime.get("presentation_config")(),
        "maintenance": {
            "enabled": maintenance_active,
            "reason": maintenance_reason() if maintenance_active else "",
            "admin_bypass": bool(is_admin),
        },
        "access": {
            "granted": access_granted,
            "status": "ADMIN" if is_admin else (customer.get("status") if customer else "NEW"),
            "contact_url": contact_url,
            "title": "محفظة USDT بخدمة تلقائية وآمنة",
            "description": (
                "تعمل الخدمة تلقائيًا وفوريًا مع محفظة Jaib، وتتوفر طرق دفع أخرى "
                "عبر التواصل المباشر مع الإدارة. يمكنك تصفح الخدمات، ويتطلب تنفيذ العمليات تفعيل الحساب."
            ),
        },
        "guidance": {
            "network_address_video_url": network_address_video_url(),
            "bot_usage_guide_url": bot_usage_guide_url(),
            "tutorial_buttons": [
                {
                    "id": int(item["id"]),
                    "name": str(item["name"]),
                    "url": str(item["url"]),
                    "icon": str(item["icon"]),
                    "custom_emoji_id": str(item["custom_emoji_id"] or ""),
                    "order": int(item["sort_order"]),
                }
                for item in list_tutorial_buttons(active_only=True)
            ],
            "withdraw_disclaimer": (
                "يجب أن يطابق عنوان الاستلام الشبكة المختارة تمامًا. إدخال عنوان خاطئ "
                "أو عنوان من شبكة مختلفة قد يؤدي إلى فقدان كامل للمبلغ، ولا تتحمل الإدارة "
                "مسؤولية العنوان أو الشبكة التي يدخلها المستخدم."
            ),
        },
        "banner": _banner_payload(),
        "user": {
            "telegram_id": telegram_id,
            "first_name": user.get("first_name") or "",
            "photo_url": user.get("photo_url") or "",
            "full_name": (customer or {}).get("full_name") or user.get("first_name") or str(telegram_id),
            "username": (customer or {}).get("telegram_username") or user.get("username") or "",
            "phone_primary": (customer or {}).get("phone_primary") or "",
            "phone_alt": (customer or {}).get("phone_alt") or "",
            "status": "ADMIN" if is_admin else ((customer or {}).get("status") or "NEW"),
            "is_admin": is_admin,
            "permissions": sorted(DBX.get_admin_permissions(telegram_id)) if is_admin else [],
        },
        "wallet": {
            "available": format_display_amount(stats["available"]),
            "locked": format_display_amount(stats["locked"]),
            "total": format_display_amount(stats["total"]),
        },
        "display": {"decimals": display_decimals()},
        "services": {
            "deposit_enabled": bool(wallet_deposit_enabled()),
            "withdraw_enabled": bool(wallet_withdraw_enabled()),
            "sales_enabled": bool(DBX.sales_enabled()),
            "auto_payout_enabled": bool(DBX.auto_payout_enabled()),
            "wallet_auto_withdraw_enabled": True,
        },
        "rate": format_yer_amount(effective_customer_rate(telegram_id, customer=row)),
        "payment": {
            "provider": f"{JAIB_DISPLAY_NAME} - Jaib",
            "automatic": True,
            "primary_phone": JAIB_PRIMARY_PHONE,
            "secondary_phone": JAIB_SECONDARY_PHONE,
            "secondary_beneficiary_name": JAIB_SECONDARY_BENEFICIARY_NAME,
            "minimum_yer": format_yer_amount(WALLET_MIN_DEPOSIT_YER),
            "third_party_policy": "لا نقبل التعامل مع طرف ثالث؛ يجب أن يكون التحويل من حسابك الشخصي فقط.",
            "liability_notice": "يخلي البوت مسؤوليته عن المبالغ المحولة من حسابات غير مطابقة أو عند إدخال رقم عملية خاطئ.",
            "other_payment_url": contact_url,
        },
        "liquidity": {
            "enabled": bool(public_liquidity_enabled()),
            "label": liquidity.get("label") or PUBLIC_LIQUIDITY_LABEL,
            "available": liquidity.get("available_display") or "0",
            "available_known": bool(liquidity.get("available_known")),
            "stale": bool(liquidity.get("stale")),
        },
        "withdrawal_policy": {
            "operation_limit_enabled": bool(withdrawal_operation_limit_enabled()),
            "operation_limit": format_display_amount(effective_withdrawal_ceiling()),
            "system_limit": format_display_amount(WALLET_MAX_WITHDRAW_USDT),
        },
        "networks": routes,
    }

    if not access_granted:
        base["onboarding"] = account_verification_public(get_account_verification(telegram_id))
        return base

    active_customer = _active_customer(user)
    base["user"]["full_name"] = active_customer.get("full_name") or base["user"]["full_name"]
    return base


@app.get("/api/me/liquidity")
async def current_liquidity(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _active_customer(user)
    snapshot = await _ui_liquidity_snapshot()
    return {
        "enabled": bool(public_liquidity_enabled()),
        "label": snapshot.get("label") or PUBLIC_LIQUIDITY_LABEL,
        "available": snapshot.get("available_display") or "0",
        "available_known": bool(snapshot.get("available_known")),
        "stale": bool(snapshot.get("stale")),
    }


@app.get("/api/onboarding")
def onboarding_state(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    telegram_id = int(user["id"])
    if DBX.is_admin(telegram_id):
        return {"status": "ADMIN", "granted": True}
    customer = DBX.get_customer_by_telegram(telegram_id)
    if customer and customer["status"] == "ACTIVE":
        return {"status": "APPROVED", "granted": True}
    return {"granted": False, "request": account_verification_public(get_account_verification(telegram_id))}


@app.post("/api/onboarding/start")
def onboarding_start(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    telegram_id = int(user["id"])
    customer = DBX.get_customer_by_telegram(telegram_id)
    if DBX.is_admin(telegram_id) or (customer and customer["status"] == "ACTIVE"):
        return {"ok": True, "granted": True, "request": {"status": "APPROVED"}}
    row = ensure_account_verification(
        SimpleNamespace(
            id=telegram_id,
            full_name=user.get("first_name") or str(telegram_id),
            first_name=user.get("first_name") or str(telegram_id),
            username=user.get("username") or "",
        )
    )
    return {"ok": True, "granted": False, "request": account_verification_public(row)}


@app.post("/api/onboarding/full-name")
def onboarding_full_name(
    request: OnboardingValueRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    try:
        row = save_verification_full_name(int(user["id"]), request.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "request": account_verification_public(row)}


@app.post("/api/onboarding/phone-primary")
def onboarding_primary_phone(
    request: OnboardingValueRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    try:
        row = save_verification_primary_phone(int(user["id"]), request.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "request": account_verification_public(row)}


@app.post("/api/onboarding/phone-alt")
def onboarding_alt_phone(
    request: OnboardingValueRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    try:
        row = save_verification_alt_phone(int(user["id"]), request.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "request": account_verification_public(row)}


@app.post("/api/onboarding/verify")
async def onboarding_verify_payment(
    request: OnboardingVerifyRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    try:
        return await verify_account_payment(int(user["id"]), request.reference)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/onboarding/submit")
async def onboarding_submit(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    bot = await _bot_context()
    try:
        row = await submit_account_verification(int(user["id"]), bot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await _close_bot(bot)
    return {
        "ok": True,
        "message": "تم تقديم طلبك. حسابك الآن قيد المراجعة.",
        "request": account_verification_public(row),
    }


@app.post("/api/onboarding/restart")
def onboarding_restart(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        row = restart_account_verification(int(user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "request": account_verification_public(row)}


@app.post("/api/access/request")
async def request_account_activation(
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    telegram_id = int(user["id"])
    if DBX.is_admin(telegram_id):
        return {"ok": True, "status": "ADMIN", "contact_url": wallet_admin_contact_url()}
    customer = ensure_pending_customer(
        SimpleNamespace(
            id=telegram_id,
            full_name=user.get("first_name") or str(telegram_id),
            first_name=user.get("first_name") or str(telegram_id),
            username=user.get("username") or "",
        )
    )
    if customer["status"] == "ACTIVE":
        return {"ok": True, "status": "ACTIVE", "contact_url": wallet_admin_contact_url()}
    DBX.audit(
        telegram_id,
        user.get("first_name") or "Mini App User",
        "REQUEST_ACCOUNT_ACTIVATION",
        "customer",
        customer["id"],
        "Mini App landing page",
    )
    background_tasks.add_task(
        _notify_admins,
        "<b>🔔 طلب تفعيل حساب من Mini App</b>\n\n"
        f"👤 {customer['full_name']}\n"
        f"🆔 <code>{telegram_id}</code>\n"
        f"المستخدم: @{user.get('username') or '—'}",
    )
    return {
        "ok": True,
        "status": customer["status"],
        "message": "تم تسجيل طلب التفعيل وإشعار الإدارة.",
        "contact_url": wallet_admin_contact_url(),
    }


@app.get("/api/me/stats")
def my_stats(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    customer = _active_customer(user)
    telegram_id = int(user["id"])
    stats = wallet_user_stats(telegram_id)
    operations = [_public_operation(item) for item in wallet_report_operations(telegram_id)[:20]]
    return {
        "app_name": APP_NAME,
        "presentation": runtime.get("presentation_config")(),
        "user": {
            "telegram_id": telegram_id,
            "telegram_name": user.get("first_name") or customer.get("full_name"),
            "full_name": customer.get("full_name") or "مدير النظام",
            "username": customer.get("telegram_username") or user.get("username"),
            "phone_primary": customer.get("phone_primary") or "",
            "phone_alt": customer.get("phone_alt") or "",
            "status": customer.get("status") or "ADMIN",
        },
        "stats": {
            "available": q2(stats["available"]),
            "locked": q2(stats["locked"]),
            "total": q2(stats["total"]),
            "purchase_count": stats["purchase_count"],
            "purchase_usdt": q2(stats["purchase_usdt"]),
            "purchase_yer": format_yer_amount(stats["purchase_yer"]),
            "withdraw_count": stats["withdraw_count"],
            "withdraw_usdt": q2(stats["withdraw_usdt"]),
            "referral_count": stats["referral_count"],
            "active_referral_count": stats["active_referral_count"],
            "referral_balance": q2(stats["referral_balance"]),
            "operation_count": stats["operation_count"],
            "successful_operations": stats["successful_operations"],
            "failed_operations": stats["failed_operations"],
            "success_rate": round(stats["success_rate"], 1),
            "withdrawal_ratio": round(stats["withdrawal_ratio"], 1),
            "first_used_at": stats["first_used_at"],
            "last_at": stats["last_at"],
        },
        "operations": operations,
    }


@app.get("/api/me/operations")
def my_operations(
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _active_customer(user)
    telegram_id = int(user["id"])
    items = list(wallet_report_operations(telegram_id))
    with DBX.connect() as con:
        pending_rows = con.execute(
            """
            SELECT COALESCE(updated_at, created_at) AS event_at, withdrawal_code,
                   amount_usdt, status, error_code, txid, network_code,
                   destination_address
            FROM wallet_withdrawals
            WHERE telegram_user_id=?
              AND status IN ('AUTO_PENDING','PROCESSING','SUBMITTED','STATUS_UNKNOWN','MANUAL_REVIEW')
            ORDER BY id DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        ).fetchall()
    items.extend(
        {
            "created_at": row["event_at"],
            "type": "WITHDRAWAL",
            "type_label": "سحب USDT",
            "reference": row["withdrawal_code"],
            "amount": row["amount_usdt"],
            "currency": "USDT",
            "status": row["status"],
            "successful": False,
            "failure_code": row["error_code"] or "",
            "txid": row["txid"] or "",
            "network_code": row["network_code"] or "",
            "destination_address": row["destination_address"] or "",
        }
        for row in pending_rows
    )
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": [_public_operation(item) for item in items[:limit]]}


@app.get("/api/me/referrals")
def my_referrals(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _active_customer(user)
    telegram_id = int(user["id"])
    summary = referral_summary(telegram_id)
    bot_username = str(DBX.get_setting("bot_username") or env("BOT_USERNAME", "")).strip().lstrip("@")
    link = f"https://t.me/{bot_username}?start=ref_{telegram_id}" if bot_username else ""
    return {
        "count": int(summary["count"]),
        "active_count": int(summary["active_count"]),
        "balance": q8(summary["balance"]),
        "link": link,
    }


@app.get("/api/me/deposits/prepare")
def prepare_deposit(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _active_customer(user, allow_admin=False)
    if not wallet_deposit_enabled():
        raise HTTPException(status_code=403, detail="الإيداع متوقف مؤقتًا")
    telegram_id = int(user["id"])
    draft = wallet_prepare_deposit(telegram_id)
    token = _sign_action(
        "deposit",
        telegram_id,
        {
            "exchange_rate_yer": draft["exchange_rate_yer"],
            "prepared_at": draft["prepared_at"],
        },
    )
    return {
        "session_token": token,
        "automatic_scan_available": runtime.get("jaib_scan_available")(),
        "exchange_rate_yer": draft["exchange_rate_yer"],
        "prepared_at": draft["prepared_at"],
        "minimum_yer": q2(WALLET_MIN_DEPOSIT_YER),
        "provider": f"{JAIB_DISPLAY_NAME} - Jaib",
        "primary_phone": JAIB_PRIMARY_PHONE,
        "secondary_phone": JAIB_SECONDARY_PHONE,
        "secondary_beneficiary_name": JAIB_SECONDARY_BENEFICIARY_NAME,
        "third_party_policy": "لا نقبل التعامل مع طرف ثالث؛ يجب أن يكون التحويل من حسابك الشخصي فقط.",
        "liability_notice": "يخلي البوت مسؤوليته عن أي مبلغ محول من حساب غير مطابق أو عند إدخال رقم عملية خاطئ.",
        "other_payment_url": wallet_admin_contact_url(),
    }


@app.post("/api/me/deposits/verify")
async def verify_deposit(
    request: DepositVerifyRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _active_customer(user, allow_admin=False)
    telegram_id = int(user["id"])
    reference = request.reference.strip()
    if not reference.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="رقم العملية غير صالح")

    deposit_id = int(request.deposit_id or 0)
    if deposit_id <= 0:
        if not request.session_token:
            raise HTTPException(status_code=400, detail="جلسة الإيداع مطلوبة")
        body = _decode_action(request.session_token, "deposit", telegram_id)
        _consume_action(body)
        try:
            deposit = wallet_create_deposit(
                telegram_id,
                exchange_rate_yer=wallet_decimal(body["payload"]["exchange_rate_yer"]),
            )
            deposit_id = int(deposit["id"])
        except Exception as exc:
            raise _safe_error(exc) from exc

    try:
        result = await wallet_verify_deposit_reference_core(
            deposit_id,
            telegram_id,
            reference,
        )
    except Exception as exc:
        raise _safe_error(exc) from exc

    result = dict(result)
    result["deposit_id"] = deposit_id
    if result["outcome"] == "COMPLETED":
        background_tasks.add_task(
            _notify_admins,
            "<b>✅ إيداع ناجح من Mini App</b>\n\n"
            f"🧾 <code>{result['deposit_code']}</code>\n"
            f"👤 {result.get('customer_name', telegram_id)}\n"
            f"💴 {format_yer_amount(result['fiat_amount_yer'])} ريال\n"
            f"💵 {result['usdt_amount']} USDT",
        )
    elif result["outcome"] == "PAYMENT_REVIEW":
        background_tasks.add_task(
            _notify_admins,
            "<b>⚠️ إيداع بانتظار المراجعة من Mini App</b>\n\n"
            f"🧾 <code>{result['deposit_code']}</code>\n"
            f"السبب: <code>{result.get('reason', '—')}</code>",
        )
    return result


@app.get("/api/me/withdrawals/networks")
async def withdrawal_networks(background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(current_user)):
    _active_customer(user, allow_admin=False)
    items=[]
    for route in wallet_network_routes():
        item=await _public_network_route(route)
        if route['provider']=='TON_HOT':
            item.update(minimum=DBX.get_setting('ton_min_usdt') or '1', system_fee='0', customer_fee='0')
        item['last_destination']=runtime.get('latest_destination')(int(user['id']),route['code'])
        items.append(item)
    available,_=wallet_balances(int(user['id']))
    return {'items':items,'available_balance':q8(available),'maximum':q8(effective_withdrawal_ceiling()),
            'maximum_enabled':withdrawal_operation_limit_enabled(), 'liquidity':{'enabled':False}}


@app.post("/api/me/withdrawals/quote")
async def quote_withdrawal(request: WithdrawalQuoteRequest, user: dict[str, Any] = Depends(current_user)):
    _active_customer(user, allow_admin=False)
    try:
        quote=await asyncio.to_thread(runtime.get('withdrawal_quote_shared'),int(user['id']),request.amount,
                                      request.network_code.upper(),request.address,request.memo)
    except Exception as exc: raise _safe_error(exc) from exc
    return dict(quote,quote_token=_sign_action('withdrawal',int(user['id']),quote))


@app.post("/api/me/withdrawals")
async def create_withdrawal(request: WithdrawalCreateRequest, background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(current_user)):
    _active_customer(user, allow_admin=False)
    body=_decode_action(request.quote_token,'withdrawal',int(user['id']))
    try:
        row=await asyncio.to_thread(runtime.get('confirm_withdrawal_shared'),int(user['id']),body['payload'],body['nonce'])
    except Exception as exc: raise _safe_error(exc) from exc
    background_tasks.add_task(_submit_withdrawal_background,int(row['id']),False)
    return {'id':row['id'],'reference':row['withdrawal_code'],'status':row['status'],
            'amount':row['amount_usdt'],'customer_fee':row['customer_fee_usdt'],
            'total_debit':q8(wallet_decimal(row['amount_usdt'])+wallet_decimal(row['customer_fee_usdt'])),
            'network_code':row['network_code'],'network_label':row['network_label'],
            'message':'تم حفظ الطلب؛ الضغط المتكرر يعيد نفس الطلب'}


@app.get("/api/me/withdrawals/{withdrawal_id}")
def withdrawal_status(
    withdrawal_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _active_customer(user, allow_admin=False)
    row = wallet_get_withdrawal(withdrawal_id)
    if not row or int(row["telegram_user_id"]) != int(user["id"]):
        raise HTTPException(status_code=404, detail="عملية السحب غير موجودة")
    data = _row_dict(row)
    return {
        key: data.get(key)
        for key in (
            "id",
            "withdrawal_code",
            "amount_usdt",
            "network_code",
            "network_label",
            "destination_address",
            "provider",
            "status",
            "provider_withdraw_id",
            "txid",
            "network_fee_amount",
            "error_code",
            "error_message",
            "created_at",
            "submitted_at",
            "completed_at",
            "updated_at",
        )
    }


@app.get("/api/me/report")
def my_report(user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    _active_customer(user)
    path = generate_user_pdf_report(int(user["id"]))
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.post("/api/me/report/send")
async def send_my_report(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _active_customer(user)
    telegram_id = int(user["id"])
    path: Path | None = None
    bot: Any | None = None
    try:
        path = Path(await asyncio.to_thread(generate_user_pdf_report, telegram_id))
        bot = await _bot_context()
        with path.open("rb") as document:
            await bot.send_document(
                chat_id=telegram_id,
                document=document,
                filename=path.name,
                caption="📄 تقريرك المالي من محفظة USDT",
            )
    except Exception as exc:
        web_log.exception("Failed to send user PDF report telegram_id=%s", telegram_id)
        raise HTTPException(
            status_code=502,
            detail=(
                "تعذر إرسال التقرير إلى محادثة البوت. "
                "افتح البوت واضغط /start ثم حاول مرة أخرى."
            ),
        ) from exc
    finally:
        if bot is not None:
            await _close_bot(bot)
        if path is not None:
            path.unlink(missing_ok=True)
    return {
        "ok": True,
        "message": "تم إنشاء التقرير وإرساله إلى محادثتك مع البوت.",
    }


# ---------------------------------------------------------------------------
# Admin APIs
# ---------------------------------------------------------------------------

@app.get("/api/admin/runtime-health")
def admin_runtime_health(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "logs_manage")
    with DBX.connect() as con:
        instances = [dict(row) for row in con.execute(
            "SELECT * FROM runtime_instances ORDER BY last_seen_at DESC LIMIT 20"
        ).fetchall()]
        active_withdrawals = [dict(row) for row in con.execute(
            """
            SELECT withdrawal_code, status, provider, amount_usdt, created_at, updated_at,
                   submission_started_at, provider_withdraw_id, txid, monitor_error_count,
                   last_monitor_error, next_check_at
            FROM wallet_withdrawals
            WHERE status IN ('AUTO_PENDING','PENDING_REVIEW','PROCESSING','SUBMITTED','STATUS_UNKNOWN','MANUAL_REVIEW')
            ORDER BY id DESC LIMIT 50
            """
        ).fetchall()]

    now = runtime.get("utc_now")()
    for item in instances:
        try:
            age = max(0, int((now - datetime.strptime(str(item.get("last_seen_at") or ""), "%Y-%m-%d %H:%M:%S")).total_seconds()))
        except Exception:
            age = -1
        item["seconds_since_seen"] = age
        item["alive"] = bool(not item.get("stopped_at") and 0 <= age <= 150)

    live_instances = [item for item in instances if item.get("alive")]
    def distinct(field: str) -> list[str]:
        return sorted({str(item.get(field) or "") for item in live_instances if str(item.get(field) or "")})

    db_paths = distinct("db_path")
    env_paths = distinct("env_path")
    key_fingerprints = distinct("provider_key_fingerprint")
    egress_ips = distinct("egress_ip")
    warnings: list[str] = []
    if len(db_paths) > 1:
        warnings.append("الخدمات الحية تستخدم قواعد بيانات مختلفة")
    if len(env_paths) > 1:
        warnings.append("الخدمات الحية تستخدم ملفات بيئة مختلفة")
    if len(key_fingerprints) > 1:
        warnings.append("الخدمات الحية تستخدم مفاتيح Binance مختلفة")
    if len(egress_ips) > 1:
        warnings.append("الخدمات الحية تخرج إلى Binance من عناوين IP مختلفة")

    return {
        "service": "MINI_APP",
        "pid": os.getpid(),
        "database_path": str(DBX.path),
        "binance": binance_auth_state(),
        "jaib": jaib_health_state(),
        "consistency": {
            "healthy": not warnings,
            "warnings": warnings,
            "live_instance_count": len(live_instances),
            "database_paths": db_paths,
            "environment_paths": env_paths,
            "binance_key_fingerprints": key_fingerprints,
            "egress_ips": egress_ips,
        },
        "instances": instances,
        "active_withdrawals": active_withdrawals,
    }


@app.post("/api/admin/jaib-health/check")
async def admin_check_jaib_health(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "wallet_settings")
    return await asyncio.to_thread(jaib_health_check,force=True)


@app.get("/api/admin/dashboard")
def admin_dashboard(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "stats_view")
    stats = DBX.stats()
    wallet = wallet_system_stats()
    with DBX.connect() as con:
        reviews = con.execute(
            "SELECT COUNT(*) c FROM wallet_deposits WHERE status='PAYMENT_REVIEW'"
        ).fetchone()["c"]
        withdrawals = con.execute(
            "SELECT COUNT(*) c FROM wallet_withdrawals "
            "WHERE status IN ('AUTO_PENDING','PROCESSING','SUBMITTED','STATUS_UNKNOWN','MANUAL_REVIEW')"
        ).fetchone()["c"]
        final_failures = con.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM wallet_deposits WHERE status='REJECTED') + "
            "(SELECT COUNT(*) FROM wallet_withdrawals WHERE status IN ('FAILED_RELEASED','EXPIRED_RELEASED')) c"
        ).fetchone()["c"]
    return {
        "customers": stats,
        "wallet": {key: str(value) for key, value in wallet.items()},
        "pending_deposit_reviews": int(reviews or 0),
        "pending_withdrawals": int(withdrawals or 0),
        "final_failures": int(final_failures or 0),
    }


@app.get("/api/admin/users")
def admin_users(
    search: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "customers_view")
    rows = DBX.search_customers(search, limit) if search.strip() else DBX.list_customers(limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        if data.get("telegram_id"):
            available, locked = wallet_balances(int(data["telegram_id"]))
            data["available_balance"] = q8(available)
            data["locked_balance"] = q8(locked)
            data["effective_exchange_rate_yer"] = q2(
                effective_customer_rate(int(data["telegram_id"]), customer=row)
            )
        else:
            data["available_balance"] = "0"
            data["locked_balance"] = "0"
        items.append(data)
    return {"items": items}


@app.get("/api/admin/users/{telegram_id}")
def admin_user_detail(
    telegram_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "customers_view")
    row = DBX.get_customer_by_telegram(telegram_id)
    if not row:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    available, locked = wallet_balances(telegram_id)
    return {
        "customer": {
            **dict(row),
            "effective_exchange_rate_yer": q2(effective_customer_rate(telegram_id, customer=row)),
        },
        "wallet": {"available": q8(available), "locked": q8(locked)},
        "stats": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in wallet_user_stats(telegram_id).items()
            if key != "recent_operations"
        },
        "operations": [_public_operation(item) for item in wallet_report_operations(telegram_id)[:50]],
    }


@app.patch("/api/admin/users/{telegram_id}/status")
async def admin_set_user_status(
    telegram_id: int,
    request: CustomerStatusRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    # Keep direct Python callers from V18 compatible with the old third
    # positional ``user`` argument while FastAPI still injects BackgroundTasks.
    if isinstance(background_tasks, dict) and not isinstance(user, dict):
        user, background_tasks = background_tasks, None
    admin_id = _require_admin(user, "customers_status")
    row = DBX.get_customer_by_telegram(telegram_id)
    if not row:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    previous_status = str(row["status"] or "").upper()
    DBX.update_customer(int(row["id"]), status=request.status)
    reward = Decimal("0")
    if request.status == "ACTIVE":
        # Always retry an uncompleted pending referral. The reward function is
        # atomic and idempotent, so repeated activation requests cannot duplicate it.
        _upsert_registration(telegram_id, status="ACTIVE", activated_at=now_str())
        bot = await _bot_context()
        try:
            reward = await complete_referral_after_activation(
                SimpleNamespace(bot=bot),
                telegram_id,
            )
        finally:
            await _close_bot(bot)
    if previous_status != request.status:
        if request.status == "ACTIVE":
            decision_text = (
                "<b>تمت الموافقة على طلبك ✅</b>\n\n"
                "تم تفعيل حسابك، ويمكنك الآن استخدام خدمات البوت وMini App."
            )
            notification_type = "MINI_APP_ACCOUNT_APPROVED"
        else:
            decision_text = (
                "<b>تم رفض طلبك ❌</b>\n\n"
                "الحساب غير نشط حاليًا. يمكنك التواصل مع الإدارة لمعرفة السبب."
            )
            notification_type = "MINI_APP_ACCOUNT_REJECTED"
        wallet_enqueue_user_notification(
            event_key=(
                f"MINI_APP_ACCOUNT_STATUS:{telegram_id}:{previous_status}:"
                f"{request.status}:{row['updated_at']}"
            ),
            telegram_user_id=telegram_id,
            notification_type=notification_type,
            text=decision_text,
        )
        if background_tasks is not None:
            background_tasks.add_task(_flush_wallet_notifications_background, telegram_id)
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "SET_CUSTOMER_STATUS",
        "customer",
        row["id"],
        f"{previous_status}->{request.status}; referral_reward={q8(reward)}",
    )
    return {"ok": True, "status": request.status, "referral_reward": q8(reward)}


@app.patch("/api/admin/users/{telegram_id}/trusted")
def admin_set_user_trusted(
    telegram_id: int,
    request: CustomerTrustedRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "customers_edit")
    row = DBX.get_customer_by_telegram(telegram_id)
    if not row:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    with DBX.connect() as con:
        con.execute(
            "UPDATE customers SET is_trusted=?, updated_at=? WHERE id=?",
            (1 if request.trusted else 0, now_str(), int(row["id"])),
        )
        con.commit()
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "SET_CUSTOMER_TRUST",
        "customer",
        int(row["id"]),
        f"trusted={1 if request.trusted else 0}",
    )
    return {"ok": True, "trusted": bool(request.trusted)}


@app.patch("/api/admin/users/{telegram_id}/exchange-rate")
def admin_set_user_exchange_rate(
    telegram_id: int,
    request: CustomerExchangeRateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "customers_edit")
    row = DBX.get_customer_by_telegram(telegram_id)
    if not row:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    try:
        updated = set_customer_custom_rate(int(row["id"]), request.rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    special = customer_custom_rate(updated)
    effective = effective_customer_rate(telegram_id, customer=updated)
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "SET_CUSTOMER_EXCHANGE_RATE" if special is not None else "CLEAR_CUSTOMER_EXCHANGE_RATE",
        "customer",
        int(row["id"]),
        f"rate={q2(special) if special is not None else 'GLOBAL'}",
    )
    return {
        "ok": True,
        "custom_exchange_rate_yer": q2(special) if special is not None else None,
        "effective_exchange_rate_yer": q2(effective),
    }


@app.post("/api/admin/users/{telegram_id}/balance-adjustment")
async def admin_balance_adjustment(
    telegram_id: int,
    request: BalanceAdjustmentRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_adjust")
    try:
        wallet = wallet_admin_adjust(
            telegram_id,
            request.amount,
            admin_id=admin_id,
            reason=request.reason,
        )
    except Exception as exc:
        raise _safe_error(exc) from exc
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "ADJUST_WALLET_BALANCE",
        "wallet",
        telegram_id,
        f"amount={request.amount}; reason={request.reason}",
    )
    background_tasks.add_task(_flush_wallet_notifications_background, telegram_id)
    return {
        "ok": True,
        "available_balance": wallet["available_balance"],
        "locked_balance": wallet["locked_balance"],
    }


@app.get("/api/admin/deposits")
def admin_deposits(
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "wallet_transactions")
    with DBX.connect() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM wallet_deposits WHERE status=? ORDER BY id DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM wallet_deposits ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/admin/deposits/{deposit_id}/approve")
def admin_approve_deposit(
    deposit_id: int,
    request: DepositDecisionRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if isinstance(background_tasks, dict) and not isinstance(user, dict):
        user, background_tasks = background_tasks, None
    admin_id = _require_admin(user, "orders_manage")
    try:
        row = wallet_credit_deposit(deposit_id, admin_id=admin_id, notes=request.reason)
    except Exception as exc:
        raise _safe_error(exc) from exc
    available, _ = wallet_balances(int(row["telegram_user_id"]))
    wallet_enqueue_user_notification(
        event_key=f"MINI_APP_DEPOSIT_APPROVED:{deposit_id}",
        telegram_user_id=int(row["telegram_user_id"]),
        notification_type="MINI_APP_DEPOSIT_APPROVED",
        text=(
            "<b>تمت الموافقة على طلبك ✅</b>\n\n"
            f"تمت إضافة <b>{format_display_amount(row['usdt_amount'])} USDT</b> إلى رصيدك.\n"
            f"الرصيد المتاح لديك: <b>{format_display_amount(available)} USDT</b>\n"
            f"المرجع: <code>{html_escape(str(row['deposit_code']))}</code>"
        ),
    )
    if background_tasks is not None:
        background_tasks.add_task(_flush_wallet_notifications_background, int(row["telegram_user_id"]))
    DBX.audit(admin_id, "Mini App Admin", "APPROVE_DEPOSIT", "wallet_deposit", deposit_id, request.reason)
    return {"ok": True, "item": dict(row)}


@app.post("/api/admin/deposits/{deposit_id}/reject")
def admin_reject_deposit(
    deposit_id: int,
    request: DepositDecisionRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if isinstance(background_tasks, dict) and not isinstance(user, dict):
        user, background_tasks = background_tasks, None
    admin_id = _require_admin(user, "orders_manage")
    with DBX.connect() as con:
        row = con.execute("SELECT * FROM wallet_deposits WHERE id=?", (deposit_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="الإيداع غير موجود")
        if row["status"] == "COMPLETED":
            raise HTTPException(status_code=409, detail="لا يمكن رفض إيداع مكتمل")
        con.execute(
            "UPDATE wallet_deposits SET status='REJECTED', review_reason='ADMIN_REJECTED', "
            "error_message=?, updated_at=? WHERE id=?",
            (request.reason, now_str(), deposit_id),
        )
        con.commit()
    wallet_enqueue_user_notification(
        event_key=f"MINI_APP_DEPOSIT_REJECTED:{deposit_id}",
        telegram_user_id=int(row["telegram_user_id"]),
        notification_type="MINI_APP_DEPOSIT_REJECTED",
        text=(
            "<b>تم رفض طلبك ❌</b>\n\n"
            f"طلب الإيداع: <code>{html_escape(str(row['deposit_code']))}</code>\n"
            f"السبب: {html_escape(request.reason)}"
        ),
    )
    if background_tasks is not None:
        background_tasks.add_task(_flush_wallet_notifications_background, int(row["telegram_user_id"]))
    DBX.audit(admin_id, "Mini App Admin", "REJECT_DEPOSIT", "wallet_deposit", deposit_id, request.reason)
    return {"ok": True, "status": "REJECTED"}


@app.get("/api/admin/withdrawals")
def admin_withdrawals(
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "wallet_transactions")
    with DBX.connect() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM wallet_withdrawals WHERE status=? ORDER BY id DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM wallet_withdrawals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/admin/withdrawals/{withdrawal_id}/complete")
def admin_complete_withdrawal(
    withdrawal_id: int,
    request: WithdrawalCompleteRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_retry")
    row = wallet_get_withdrawal(withdrawal_id)
    if not row:
        raise HTTPException(status_code=404, detail="السحب غير موجود")
    if row["provider"] == "TON_HOT":
        raise HTTPException(409,detail="TON يُكمل فقط بعد إثبات وصول USDT على الشبكة؛ الإكمال اليدوي برقم مكتوب غير مسموح.")
    if row["status"] not in {"PENDING_REVIEW", "RESERVED", "FAILED_RETRYABLE", "SUBMITTED", "STATUS_UNKNOWN", "MANUAL_REVIEW"}:
        raise HTTPException(status_code=409, detail="حالة السحب لا تسمح بالإكمال اليدوي")
    try:
        completed = wallet_complete_withdrawal(
            withdrawal_id,
            request.txid.strip(),
            {"source": "MINI_APP_ADMIN", "note": request.note, "admin_id": admin_id},
        )
    except Exception as exc:
        raise _safe_error(exc) from exc
    item = dict(completed)
    background_tasks.add_task(_notify_withdrawal_completed, item)
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "COMPLETE_WITHDRAWAL_MANUALLY",
        "wallet_withdrawal",
        withdrawal_id,
        f"txid={request.txid}; note={request.note}",
    )
    return {"ok": True, "item": item}


@app.post("/api/admin/withdrawals/{withdrawal_id}/retry")
def admin_retry_withdrawal(
    withdrawal_id: int,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_retry")
    row = wallet_get_withdrawal(withdrawal_id)
    if not row:
        raise HTTPException(status_code=404, detail="السحب غير موجود")
    if row["status"] not in {"RESERVED", "FAILED_RETRYABLE", "MANUAL_REVIEW"}:
        raise HTTPException(status_code=409, detail="حالة السحب لا تسمح بإعادة المحاولة")
    background_tasks.add_task(_submit_withdrawal_background, withdrawal_id, True)
    DBX.audit(admin_id, "Mini App Admin", "RETRY_WITHDRAWAL", "wallet_withdrawal", withdrawal_id)
    return {"ok": True, "status": "QUEUED"}


@app.post("/api/admin/withdrawals/{withdrawal_id}/release")
def admin_release_withdrawal(
    withdrawal_id: int,
    request: WithdrawalReleaseRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if isinstance(background_tasks, dict) and not isinstance(user, dict):
        user, background_tasks = background_tasks, None
    admin_id = _require_admin(user, "wallet_retry")
    current = wallet_get_withdrawal(withdrawal_id)
    if not current:
        raise HTTPException(status_code=404, detail="السحب غير موجود")
    has_provider_reference = bool(current["provider_withdraw_id"] or current["txid"])
    safe_status = current["status"] in {"PENDING_REVIEW", "RESERVED", "FAILED_RETRYABLE"} or (
        current["status"] == "MANUAL_REVIEW" and not has_provider_reference
    )
    if not safe_status:
        raise HTTPException(status_code=409, detail="لا يمكن إعادة الرصيد بعد بدء الإرسال أو ظهور معرف مزود. تحقق من الشبكة أولًا")
    try:
        row = wallet_release_withdrawal(
            withdrawal_id,
            error_code="ADMIN_RELEASE",
            error_message=request.reason,
            admin_id=admin_id,
        )
    except Exception as exc:
        raise _safe_error(exc) from exc
    wallet_enqueue_user_notification(
        event_key=f"MINI_APP_WITHDRAWAL_REJECTED:{withdrawal_id}",
        telegram_user_id=int(row["telegram_user_id"]),
        notification_type="MINI_APP_WITHDRAWAL_REJECTED",
        text=(
            "<b>تم رفض طلبك ❌</b>\n\n"
            f"طلب السحب: <code>{html_escape(str(row['withdrawal_code']))}</code>\n"
            "تمت إعادة المبلغ المحجوز كاملًا إلى رصيدك المتاح.\n"
            f"السبب: {html_escape(request.reason)}"
        ),
    )
    if background_tasks is not None:
        background_tasks.add_task(_flush_wallet_notifications_background, int(row["telegram_user_id"]))
    DBX.audit(admin_id, "Mini App Admin", "RELEASE_WITHDRAWAL", "wallet_withdrawal", withdrawal_id, request.reason)
    return {"ok": True, "item": dict(row)}


_ALLOWED_SETTINGS = {
    "usdt_rate_yer",
    "sales_enabled",
    "auto_payout_enabled",
    "wallet_deposit_enabled",
    "wallet_withdraw_enabled",
    "display_decimals",
    "public_liquidity_enabled",
    "liquidity_alert_enabled",
    "liquidity_alert_threshold_usdt",
    "liquidity_alert_recovery_usdt",
    "liquidity_alert_cooldown_minutes",
    "public_liquidity_reserve_usdt",
    "withdrawal_operation_limit_enabled",
    "withdrawal_operation_limit_usdt",
    "notification_channel_id",
    "technical_channel_id",
    "backup_mode",
    "backup_interval_seconds",
    "backup_max_copies",
    "backup_channel_id",
    "mini_banner_enabled",
    "mini_banner_animation_enabled",
    "mini_banner_badge",
    "mini_banner_title",
    "mini_banner_text",
    "mini_banner_image_url",
    "mini_banner_link_url",
    "mini_banner_button_text",
    "mini_banner_width_percent",
    "mini_banner_height_px",
    "mini_banner_image_fit",
    "account_verification_channel_id",
    "account_verification_recipient_phone",
    "account_verification_recipient_name",
    "network_address_video_url",
    "bot_usage_guide_url",
    "maintenance_enabled",
    "maintenance_reason",
    "account_approval_mode",
    "account_auto_approval_delay_seconds",
    "account_verification_max_yer",
    "unified_notification_channel_id",
    "channel_users_id",
    "channel_admin_id",
    "channel_backup_id",
}


@app.post("/api/admin/customers")
def admin_create_customer(
    request: CustomerCreateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "customers_add")
    if DBX.get_customer_by_any_phone(request.phone_primary):
        raise HTTPException(status_code=409, detail="رقم الهاتف مستخدم مسبقًا")
    if request.phone_alt and DBX.get_customer_by_any_phone(request.phone_alt):
        raise HTTPException(status_code=409, detail="الرقم البديل مستخدم مسبقًا")
    if request.telegram_id and DBX.get_customer_by_telegram(request.telegram_id):
        raise HTTPException(status_code=409, detail="Telegram ID مرتبط بعميل آخر")
    try:
        customer_id = DBX.create_customer(
            request.full_name,
            request.phone_primary,
            request.phone_alt,
            request.telegram_id,
            request.telegram_username,
            request.notes,
            admin_id,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="بيانات العميل مستخدمة مسبقًا") from exc
    DBX.audit(admin_id, "Mini App Admin", "CREATE_CUSTOMER", "customer", customer_id)
    return {"ok": True, "customer_id": customer_id}


@app.patch("/api/admin/customers/{customer_id}")
def admin_update_customer(
    customer_id: int,
    request: CustomerUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "customers_edit")
    current = DBX.get_customer(customer_id)
    if not current:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    values = request.model_dump(exclude_unset=True)
    if "phone_primary" in values:
        duplicate = DBX.get_customer_by_any_phone(values["phone_primary"], customer_id)
        if duplicate:
            raise HTTPException(status_code=409, detail="رقم الهاتف مستخدم مسبقًا")
    if values.get("phone_alt"):
        duplicate = DBX.get_customer_by_any_phone(values["phone_alt"], customer_id)
        if duplicate:
            raise HTTPException(status_code=409, detail="الرقم البديل مستخدم مسبقًا")
    if values.get("telegram_id"):
        duplicate = DBX.get_customer_by_telegram(int(values["telegram_id"]))
        if duplicate and int(duplicate["id"]) != int(customer_id):
            raise HTTPException(status_code=409, detail="Telegram ID مرتبط بعميل آخر")
    if values:
        DBX.update_customer(customer_id, **values)
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_CUSTOMER", "customer", customer_id, json.dumps(values, ensure_ascii=False))
    return {"ok": True}


@app.delete("/api/admin/customers/{customer_id}")
def admin_delete_customer(
    customer_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "customers_delete")
    current = DBX.get_customer(customer_id)
    if not current:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    deleted = DBX.delete_customer(customer_id)
    DBX.audit(
        admin_id,
        "Mini App Admin",
        "DELETE_CUSTOMER_PERMANENT",
        "system",
        None,
        f"purged_records={sum(deleted.values())}",
    )
    return {"ok": True, "deleted_records": sum(deleted.values())}


@app.get("/api/admin/admins")
def admin_list_admins(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "admins_manage")
    items = [{
        "telegram_id": ADMIN_ID,
        "display_name": "المدير الرئيسي",
        "username": "",
        "status": "ACTIVE",
        "permissions": sorted(set(ALL_ADMIN_PERMISSIONS) | {"admins_manage"}),
        "is_primary": True,
    }]
    for row in DBX.list_admins():
        item = dict(row)
        item["permissions"] = sorted(DBX.get_admin_permissions(int(row["telegram_id"])))
        item["is_primary"] = False
        item.pop("permissions_json", None)
        items.append(item)
    return {
        "items": items,
        "permission_labels": ADMIN_PERMISSION_LABELS,
    }


@app.post("/api/admin/admins")
def admin_create_admin(
    request: AdminCreateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "admins_manage")
    if int(request.telegram_id) == int(ADMIN_ID) or DBX.get_admin(request.telegram_id):
        raise HTTPException(status_code=409, detail="المشرف موجود مسبقًا")
    permissions = set(request.permissions).intersection(ALL_ADMIN_PERMISSIONS)
    DBX.create_admin(request.telegram_id, request.display_name, request.username, permissions, admin_id)
    DBX.audit(admin_id, "Mini App Admin", "CREATE_ADMIN", "admin", request.telegram_id, ",".join(sorted(permissions)))
    return {"ok": True}


@app.patch("/api/admin/admins/{telegram_id}")
def admin_update_admin(
    telegram_id: int,
    request: AdminUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "admins_manage")
    if int(telegram_id) == int(ADMIN_ID):
        raise HTTPException(status_code=400, detail="لا يمكن تعديل المدير الرئيسي")
    if not DBX.get_admin(telegram_id):
        raise HTTPException(status_code=404, detail="المشرف غير موجود")
    if request.status is not None:
        DBX.set_admin_status(telegram_id, request.status)
    if request.permissions is not None:
        DBX.update_admin_permissions(telegram_id, set(request.permissions).intersection(ALL_ADMIN_PERMISSIONS))
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_ADMIN", "admin", telegram_id, request.model_dump_json())
    return {"ok": True}


@app.delete("/api/admin/admins/{telegram_id}")
def admin_delete_admin(
    telegram_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "admins_manage")
    if int(telegram_id) == int(ADMIN_ID):
        raise HTTPException(status_code=400, detail="لا يمكن حذف المدير الرئيسي")
    DBX.delete_admin(telegram_id)
    DBX.audit(admin_id, "Mini App Admin", "DELETE_ADMIN", "admin", telegram_id)
    return {"ok": True}


@app.get("/api/admin/required-channels")
def admin_required_channels(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "orders_manage")
    return {"items": [dict(row) for row in list_required_channels(False)]}


@app.post("/api/admin/required-channels")
def admin_save_required_channel(
    request: RequiredChannelRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "orders_manage")
    save_required_channel(
        request.chat_id.strip(),
        request.username.strip().lstrip("@"),
        request.title.strip(),
        request.invite_link.strip(),
        admin_id,
    )
    DBX.audit(admin_id, "Mini App Admin", "SAVE_REQUIRED_CHANNEL", "channel", request.chat_id)
    return {"ok": True}


@app.delete("/api/admin/required-channels/{channel_id}")
def admin_delete_required_channel(
    channel_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "orders_manage")
    delete_required_channel(channel_id)
    DBX.audit(admin_id, "Mini App Admin", "DELETE_REQUIRED_CHANNEL", "channel", channel_id)
    return {"ok": True}


@app.get("/api/admin/security-log")
def admin_security_log(
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "security_view")
    return {"items": [dict(row) for row in DBX.list_security_logs(limit)]}


@app.get("/api/admin/export/customers.csv")
def admin_export_customers(user: dict[str, Any] = Depends(current_user)) -> StreamingResponse:
    _require_admin(user, "backup_export")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "full_name", "phone_primary", "phone_alt", "telegram_id", "telegram_username", "status", "available_usdt", "locked_usdt", "created_at"])
    for row in DBX.list_customers(100000):
        available = locked = Decimal("0")
        if row["telegram_id"]:
            available, locked = wallet_balances(int(row["telegram_id"]))
        writer.writerow([row["id"], row["full_name"], row["phone_primary"], row["phone_alt"], row["telegram_id"], row["telegram_username"], row["status"], q8(available), q8(locked), row["created_at"]])
    payload = ("\ufeff" + output.getvalue()).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@app.post("/api/admin/backups/restore")
def admin_restore_backup(
    request: BackupRestoreRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "backup_import")
    safety = create_backup_file()
    try:
        DBX.import_snapshot(request.payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"فشل الاستعادة: {type(exc).__name__}") from exc
    DBX.audit(admin_id, "Mini App Admin", "RESTORE_BACKUP", "backup", safety.name, "Mini App JSON restore")
    return {"ok": True, "safety_backup": safety.name}


@app.get("/api/admin/logs")
def admin_logs(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "logs_manage")
    try:
        size = LOG_PATH.stat().st_size
    except OSError:
        size = 0
    return {
        "path": LOG_PATH.name,
        "size": size,
        "level": current_log_level(),
        "debug": bool(debug_mode_enabled()),
        "max_bytes": current_log_max_bytes(),
        "backup_count": current_log_backup_count(),
    }


@app.get("/api/admin/logs/download")
def admin_download_logs(user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    _require_admin(user, "logs_manage")
    flush_log_handlers()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    return FileResponse(LOG_PATH, media_type="text/plain; charset=utf-8", filename=LOG_PATH.name)


@app.post("/api/admin/logs/truncate")
def admin_truncate_logs(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    admin_id = _require_admin(user, "logs_manage")
    truncate_active_log()
    DBX.audit(admin_id, "Mini App Admin", "TRUNCATE_LOG", "log", LOG_PATH.name)
    return {"ok": True}


@app.patch("/api/admin/logs")
def admin_update_logs(
    request: LogSettingsRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "logs_manage")
    DBX.set_setting("log_level", request.level)
    DBX.set_setting("debug_mode", "1" if request.debug else "0")
    DBX.set_setting("log_max_bytes", str(request.max_bytes))
    DBX.set_setting("log_backup_count", str(request.backup_count))
    configure_logging(level=request.level, debug=request.debug, max_bytes=request.max_bytes, backup_count=request.backup_count)
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_LOG_SETTINGS", "log", LOG_PATH.name)
    return {"ok": True, "note": "تُطبق الإعدادات على خادم الويب فورًا، وعلى عملية البوت بعد إعادة التشغيل إذا كان يعمل كعملية منفصلة."}


@app.get("/api/admin/settings")
def admin_settings(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user)
    values: dict[str, Any] = {}
    runtime_defaults = {
        "display_decimals": str(display_decimals()),
        "liquidity_alert_enabled": "1" if liquidity_alert_enabled() else "0",
        "liquidity_alert_threshold_usdt": q8(liquidity_alert_threshold()),
        "liquidity_alert_recovery_usdt": q8(liquidity_alert_recovery()),
        "liquidity_alert_cooldown_minutes": str(liquidity_alert_cooldown_minutes()),
        "maintenance_enabled": "1" if maintenance_enabled() else "0",
        "maintenance_reason": maintenance_reason(),
        "account_approval_mode": account_approval_mode(),
        "account_auto_approval_delay_seconds": str(account_auto_approval_delay_seconds()),
        "account_verification_max_yer": str(account_verification_max_yer()),
        "unified_notification_channel_id": str(unified_notification_channel_id() or ""),
        "channel_users_id": str(users_channel_id() or ""),
        "channel_admin_id": str(admin_channel_id() or ""),
        "channel_backup_id": str(project_backup_channel_id() or ""),
    }
    onboarding_defaults = {
        "account_verification_channel_id": str(runtime.get("ACCOUNT_VERIFICATION_CHANNEL_ID") or ""),
        "account_verification_recipient_phone": verification_recipient_phone(),
        "account_verification_recipient_name": verification_recipient_name(),
        "network_address_video_url": network_address_video_url(),
        "bot_usage_guide_url": bot_usage_guide_url(),
    }
    for key in sorted(_ALLOWED_SETTINGS):
        value = DBX.get_setting(key)
        if value is None and key in _BANNER_DEFAULTS:
            value = _BANNER_DEFAULTS[key]
        elif value is None and key in onboarding_defaults:
            value = onboarding_defaults[key]
        elif value is None and key in runtime_defaults:
            value = runtime_defaults[key]
        values[key] = value
    return {
        "values": values,
        "permissions": {
            "settings_rate": DBX.has_permission(int(user["id"]), "settings_rate"),
            "settings_sales": DBX.has_permission(int(user["id"]), "settings_sales"),
            "settings_payout": DBX.has_permission(int(user["id"]), "settings_payout"),
            "wallet_settings": DBX.has_permission(int(user["id"]), "wallet_settings"),
        },
    }


@app.patch("/api/admin/settings")
def admin_update_settings(
    request: SettingsRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user)
    unknown = set(request.values) - _ALLOWED_SETTINGS
    if unknown:
        raise HTTPException(status_code=400, detail=f"إعدادات غير مسموحة: {', '.join(sorted(unknown))}")
    for key, value in request.values.items():
        if key == "usdt_rate_yer":
            if not DBX.has_permission(admin_id, "settings_rate"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية تعديل السعر")
            decimal_value = wallet_decimal(value)
            if decimal_value <= 0:
                raise HTTPException(status_code=400, detail="السعر يجب أن يكون أكبر من صفر")
            DBX.set_setting(key, q2(decimal_value))
        elif key == "sales_enabled":
            if not DBX.has_permission(admin_id, "settings_sales"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية تشغيل البيع")
            DBX.set_sales_enabled(bool(value))
        elif key == "auto_payout_enabled":
            if not DBX.has_permission(admin_id, "settings_payout"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية الصرف")
            DBX.set_auto_payout_enabled(bool(value))
        elif key == "wallet_deposit_enabled":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            wallet_set_deposit_enabled(bool(value))
        elif key == "wallet_withdraw_enabled":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            wallet_set_withdraw_enabled(bool(value))
        elif key in {
            "maintenance_enabled",
            "maintenance_reason",
            "account_approval_mode",
            "account_auto_approval_delay_seconds",
            "account_verification_max_yer",
            "unified_notification_channel_id",
            "channel_users_id",
            "channel_admin_id",
            "channel_backup_id",
            "notification_channel_id",
            "technical_channel_id",
            "backup_channel_id",
            "account_verification_channel_id",
        }:
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية التحكم التشغيلي")
            if key == "maintenance_enabled":
                set_maintenance(bool(value), str(request.values.get("maintenance_reason") or maintenance_reason()))
            elif key == "maintenance_reason":
                reason = str(value or "").strip()
                if len(reason) > 500:
                    raise HTTPException(status_code=400, detail="سبب الصيانة أطول من الحد المسموح")
                set_maintenance(maintenance_enabled(), reason)
            elif key == "account_approval_mode":
                mode = str(value or "").upper()
                if mode not in {"MANUAL", "AUTO"}:
                    raise HTTPException(status_code=400, detail="وضع القبول يجب أن يكون MANUAL أو AUTO")
                DBX.set_setting(key, mode)
            elif key == "account_auto_approval_delay_seconds":
                try:
                    seconds = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="مدة القبول التلقائي غير صالحة") from exc
                if not 0 <= seconds <= 86400:
                    raise HTTPException(status_code=400, detail="المدة يجب أن تكون بين 0 و86400 ثانية")
                DBX.set_setting(key, str(seconds))
            elif key == "account_verification_max_yer":
                try:
                    maximum = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="حد مبلغ التحقق غير صالح") from exc
                if not 100 <= maximum <= 5000:
                    raise HTTPException(status_code=400, detail="الحد يجب أن يكون بين 100 و5000 ريال")
                DBX.set_setting(key, str(maximum))
            elif key in {
                "unified_notification_channel_id",
                "channel_users_id",
                "channel_admin_id",
                "channel_backup_id",
                "notification_channel_id",
                "technical_channel_id",
                "backup_channel_id",
                "account_verification_channel_id",
            }:
                identifier = str(value or "").strip()
                if identifier and not (identifier.startswith("@") or identifier.lstrip("-").isdigit()):
                    raise HTTPException(status_code=400, detail="معرف القناة يجب أن يكون chat_id أو @username")
                if key in {"channel_backup_id", "backup_channel_id"} and identifier and not identifier.lstrip("-").isdigit():
                    raise HTTPException(
                        status_code=400,
                        detail="قناة النسخ الاحتياطية تتطلب Chat ID رقميًا مثل -100...",
                    )
                if key in {"channel_users_id", "notification_channel_id"}:
                    set_users_channel(identifier)
                elif key in {"channel_backup_id", "backup_channel_id"}:
                    set_project_backup_channel(identifier)
                else:
                    set_admin_channel(identifier)
        elif key == "display_decimals":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            try:
                set_display_decimals(value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif key == "public_liquidity_enabled":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_public_liquidity_enabled(bool(value))
        elif key == "public_liquidity_reserve_usdt":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_public_liquidity_reserve(value)
        elif key == "liquidity_alert_enabled":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_liquidity_alert_enabled(bool(value))
        elif key == "liquidity_alert_threshold_usdt":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_liquidity_alert_threshold(value)
        elif key == "liquidity_alert_recovery_usdt":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_liquidity_alert_recovery(value)
        elif key == "liquidity_alert_cooldown_minutes":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_liquidity_alert_cooldown_minutes(value)
        elif key == "withdrawal_operation_limit_enabled":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_withdrawal_operation_limit_enabled(bool(value))
        elif key == "withdrawal_operation_limit_usdt":
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات المحفظة")
            set_withdrawal_operation_limit(value)
        elif key in {
            "account_verification_channel_id",
            "account_verification_recipient_phone",
            "account_verification_recipient_name",
            "network_address_video_url",
            "bot_usage_guide_url",
        }:
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات التوثيق")
            text_value = str(value or "").strip()
            if key in {"network_address_video_url", "bot_usage_guide_url"} and text_value and not _valid_https_url(text_value):
                raise HTTPException(status_code=400, detail="رابط الشرح يجب أن يبدأ بـ https://")
            if key == "account_verification_channel_id" and text_value:
                valid_channel = text_value.startswith("@") or text_value.lstrip("-").isdigit()
                if not valid_channel:
                    raise HTTPException(status_code=400, detail="معرف قناة الطلبات يجب أن يكون رقم chat_id أو @username")
            if key == "account_verification_recipient_phone" and text_value and not text_value.isdigit():
                raise HTTPException(status_code=400, detail="رقم مستفيد التحقق يجب أن يحتوي على أرقام فقط")
            limits = {
                "account_verification_channel_id": 80,
                "account_verification_recipient_phone": 20,
                "account_verification_recipient_name": 120,
                "network_address_video_url": 500,
                "bot_usage_guide_url": 500,
            }
            if len(text_value) > limits[key]:
                raise HTTPException(status_code=400, detail="القيمة أطول من الحد المسموح")
            DBX.set_setting(key, text_value)
        elif key in {
            "mini_banner_enabled",
            "mini_banner_animation_enabled",
            "mini_banner_badge",
            "mini_banner_title",
            "mini_banner_text",
            "mini_banner_image_url",
            "mini_banner_link_url",
            "mini_banner_button_text",
            "mini_banner_width_percent",
            "mini_banner_height_px",
            "mini_banner_image_fit",
        }:
            if not DBX.has_permission(admin_id, "wallet_settings"):
                raise HTTPException(status_code=403, detail="لا تملك صلاحية إعدادات واجهة المحفظة")
            if key in {"mini_banner_enabled", "mini_banner_animation_enabled"}:
                DBX.set_setting(key, "1" if bool(value) else "0")
                continue
            if key == "mini_banner_width_percent":
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="عرض الإعلان يجب أن يكون رقمًا صحيحًا") from exc
                if not 80 <= numeric <= 100:
                    raise HTTPException(status_code=400, detail="عرض الإعلان يجب أن يكون بين 80% و100%")
                DBX.set_setting(key, str(numeric))
                continue
            if key == "mini_banner_height_px":
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="ارتفاع الإعلان يجب أن يكون رقمًا صحيحًا") from exc
                if not 90 <= numeric <= 260:
                    raise HTTPException(status_code=400, detail="ارتفاع الإعلان يجب أن يكون بين 90 و260 بكسل")
                DBX.set_setting(key, str(numeric))
                continue
            if key == "mini_banner_image_fit":
                fit = str(value or "cover").strip().lower()
                if fit not in {"cover", "contain"}:
                    raise HTTPException(status_code=400, detail="طريقة عرض الصورة غير صالحة")
                DBX.set_setting(key, fit)
                continue
            text_value = str(value or "").strip()
            limits = {
                "mini_banner_badge": 40,
                "mini_banner_title": 90,
                "mini_banner_text": 240,
                "mini_banner_image_url": 500,
                "mini_banner_link_url": 500,
                "mini_banner_button_text": 40,
            }
            if len(text_value) > limits[key]:
                raise HTTPException(status_code=400, detail="محتوى الإعلان أطول من الحد المسموح")
            if key in {"mini_banner_image_url", "mini_banner_link_url"} and not _valid_https_url(text_value):
                raise HTTPException(status_code=400, detail="رابط الإعلان يجب أن يبدأ بـ https://")
            DBX.set_setting(key, text_value)
        else:
            DBX.set_setting(key, str(value).strip())
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_SETTINGS", "settings", "webapp", json.dumps(request.values, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/admin/managed-assets")
def admin_managed_assets(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user, "wallet_settings")
    items: list[dict[str, Any]] = []
    for row in list_managed_assets():
        item = dict(row)
        item["liquidity"] = managed_asset_liquidity(str(row["symbol"]))
        items.append(item)
    return {"items": items}


@app.post("/api/admin/managed-assets")
def admin_save_managed_asset(
    request: ManagedAssetRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        row = upsert_managed_asset(request.symbol, request.display_name, enabled=request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "UPSERT_MANAGED_ASSET", "managed_asset", row["symbol"])
    return {"ok": True, "item": dict(row)}


@app.delete("/api/admin/managed-assets/{symbol}")
def admin_delete_managed_asset(
    symbol: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        delete_managed_asset(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "DELETE_MANAGED_ASSET", "managed_asset", symbol.upper())
    return {"ok": True}


@app.get("/api/admin/managed-networks")
def admin_managed_networks(
    asset_symbol: str = Query(default="USDT"),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "wallet_settings")
    return {"items": [dict(row) for row in list_managed_networks(asset_symbol=asset_symbol)]}


@app.post("/api/admin/managed-networks")
def admin_create_managed_network(
    request: ManagedNetworkRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        row = create_managed_network(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "CREATE_MANAGED_NETWORK", "managed_network", int(row["id"]))
    return {"ok": True, "item": dict(row)}


@app.patch("/api/admin/managed-networks/{network_id}")
def admin_update_managed_network(
    network_id: int,
    request: ManagedNetworkUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    values = request.model_dump(exclude_unset=True)
    try:
        if "enabled" in values:
            set_managed_network_enabled(network_id, bool(values.pop("enabled")))
        if "minimum_floor" in values:
            set_managed_network_minimum_floor(network_id, values.pop("minimum_floor"))
        row = update_managed_network(network_id, **values) if values else None
        if row is None:
            with DBX.connect() as con:
                row = con.execute("SELECT * FROM managed_networks WHERE id=?", (network_id,)).fetchone()
        if row is None:
            raise ValueError("الشبكة غير موجودة")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_MANAGED_NETWORK", "managed_network", network_id)
    return {"ok": True, "item": dict(row)}


@app.get("/api/admin/tutorial-buttons")
def admin_tutorial_buttons(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "wallet_settings")
    return {"items": [dict(row) for row in list_tutorial_buttons()]}


@app.post("/api/admin/tutorial-buttons")
def admin_create_tutorial_button(
    request: TutorialButtonCreateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        row = create_tutorial_button(
            request.name,
            request.url,
            request.icon,
            admin_id=admin_id,
            custom_emoji_id=request.custom_emoji_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "CREATE_TUTORIAL_BUTTON", "tutorial_button", int(row["id"]), row["name"])
    return {"ok": True, "item": dict(row)}


@app.patch("/api/admin/tutorial-buttons/{button_id}")
def admin_update_tutorial_button(
    button_id: int,
    request: TutorialButtonUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        row = update_tutorial_button(button_id, **request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "UPDATE_TUTORIAL_BUTTON", "tutorial_button", button_id)
    return {"ok": True, "item": dict(row)}


@app.delete("/api/admin/tutorial-buttons/{button_id}")
def admin_delete_tutorial_button(
    button_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        delete_tutorial_button(button_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "DELETE_TUTORIAL_BUTTON", "tutorial_button", button_id)
    return {"ok": True}


@app.delete("/api/admin/managed-networks/{network_id}")
def admin_delete_managed_network(
    network_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "wallet_settings")
    try:
        delete_managed_network(network_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DBX.audit(admin_id, "Mini App Admin", "DELETE_MANAGED_NETWORK", "managed_network", network_id)
    return {"ok": True}


@app.post("/api/admin/broadcasts")
def admin_broadcast(
    request: BroadcastRequest,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_id = _require_admin(user, "orders_manage")
    background_tasks.add_task(_broadcast_background, admin_id, request.text)
    DBX.audit(admin_id, "Mini App Admin", "WEBAPP_BROADCAST_QUEUED", "broadcast", "pending")
    return {"ok": True, "status": "QUEUED", "targets": len(list_broadcast_targets())}


@app.post("/api/admin/backups")
def admin_backup(user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    _require_admin(user, "backup_create")
    path = create_backup_file()
    return FileResponse(
        path,
        media_type="application/json",
        filename=path.name,
    )


@app.get("/api/admin/audit-log")
def admin_audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    _require_admin(user, "security_view")
    with DBX.connect() as con:
        rows = con.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get('/api/admin/customization')
def customization_get(user: dict[str, Any] = Depends(current_user)):
    _require_admin(user, 'settings_sales')
    return runtime.get('presentation_config')()

@app.put('/api/admin/customization')
def customization_put(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)):
    uid=_require_admin(user, 'settings_sales')
    try: result=runtime.get('save_presentation')(payload)
    except Exception as exc: raise _safe_error(exc) from exc
    DBX.audit(uid,'admin','CUSTOMIZE_BOT','settings','presentation_v1')
    return result

@app.get('/api/admin/ton-wallet')
async def ton_wallet_status(user: dict[str, Any] = Depends(current_user)):
    _require_admin(user,'wallet_settings')
    from bot.providers.ton_hot import TonHotWallet
    provider=TonHotWallet()
    result={'configured':provider.configured(),'address':provider.address,'enabled':runtime.get('ton_enabled')(),
        'global_withdraw_enabled':wallet_withdraw_enabled(),'maintenance':maintenance_enabled(),
        'settings':{k:DBX.get_setting(k) or v for k,v in {'ton_min_usdt':'1','ton_gas_budget':'0.1','ton_low_usdt':'20','ton_low_gas':'0.5','ton_max_usdt':'500'}.items()}}
    if provider.configured():
        try: result['balances']={k:str(v) for k,v in (await asyncio.to_thread(provider.balances)).items() if k!='jetton'}
        except Exception: result['error']='تعذر قراءة أرصدة TON. تحقق من الاتصال والإعدادات.'
    return result

@app.put('/api/admin/ton-wallet')
async def ton_wallet_settings(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)):
    uid=_require_admin(user,'wallet_settings')
    from bot.providers.ton_hot import TonHotWallet
    enabled=payload.get('enabled') is True
    cleaned={}
    for key,low,high in [('ton_min_usdt','0.01','100000'),('ton_gas_budget','0.05','1'),('ton_low_usdt','0','100000'),('ton_low_gas','0.1','100'),('ton_max_usdt','1','1000000')]:
        try:
            value=Decimal(str(payload[key]))
            if not value.is_finite() or not Decimal(low)<=value<=Decimal(high): raise ValueError()
        except Exception: raise HTTPException(400,detail='إعدادات TON غير صالحة')
        cleaned[key]=str(value)
    if enabled:
        provider=TonHotWallet()
        if not provider.configured(): raise HTTPException(400,detail='أكمل ملف المفتاح وعنوان المحفظة وTONCENTER_API_KEY على السيرفر أولاً.')
        try:
            await asyncio.to_thread(provider.signer)
            balances=await asyncio.to_thread(provider.balances)
            seqno=int((await asyncio.to_thread(provider.run,provider.address,'seqno'))[0][1],16)
            if seqno<=0 or balances['ton']<Decimal(cleaned['ton_gas_budget'])+Decimal('0.05'): raise ValueError()
        except Exception: raise HTTPException(400,detail='فشل فحص المحفظة؛ يجب تهيئتها وتمويل رسومها والتأكد من تطابق المفتاح والعنوان.')
    with DBX.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        for key,value in cleaned.items():
            con.execute('INSERT INTO settings_v2(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,now_str()))
        con.execute("UPDATE managed_networks SET enabled=?,updated_at=? WHERE network_code='TON_USDT'",(int(enabled),now_str()))
        con.commit()
    DBX.audit(uid,'admin','TON_SETTINGS','settings','ton_wallet')
    return {'ok':True}


class JaibScanRequest(BaseModel):
    session_token: str

@app.post('/api/me/deposits/scan')
async def scan_deposits(request: JaibScanRequest,user: dict[str,Any]=Depends(current_user)):
    _active_customer(user,allow_admin=False)
    body=_decode_action(request.session_token,'deposit',int(user['id']))
    try:
        return await runtime.get('scan_jaib_deposits')(int(user['id']),body['payload']['exchange_rate_yer'],body['payload']['prepared_at'])
    except Exception as exc:raise _safe_error(exc) from exc

# Admin wallet routes use the same durable queue and database as Telegram.
from bot.webapp.wallet20 import install_wallet_routes
install_wallet_routes(app,globals())
from bot.webapp.diagnostics20 import install_diagnostic_routes,install_design_routes
install_diagnostic_routes(app,globals())
install_design_routes(app,globals())

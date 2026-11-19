"""V18 operational controls, separated channels, and managed Binance routes.

This layer is loaded last so it can extend the legacy administration UI without
duplicating the financial ledger.  Existing USDT balances and pending operations
remain in their original tables; only configuration and scheduling metadata are
added here.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals

_seed_globals(globals())


_DEFAULT_NETWORKS = (
    ("USDT", "BSC", "BNB Smart Chain (BEP20)", r"0x[0-9a-fA-F]{40}", 1, 10),
    ("USDT", "TRX", "Tron (TRC20)", r"T[1-9A-HJ-NP-Za-km-z]{33}", 0, 20),
    ("USDT", "TON", "TON", r"(?:(?:EQ|UQ)[A-Za-z0-9_-]{46}|[0-9a-fA-F]{64})", 0, 30),
    ("USDT", "ETH", "Ethereum (ERC20)", r"0x[0-9a-fA-F]{40}", 0, 40),
    ("USDT", "MATIC", "Polygon", r"0x[0-9a-fA-F]{40}", 0, 50),
)

_DEFAULT_ASSETS = (
    ("USDT", "Tether USD", 1, 10),
    ("TRX", "Tron", 0, 20),
    ("TON", "Toncoin", 0, 30),
    ("BNB", "BNB", 0, 40),
)


def _v18_schema() -> None:
    with DBX.connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_assets (
                symbol TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'BINANCE',
                enabled INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_networks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_symbol TEXT NOT NULL,
                network_code TEXT NOT NULL,
                display_label TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'BINANCE',
                address_regex TEXT NOT NULL,
                wallet_address TEXT,
                fee_override TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(asset_symbol, network_code),
                FOREIGN KEY(asset_symbol) REFERENCES managed_assets(symbol)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS liquidity_message_targets (
                telegram_id INTEGER PRIMARY KEY,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                last_liquidity TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        asset_columns = {
            str(row["name"]) for row in con.execute("PRAGMA table_info(managed_assets)").fetchall()
        }
        if "liquidity_reserve" not in asset_columns:
            con.execute("ALTER TABLE managed_assets ADD COLUMN liquidity_reserve TEXT NOT NULL DEFAULT '0'")
        columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(account_verification_requests)").fetchall()
        }
        for column, declaration in {
            "approval_mode": "TEXT NOT NULL DEFAULT 'MANUAL'",
            "auto_approve_at_epoch": "REAL",
            "auto_approval_attempts": "INTEGER NOT NULL DEFAULT 0",
            "auto_approval_error": "TEXT",
        }.items():
            if column not in columns:
                con.execute(
                    f"ALTER TABLE account_verification_requests ADD COLUMN {column} {declaration}"
                )
        now = now_str()
        for symbol, name, enabled, sort_order in _DEFAULT_ASSETS:
            con.execute(
                """
                INSERT OR IGNORE INTO managed_assets(
                    symbol, display_name, provider, enabled, sort_order, created_at, updated_at
                ) VALUES (?, ?, 'BINANCE', ?, ?, ?, ?)
                """,
                (symbol, name, enabled, sort_order, now, now),
            )
        for asset, code, label, regex, enabled, sort_order in _DEFAULT_NETWORKS:
            con.execute(
                """
                INSERT OR IGNORE INTO managed_networks(
                    asset_symbol, network_code, display_label, provider, address_regex,
                    enabled, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, 'BINANCE', ?, ?, ?, ?, ?)
                """,
                (asset, code, label, regex, enabled, sort_order, now, now),
            )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_managed_networks_active "
            "ON managed_networks(asset_symbol, enabled, sort_order)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_verification_auto_due "
            "ON account_verification_requests(status, auto_approve_at_epoch)"
        )
        con.commit()

    legacy_users_channel = str(DBX.get_setting("notification_channel_id") or CHANNEL_USERS or "").strip()
    legacy_admin_channel = str(
        DBX.get_setting("channel_admin_id")
        or DBX.get_setting("unified_notification_channel_id")
        or DBX.get_setting("technical_channel_id")
        or DBX.get_setting("account_verification_channel_id")
        or CHANNEL_ADMIN
        or ""
    ).strip()
    legacy_backup_channel = str(DBX.get_setting("backup_channel_id") or CHANNEL_BACKUP or "").strip()
    defaults = {
        "maintenance_enabled": "0",
        "maintenance_reason": "",
        "account_approval_mode": "MANUAL",
        "account_auto_approval_delay_seconds": "30",
        "account_verification_max_yer": "300",
        "channel_users_id": legacy_users_channel,
        "channel_users_title": DBX.get_setting("notification_channel_title") or legacy_users_channel,
        "channel_admin_id": legacy_admin_channel,
        "channel_admin_title": DBX.get_setting("unified_notification_channel_title") or legacy_admin_channel,
        "channel_backup_id": legacy_backup_channel,
        "channel_backup_title": DBX.get_setting("backup_channel_title") or legacy_backup_channel,
    }
    for key, value in defaults.items():
        if DBX.get_setting(key) is None:
            DBX.set_setting(key, value)

    # مرآة توافقية للطبقات القديمة: قناة المستخدمين للنشر العام فقط، وكل
    # الأحداث التشغيلية والتقنية لقناة الأدمن، وقناة النسخ تبقى مستقلة.
    if legacy_users_channel:
        DBX.set_setting("notification_channel_id", legacy_users_channel)
    if legacy_admin_channel:
        DBX.set_setting("unified_notification_channel_id", legacy_admin_channel)
        for key in ("technical_channel_id", "log_channel_id", "account_verification_channel_id"):
            DBX.set_setting(key, legacy_admin_channel)
        DBX.set_setting("technical_channel_enabled", "1")
        DBX.set_setting("log_channel_enabled", "1")
    if legacy_backup_channel:
        DBX.set_setting("backup_channel_id", legacy_backup_channel)


_v18_schema()


def format_yer_amount(value: Any) -> str:
    """YER is always displayed as a whole number; storage precision is unchanged."""
    amount = decimal_or_zero(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return format(amount, ".0f")


def customer_network_fee(actual_fee: Any) -> Decimal:
    """The service absorbs one cent of the live network fee."""
    actual = max(Decimal("0"), decimal_or_zero(actual_fee))
    if actual <= Decimal("0.01"):
        return Decimal("0")
    return (actual - Decimal("0.01")).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def customer_network_fee_text(actual_fee: Any) -> str:
    fee = customer_network_fee(actual_fee)
    return "مجانًا 🎁" if fee <= 0 else f"{format_display_amount(fee)} USDT"


def maintenance_enabled() -> bool:
    return str(DBX.get_setting("maintenance_enabled") or "0") == "1"


def maintenance_reason() -> str:
    return str(DBX.get_setting("maintenance_reason") or "").strip()


def maintenance_message() -> str:
    reason = maintenance_reason()
    suffix = f"\n\nالسبب: {html(reason)}" if reason else ""
    return f"🔧 <b>البوت في وضع الصيانة حاليًا.</b>{suffix}"


def set_maintenance(enabled: bool, reason: str = "") -> None:
    DBX.set_setting("maintenance_enabled", "1" if enabled else "0")
    DBX.set_setting("maintenance_reason", str(reason or "").strip()[:500] if enabled else "")


def account_approval_mode() -> str:
    value = str(DBX.get_setting("account_approval_mode") or "MANUAL").upper()
    return value if value in {"MANUAL", "AUTO"} else "MANUAL"


def account_auto_approval_delay_seconds() -> int:
    try:
        value = int(DBX.get_setting("account_auto_approval_delay_seconds") or 30)
    except (TypeError, ValueError):
        value = 30
    return max(0, min(value, 86400))


def account_verification_max_yer() -> int:
    try:
        value = int(DBX.get_setting("account_verification_max_yer") or 300)
    except (TypeError, ValueError):
        value = 300
    return max(int(globals().get("ACCOUNT_VERIFICATION_MIN_YER", 100)), min(value, 5000))


def _channel_identifier(*candidates: Any) -> Optional[int | str]:
    for raw in candidates:
        value = str(raw or "").strip()
        if value:
            return int(value) if value.lstrip("-").isdigit() else value
    return None


def users_channel_id() -> Optional[int | str]:
    return _channel_identifier(
        DBX.get_setting("channel_users_id"),
        DBX.get_setting("notification_channel_id"),
        CHANNEL_USERS,
    )


def admin_channel_id() -> Optional[int | str]:
    return _channel_identifier(
        DBX.get_setting("channel_admin_id"),
        DBX.get_setting("unified_notification_channel_id"),
        DBX.get_setting("technical_channel_id"),
        CHANNEL_ADMIN,
    )


def project_backup_channel_id() -> Optional[int | str]:
    return _channel_identifier(
        DBX.get_setting("channel_backup_id"),
        DBX.get_setting("backup_channel_id"),
        CHANNEL_BACKUP,
    )


def unified_notification_channel_id() -> Optional[int | str]:
    """Compatibility alias: unified operational events now belong to admin."""
    return admin_channel_id()


def set_users_channel(identifier: str, title: str = "") -> None:
    value = str(identifier or "").strip()
    label = str(title or value).strip()[:200]
    DBX.set_setting("channel_users_id", value)
    DBX.set_setting("channel_users_title", label)
    DBX.set_setting("notification_channel_id", value)
    DBX.set_setting("notification_channel_title", label)


def set_admin_channel(identifier: str, title: str = "") -> None:
    value = str(identifier or "").strip()
    label = str(title or value).strip()[:200]
    DBX.set_setting("channel_admin_id", value)
    DBX.set_setting("channel_admin_title", label)
    DBX.set_setting("unified_notification_channel_id", value)
    DBX.set_setting("unified_notification_channel_title", label)
    # كل المنتجين التشغيليين والتقنيين يتجهون إلى قناة الأدمن فقط.
    for key in (
        "technical_channel_id",
        "log_channel_id",
        "account_verification_channel_id",
    ):
        DBX.set_setting(key, value)
    DBX.set_setting("technical_channel_enabled", "1" if value else "0")
    DBX.set_setting("log_channel_enabled", "1" if value else "0")


def set_project_backup_channel(identifier: str, title: str = "") -> None:
    value = str(identifier or "").strip()
    label = str(title or value).strip()[:200]
    DBX.set_setting("channel_backup_id", value)
    DBX.set_setting("channel_backup_title", label)
    DBX.set_setting("backup_channel_id", value)
    DBX.set_setting("backup_channel_title", label)


def set_unified_notification_channel(identifier: str, title: str = "") -> None:
    """Legacy setter retained for older screens; it now changes admin only."""
    set_admin_channel(identifier, title)


async def notify_admin(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Send operational notices to the private admin channel and active admins."""
    destinations: list[int | str] = []
    channel = admin_channel_id()
    if channel is not None:
        destinations.append(channel)
    destinations.append(int(ADMIN_ID))
    for row in DBX.list_admins():
        if row["status"] == "ACTIVE":
            destinations.append(int(row["telegram_id"]))
    seen: set[str] = set()
    for destination in destinations:
        key = str(destination)
        if key in seen:
            continue
        seen.add(key)
        try:
            await context.bot.send_message(
                chat_id=destination,
                text=render_bot_text('bot.services.v18_features.py:343:message','{v0}', [str(text)]),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            log.exception("failed to send admin notification destination=%s", destination)


def list_managed_assets(*, enabled_only: bool = False) -> list[Any]:
    where = "WHERE enabled=1" if enabled_only else ""
    with DBX.connect() as con:
        return con.execute(
            f"SELECT * FROM managed_assets {where} ORDER BY sort_order, symbol"
        ).fetchall()


def set_managed_asset_enabled(symbol: str, enabled: bool) -> None:
    with DBX.connect() as con:
        changed = con.execute(
            "UPDATE managed_assets SET enabled=?, updated_at=? WHERE symbol=?",
            (1 if enabled else 0, now_str(), str(symbol or "").upper()),
        )
        con.commit()
    if changed.rowcount != 1:
        raise ValueError("العملة غير موجودة")


def upsert_managed_asset(symbol: str, display_name: str, *, enabled: bool = False) -> Any:
    code = str(symbol or "").strip().upper()
    name = str(display_name or "").strip()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", code):
        raise ValueError("رمز العملة يجب أن يكون 2-12 حرفًا أو رقمًا إنجليزيًا")
    if len(name) < 2 or len(name) > 100:
        raise ValueError("اسم العملة غير صالح")
    with DBX.connect() as con:
        con.execute(
            """
            INSERT INTO managed_assets(symbol, display_name, provider, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, 'BINANCE', ?, 100, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET display_name=excluded.display_name,
                enabled=excluded.enabled, updated_at=excluded.updated_at
            """,
            (code, name, 1 if enabled else 0, now_str(), now_str()),
        )
        con.commit()
        return con.execute("SELECT * FROM managed_assets WHERE symbol=?", (code,)).fetchone()


def delete_managed_asset(symbol: str) -> None:
    code = str(symbol or "").strip().upper()
    if code == "USDT":
        raise ValueError("لا يمكن حذف USDT لأنها عملة الأرصدة الحالية")
    with DBX.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) AS c FROM managed_networks WHERE asset_symbol=?", (code,)
        ).fetchone()["c"]
        if int(count or 0):
            raise ValueError("احذف شبكات العملة أولًا")
        changed = con.execute("DELETE FROM managed_assets WHERE symbol=?", (code,))
        con.commit()
    if changed.rowcount != 1:
        raise ValueError("العملة غير موجودة")


def managed_asset_liquidity(symbol: str) -> dict[str, Any]:
    code = str(symbol or "").strip().upper()
    with DBX.connect() as con:
        row = con.execute("SELECT * FROM managed_assets WHERE symbol=?", (code,)).fetchone()
    if not row:
        raise ValueError("العملة غير موجودة")
    reserve = max(Decimal("0"), decimal_or_zero(row["liquidity_reserve"]))
    if not BINANCE.enabled():
        return {"symbol": code, "available_known": False, "available": "", "reserve": q8(reserve)}
    try:
        free = max(Decimal("0"), wallet_decimal(BINANCE.get_spot_free_balance(code)))
        available = max(Decimal("0"), free - reserve)
        return {
            "symbol": code,
            "available_known": True,
            "available": q8(available),
            "provider_free": q8(free),
            "reserve": q8(reserve),
        }
    except Exception as exc:
        return {
            "symbol": code,
            "available_known": False,
            "available": "",
            "reserve": q8(reserve),
            "error": type(exc).__name__,
        }


def list_managed_networks(
    *, asset_symbol: str = "USDT", enabled_only: bool = False
) -> list[Any]:
    where = "asset_symbol=?"
    params: list[Any] = [str(asset_symbol or "USDT").upper()]
    if enabled_only:
        where += " AND enabled=1"
    with DBX.connect() as con:
        return con.execute(
            f"SELECT * FROM managed_networks WHERE {where} ORDER BY sort_order, id",
            params,
        ).fetchall()


def get_managed_network(network_code: str, *, asset_symbol: str = "USDT") -> Optional[Any]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM managed_networks WHERE asset_symbol=? AND network_code=?",
            (str(asset_symbol or "USDT").upper(), str(network_code or "").upper()),
        ).fetchone()


def set_managed_network_enabled(network_id: int, enabled: bool) -> None:
    with DBX.connect() as con:
        changed = con.execute(
            "UPDATE managed_networks SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now_str(), int(network_id)),
        )
        con.commit()
    if changed.rowcount != 1:
        raise ValueError("الشبكة غير موجودة")


def create_managed_network(
    *,
    asset_symbol: str,
    network_code: str,
    display_label: str,
    address_regex: str,
    wallet_address: str = "",
    fee_override: str = "",
    enabled: bool = False,
) -> Any:
    asset = str(asset_symbol or "USDT").strip().upper()
    code = str(network_code or "").strip().upper()
    label = str(display_label or "").strip()
    pattern = str(address_regex or "").strip()
    if not re.fullmatch(r"[A-Z0-9_-]{2,20}", code):
        raise ValueError("رمز الشبكة غير صالح")
    if len(label) < 2 or len(label) > 100:
        raise ValueError("اسم الشبكة غير صالح")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError("نمط عنوان الشبكة غير صالح") from exc
    fee_value: Optional[str] = None
    if str(fee_override or "").strip():
        fee = decimal_or_zero(fee_override)
        if fee < 0:
            raise ValueError("الرسوم لا يمكن أن تكون سالبة")
        fee_value = q8(fee)
    with DBX.connect() as con:
        if not con.execute("SELECT 1 FROM managed_assets WHERE symbol=?", (asset,)).fetchone():
            raise ValueError("العملة غير موجودة في الكتالوج")
        try:
            cur = con.execute(
                """
                INSERT INTO managed_networks(
                    asset_symbol, network_code, display_label, provider, address_regex,
                    wallet_address, fee_override, enabled, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, 'BINANCE', ?, ?, ?, ?, 100, ?, ?)
                """,
                (
                    asset, code, label, pattern, str(wallet_address or "").strip()[:300] or None,
                    fee_value, 1 if enabled else 0, now_str(), now_str(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("هذه الشبكة موجودة مسبقًا للعملة") from exc
        con.commit()
        return con.execute("SELECT * FROM managed_networks WHERE id=?", (int(cur.lastrowid),)).fetchone()


def delete_managed_network(network_id: int) -> None:
    with DBX.connect() as con:
        row = con.execute("SELECT * FROM managed_networks WHERE id=?", (int(network_id),)).fetchone()
        if not row:
            raise ValueError("الشبكة غير موجودة")
        referenced = con.execute(
            "SELECT COUNT(*) AS c FROM wallet_withdrawals WHERE network_code=?",
            (str(row["network_code"]),),
        ).fetchone()["c"]
        if int(referenced or 0):
            raise ValueError("لا يمكن حذف شبكة لها عمليات سابقة؛ أوقفها بدلًا من ذلك")
        con.execute("DELETE FROM managed_networks WHERE id=?", (int(network_id),))
        con.commit()


def update_managed_network(
    network_id: int, *, wallet_address: Optional[str] = None, fee_override: Optional[str] = None
) -> Any:
    row = None
    with DBX.connect() as con:
        current = con.execute("SELECT * FROM managed_networks WHERE id=?", (int(network_id),)).fetchone()
        if not current:
            raise ValueError("الشبكة غير موجودة")
        if wallet_address is not None:
            con.execute(
                "UPDATE managed_networks SET wallet_address=?, updated_at=? WHERE id=?",
                (str(wallet_address).strip()[:300], now_str(), int(network_id)),
            )
        if fee_override is not None:
            value = str(fee_override or "").strip()
            if value:
                fee = decimal_or_zero(value)
                if fee < 0:
                    raise ValueError("الرسوم لا يمكن أن تكون سالبة")
                value = q8(fee)
            con.execute(
                "UPDATE managed_networks SET fee_override=?, updated_at=? WHERE id=?",
                (value or None, now_str(), int(network_id)),
            )
        con.commit()
        row = con.execute("SELECT * FROM managed_networks WHERE id=?", (int(network_id),)).fetchone()
    return row


def wallet_network_routes() -> list[dict[str, str]]:
    if not BINANCE_PROVIDER.enabled():
        return []
    return [
        {
            "code": str(row["network_code"]),
            "label": str(row["display_label"]),
            "provider": "BINANCE",
            "asset": str(row["asset_symbol"]),
        }
        for row in list_managed_networks(asset_symbol="USDT", enabled_only=True)
    ]


def available_network_routes() -> list[dict[str, str]]:
    return wallet_network_routes()


def wallet_network_kb() -> InlineKeyboardMarkup:
    rows = [
        [btn(render_bot_button('bot.services.v18_features.py:585:button','🌐 {v0}', [f"{item['label']}"]), f"wallet_network:{item['code']}", "primary")]
        for item in wallet_network_routes()
    ]
    if not rows:
        rows.append([btn(render_bot_button('bot.services.v18_features.py:589:button','⚠️ لا توجد شبكة جاهزة', []), "main_menu")])
    rows.append([btn(render_bot_button('bot.services.v18_features.py:590:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


def wallet_validate_destination(network_code: str, address: str) -> str:
    code = str(network_code or "").upper()
    value = str(address or "").strip()
    row = get_managed_network(code)
    if not row or not int(row["enabled"] or 0):
        raise WalletUserError("الشبكة المختارة غير متاحة حاليًا.")
    pattern = str(row["address_regex"] or "").strip()
    try:
        matched = bool(pattern and re.fullmatch(pattern, value))
    except re.error as exc:
        raise WalletTechnicalError(f"نمط عنوان الشبكة {code} غير صالح") from exc
    if not matched:
        raise WalletUserError(f"عنوان الاستلام لا يطابق شبكة {row['display_label']}.")
    return value


def wallet_provider_for_network(network_code: str) -> str:
    row = get_managed_network(network_code)
    if not row or not int(row["enabled"] or 0):
        raise WalletUserError("الشبكة المختارة غير متاحة حاليًا.")
    if str(row["provider"] or "").upper() != "BINANCE" or not BINANCE_PROVIDER.enabled():
        raise WalletTechnicalError("Binance غير جاهز للسحب")
    return "BINANCE"


def payout_provider_for_network(network_code: str) -> Any:
    wallet_provider_for_network(network_code)
    return BINANCE_PROVIDER


def managed_network_label(network_code: str) -> str:
    row = get_managed_network(network_code)
    return str(row["display_label"] if row else network_code)


def managed_network_fee_override(network_code: str) -> Optional[Decimal]:
    row = get_managed_network(network_code)
    if not row or not str(row["fee_override"] or "").strip():
        return None
    return decimal_or_zero(row["fee_override"])


def effective_network_fee(network_code: str, live_fee: Any) -> Decimal:
    override = managed_network_fee_override(network_code)
    return max(Decimal("0"), override if override is not None else decimal_or_zero(live_fee))


def remember_liquidity_message(
    telegram_id: int,
    chat_id: int | str,
    message_id: int,
    message_text: str,
    liquidity_value: Any,
) -> None:
    with DBX.connect() as con:
        con.execute(
            """
            INSERT INTO liquidity_message_targets(
                telegram_id, chat_id, message_id, message_text, last_liquidity, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                chat_id=excluded.chat_id, message_id=excluded.message_id,
                message_text=excluded.message_text, last_liquidity=excluded.last_liquidity,
                updated_at=excluded.updated_at
            """,
            (
                int(telegram_id), str(chat_id), int(message_id), str(message_text),
                str(liquidity_value or ""), now_str(),
            ),
        )
        con.commit()


async def monitor_user_liquidity_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not public_liquidity_enabled():
        return
    try:
        snapshot = await asyncio.to_thread(get_public_liquidity, force_refresh=False)
    except Exception:
        log.exception("failed to refresh user liquidity messages")
        return
    if not snapshot.get("available_known"):
        return
    value = str(snapshot.get("available_display") or "0")
    label = str(snapshot.get("label") or PUBLIC_LIQUIDITY_LABEL)
    stale = " — آخر قيمة متاحة" if snapshot.get("stale") else ""
    replacement = (
        "<b>━━━━━━━━━━━━━━\n"
        f"💧 {html(label)}\n"
        f"💲 {html(value)} USDT{stale}\n"
        "━━━━━━━━━━━━━━</b>\n"
    )
    pattern = re.compile(r"<b>━━━━━━━━━━━━━━\n💧 .*?\n💲 .*? USDT.*?\n━━━━━━━━━━━━━━</b>\n", re.DOTALL)
    with DBX.connect() as con:
        rows = con.execute(
            "SELECT * FROM liquidity_message_targets WHERE last_liquidity!=? ORDER BY updated_at DESC LIMIT 200",
            (value,),
        ).fetchall()
    for row in rows:
        old_text = str(row["message_text"] or "")
        new_text, count = pattern.subn(replacement, old_text, count=1)
        if count != 1:
            continue
        chat_text = str(row["chat_id"])
        chat_id: int | str = int(chat_text) if chat_text.lstrip("-").isdigit() else chat_text
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(row["message_id"]),
                text=render_bot_text('bot.services.v18_features.py:700:message','{v0}', [str(new_text)]),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_user_main_kb(DBX.is_admin(int(row["telegram_id"]))),
            )
            with DBX.connect() as con:
                con.execute(
                    "UPDATE liquidity_message_targets SET message_text=?, last_liquidity=?, updated_at=? WHERE telegram_id=?",
                    (new_text, value, now_str(), int(row["telegram_id"])),
                )
                con.commit()
        except Exception as exc:
            message = str(exc).lower()
            if "message is not modified" in message:
                with DBX.connect() as con:
                    con.execute(
                        "UPDATE liquidity_message_targets SET last_liquidity=?, updated_at=? WHERE telegram_id=?",
                        (value, now_str(), int(row["telegram_id"])),
                    )
                    con.commit()
            elif "message to edit not found" in message or "can't be edited" in message:
                with DBX.connect() as con:
                    con.execute("DELETE FROM liquidity_message_targets WHERE telegram_id=?", (int(row["telegram_id"]),))
                    con.commit()


def _v18_admin_text() -> str:
    users_channel = DBX.get_setting("channel_users_title") or users_channel_id() or "غير محددة"
    admin_channel = DBX.get_setting("channel_admin_title") or admin_channel_id() or "غير محددة"
    backup_channel = DBX.get_setting("channel_backup_title") or project_backup_channel_id() or "غير محددة"
    jaib = jaib_health_state()
    return (
        render_bot_text('fragment.bot.services.v18_features.py:_v18_admin_text:735','<b>⚙️ التحكم التشغيلي المتكامل</b>\n\n🔧 الصيانة: <b>{v0}</b>\n👤 قبول الحسابات: <b>{v1}</b>\n⏱️ انتظار القبول التلقائي: <b>{v2} ثانية</b>\n💴 الحد الأعلى لمبلغ التحقق: <b>{v3} YER</b>\n👥 قناة المستخدمين: <b>{v4}</b>\n🔐 قناة الأدمن: <b>{v5}</b>\n💾 قناة النسخ: <b>{v6}</b>\n📲 اتصال Jaib: <b>{v7}</b>\n🕒 آخر فحص Jaib: <b>{v8}</b>\n🌐 شبكات USDT المفعلة: <b>{v9}</b>',[f"{('مفعلة' if maintenance_enabled() else 'متوقفة')}",f"{('تلقائي' if account_approval_mode() == 'AUTO' else 'يدوي')}",f'{account_auto_approval_delay_seconds()}',f'{format_yer_amount(account_verification_max_yer())}',f'{html(users_channel)}',f'{html(admin_channel)}',f'{html(backup_channel)}',f"{('متصل ✅' if jaib['ok'] else 'غير متصل ❌')}",f"{html(jaib['checked_at'])}",f'{len(list_managed_networks(enabled_only=True))}'])
    )


def _v18_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.v18_features.py:751:button','{v0}', [str('🔧 إيقاف الصيانة' if maintenance_enabled() else '🔧 تفعيل الصيانة')]), "adm_v18_maintenance", "danger" if not maintenance_enabled() else "success")],
            [btn(render_bot_button('bot.services.v18_features.py:752:button','{v0}', [str('👤 تحويل القبول إلى يدوي' if account_approval_mode() == 'AUTO' else '🤖 تحويل القبول إلى تلقائي')]), "adm_v18_approval_mode", "primary")],
            [btn(render_bot_button('bot.services.v18_features.py:753:button','⏱️ مدة القبول التلقائي', []), "adm_v18_approval_delay"), btn(render_bot_button('bot.services.v18_features.py:753:button','💴 حد مبلغ التحقق', []), "adm_v18_verification_max")],
            [btn(render_bot_button('bot.services.v18_features.py:754:button','👥 قناة المستخدمين', []), "adm_v18_channel_users"), btn(render_bot_button('bot.services.v18_features.py:754:button','🔐 قناة الأدمن', []), "adm_v18_channel_admin", "primary")],
            [btn(render_bot_button('bot.services.v18_features.py:755:button','💾 قناة النسخ الاحتياطية', []), "adm_v18_channel_backup")],
            [btn(render_bot_button('bot.services.v18_features.py:756:button','📲 فحص اتصال Jaib الآن', []), "adm_v18_jaib_check", "success")],
            [btn(render_bot_button('bot.services.v18_features.py:757:button','🌐 إدارة شبكات USDT', []), "adm_v18_networks"), btn(render_bot_button('bot.services.v18_features.py:757:button','🪙 إدارة العملات', []), "adm_v18_assets")],
            [btn(render_bot_button('bot.services.v18_features.py:758:button','🏠 لوحة الأدمن', []), "admin_panel")],
        ]
    )


def _network_list_kb() -> InlineKeyboardMarkup:
    rows = []
    for row in list_managed_networks():
        icon = "✅" if int(row["enabled"] or 0) else "⛔"
        rows.append([btn(render_bot_button('bot.services.v18_features.py:767:button','{v0} {v1}', [f'{icon}',f"{row['display_label']}"]), f"adm_v18_network:{row['id']}")])
    rows.append([btn(render_bot_button('bot.services.v18_features.py:768:button','↩️ التحكم التشغيلي', []), "adm_v18")])
    return InlineKeyboardMarkup(rows)


def _asset_list_kb() -> InlineKeyboardMarkup:
    rows = []
    for row in list_managed_assets():
        icon = "✅" if int(row["enabled"] or 0) else "⛔"
        label = f"{icon} {row['display_name']} ({row['symbol']})"
        rows.append([btn(render_bot_button('bot.services.v18_features.py:777:button','{v0}', [str(label)]), f"adm_v18_asset:{row['symbol']}")])
    rows.append([btn(render_bot_button('bot.services.v18_features.py:778:button','↩️ التحكم التشغيلي', []), "adm_v18")])
    return InlineKeyboardMarkup(rows)


_v18_previous_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _v18_previous_admin_kb(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    back = rows.pop() if rows and any((button.callback_data or "") == "main_menu" for button in rows[-1]) else [btn(render_bot_button('bot.services.v18_features.py:788:button','🏠 رجوع', []), "main_menu")]
    if DBX.has_permission(admin_id, "wallet_settings"):
        rows.append([btn(render_bot_button('bot.services.v18_features.py:790:button','🧭 التحكم التشغيلي', []), "adm_v18", "primary")])
    rows.append(back)
    return InlineKeyboardMarkup(rows)


_v18_previous_callback_permission = callback_permission


def callback_permission(data: str) -> Optional[str]:
    if data.startswith("cust_trusted_toggle:"):
        return "customers_edit"
    if data == "adm_v18" or data.startswith("adm_v18_"):
        return "wallet_settings"
    return _v18_previous_callback_permission(data)


_v18_previous_state_permission = state_permission


def state_permission(state: Optional[str]) -> Optional[str]:
    if state in {
        "await_v18_maintenance_reason",
        "await_v18_approval_delay",
        "await_v18_verification_max",
        "await_v18_channel_users",
        "await_v18_channel_admin",
        "await_v18_channel_backup",
        "await_v18_network_wallet",
        "await_v18_network_fee",
    }:
        return "wallet_settings"
    return _v18_previous_state_permission(state)


async def monitor_auto_account_approvals(context: ContextTypes.DEFAULT_TYPE) -> None:
    with DBX.connect() as con:
        rows = con.execute(
            """
            SELECT id FROM account_verification_requests
            WHERE status='PENDING_REVIEW' AND approval_mode='AUTO'
              AND auto_approve_at_epoch IS NOT NULL AND auto_approve_at_epoch <= ?
              AND auto_approval_attempts < 10
            ORDER BY id LIMIT 20
            """,
            (time.time(),),
        ).fetchall()
    for item in rows:
        request_id = int(item["id"])
        actor = SimpleNamespace(id=int(ADMIN_ID), full_name="النظام التلقائي", username="auto")
        try:
            updated, _ = await approve_account_verification(context.bot, request_id, actor)
            await notify_admin(
                context,
                render_bot_text('notice.bot.services.v18_features.py:834','<b>✅ تم قبول طلب تفعيل تلقائيًا</b>\n\n👤 {v0}\n🆔 <code>{v1}</code>\n🧾 <code>{v2}</code>',[f"{html(updated['full_name'] or '—')}",f"{int(updated['telegram_id'])}",f"{html(updated['request_code'])}"]),
            )
        except ValueError as exc:
            with DBX.connect() as con:
                con.execute(
                    "UPDATE account_verification_requests SET auto_approval_attempts=10, auto_approval_error=?, updated_at=? WHERE id=?",
                    (str(exc)[:1000], now_str(), request_id),
                )
                con.commit()
            log.warning("automatic account approval stopped request=%s reason=%s", request_id, exc)
        except Exception as exc:
            with DBX.connect() as con:
                con.execute(
                    """
                    UPDATE account_verification_requests
                    SET auto_approval_attempts=auto_approval_attempts+1,
                        auto_approval_error=?, auto_approve_at_epoch=?, updated_at=?
                    WHERE id=?
                    """,
                    (str(exc)[:1000], time.time() + 30, now_str(), request_id),
                )
                con.commit()
            log.exception("automatic account approval failed request=%s", request_id)


_v18_previous_post_init = post_init


async def post_init(application: Any) -> None:
    await _v18_previous_post_init(application)
    if application.job_queue is None:
        raise RuntimeError("JobQueue غير متاح")
    if not application.job_queue.get_jobs_by_name("v18-auto-account-approval"):
        application.job_queue.run_repeating(
            monitor_auto_account_approvals,
            interval=5,
            first=5,
            name="v18-auto-account-approval",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 15},
        )
    if not application.job_queue.get_jobs_by_name("v18-user-liquidity-messages"):
        application.job_queue.run_repeating(
            monitor_user_liquidity_messages,
            interval=45,
            first=45,
            name="v18-user-liquidity-messages",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )


_v18_previous_show_main = show_main
_v18_previous_on_callback = on_callback
_v18_previous_on_message = on_message


async def show_main(target: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    if user is not None and not DBX.is_admin(int(user.id)) and maintenance_enabled():
        await send_or_edit(target, render_bot_text('bot.services.v18_features.py:904:message','{v0}', [str(maintenance_message())]))
        return
    await _v18_previous_show_main(target, context)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = str(query.data or "")
    user = query.from_user

    if not DBX.is_admin(int(user.id)) and maintenance_enabled():
        try:
            await query.answer("البوت في وضع الصيانة", show_alert=True)
        except Exception:
            pass
        await send_or_edit(query, render_bot_text('bot.services.v18_features.py:919:message','{v0}', [str(maintenance_message())]))
        return

    if data.startswith("cust_trusted_toggle:"):
        if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "customers_edit"):
            await query.answer("لا تملك صلاحية تعديل العملاء", show_alert=True)
            return
        try:
            customer_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("معرف العميل غير صالح", show_alert=True)
            return
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        current = bool(row["is_trusted"]) if "is_trusted" in row.keys() else False
        with DBX.connect() as con:
            changed = con.execute(
                "UPDATE customers SET is_trusted=?, updated_at=? WHERE id=?",
                (0 if current else 1, now_str(), customer_id),
            )
            con.commit()
        if changed.rowcount != 1:
            await query.answer("تعذر تحديث العميل", show_alert=True)
            return
        updated = DBX.get_customer(customer_id)
        new_value = bool(updated["is_trusted"])
        DBX.audit(
            user.id,
            actor_name(user),
            "SET_CUSTOMER_TRUST",
            "customer",
            customer_id,
            "trusted=1" if new_value else "trusted=0",
        )
        await query.answer("تم تعيين العميل موثوقًا" if new_value else "تم إلغاء صفة العميل الموثوق")
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:956:message','{v0}', [str(customer_detail_text(updated))]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_actions_kb(updated, int(user.id)),
        )
        return

    if data.startswith("av_confirm_field:"):
        state, payload = current_state(context)
        field = data.split(":", 1)[1]
        if state != "await_av_confirm_field" or field != str(payload.get("field") or ""):
            await query.answer("انتهت صلاحية التأكيد. أعد إدخال البيانات.", show_alert=True)
            return
        value = str(payload.get("value") or "")
        try:
            if field == "full_name":
                save_verification_full_name(int(user.id), value)
            elif field == "phone_primary":
                save_verification_primary_phone(int(user.id), value)
            elif field == "phone_alt":
                save_verification_alt_phone(int(user.id), value)
            else:
                raise ValueError("الحقل غير صالح")
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        set_state(context, None)
        await query.answer("تم حفظ البيانات")
        await show_account_onboarding(query, context)
        return

    if not (data == "adm_v18" or data.startswith("adm_v18_")):
        await _v18_previous_on_callback(update, context)
        return
    if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
        await query.answer("لا تملك صلاحية إعدادات التشغيل", show_alert=True)
        return

    await query.answer()
    if data == "adm_v18":
        set_state(context, None)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:997:message','{v0}', [str(_v18_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_v18_admin_kb())
        return
    if data == "adm_v18_maintenance":
        if maintenance_enabled():
            set_maintenance(False)
            DBX.audit(user.id, actor_name(user), "DISABLE_MAINTENANCE", "settings", "maintenance")
            await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1003:message','{v0}', [str(_v18_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_v18_admin_kb())
        else:
            set_state(context, "await_v18_maintenance_reason")
            await query.edit_message_text(
                render_bot_text('bot.services.v18_features.py:1006:message','<b>🔧 تفعيل وضع الصيانة</b>\n\nاكتب سبب الصيانة، أو اختر التفعيل بدون سبب.', []),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[btn(render_bot_button('bot.services.v18_features.py:1010:button','تفعيل بدون سبب', []), "adm_v18_maintenance_default", "danger")], [btn(render_bot_button('bot.services.v18_features.py:1010:button','إلغاء', []), "adm_v18")]]
                ),
            )
        return
    if data == "adm_v18_maintenance_default":
        set_maintenance(True)
        set_state(context, None)
        DBX.audit(user.id, actor_name(user), "ENABLE_MAINTENANCE", "settings", "maintenance", "default message")
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1018:message','{v0}', [str(_v18_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_v18_admin_kb())
        return
    if data == "adm_v18_approval_mode":
        value = "MANUAL" if account_approval_mode() == "AUTO" else "AUTO"
        DBX.set_setting("account_approval_mode", value)
        DBX.audit(user.id, actor_name(user), "SET_ACCOUNT_APPROVAL_MODE", "settings", "account_approval_mode", value)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1024:message','{v0}', [str(_v18_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_v18_admin_kb())
        return
    if data == "adm_v18_approval_delay":
        set_state(context, "await_v18_approval_delay")
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1028:message','أرسل مدة الانتظار بالثواني من 0 إلى 86400.', []), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1028:button','إلغاء', []), "adm_v18")]]))
        return
    if data == "adm_v18_verification_max":
        set_state(context, "await_v18_verification_max")
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1032:message','أرسل الحد الأعلى لمبلغ التحقق بالريال (من 100 إلى 5000).', []), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1032:button','إلغاء', []), "adm_v18")]]))
        return
    if data in {"adm_v18_channel", "adm_v18_channel_admin"}:
        set_state(context, "await_v18_channel_admin")
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1036:message','<b>🔐 تعيين قناة الأدمن الخاصة</b>\n\nأرسل Chat ID أو @username. ستصلها الإيداعات وطلبات التسجيل والموافقات والأخطاء والتنبيهات. يجب أن يكون البوت مشرفًا فيها.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1041:button','إلغاء', []), "adm_v18")]]),
        )
        return
    if data == "adm_v18_channel_users":
        set_state(context, "await_v18_channel_users")
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1046:message','<b>👥 تعيين قناة المستخدمين العامة</b>\n\nأرسل Chat ID أو @username. لن يصلها إلا إثبات عمليات السحب الناجحة. يجب أن يكون البوت مشرفًا فيها.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1051:button','إلغاء', []), "adm_v18")]]),
        )
        return
    if data == "adm_v18_channel_backup":
        set_state(context, "await_v18_channel_backup")
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1056:message','<b>💾 تعيين قناة النسخ الاحتياطية</b>\n\nأرسل Chat ID أو @username للقناة الخاصة بالنسخ فقط. يجب أن يكون البوت مشرفًا فيها.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1060:button','إلغاء', []), "adm_v18")]]),
        )
        return
    if data == "adm_v18_jaib_check":
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1064:message','⏳ <b>جاري فحص اتصال Jaib الآن...</b>\n\nلن يتم تنفيذ أي عملية مالية.', []),
            parse_mode=ParseMode.HTML,
        )
        result = await asyncio.to_thread(jaib_health_check)
        if result.get("ok"):
            body = (
                "<b>✅ اتصال Jaib يعمل بصورة صحيحة</b>\n\n"
                f"وقت الفحص: <code>{html(result['checked_at'])}</code>\n"
                f"زمن الاستجابة: <code>{int(result['response_ms'])} ms</code>\n"
                "نوع الفحص: تهيئة وتسجيل دخول فقط — دون أي عملية مالية."
            )
        else:
            body = (
                "<b>❌ اتصال Jaib لا يعمل</b>\n\n"
                f"السبب الآمن: <code>{html(result['reason'])}</code>\n"
                f"وقت الفحص: <code>{html(result['checked_at'])}</code>\n\n"
                "سيستمر الفحص التلقائي، وسيصل إشعار عند عودة الاتصال."
            )
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1083:message','{v0}', [str(body)]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1086:button','🔄 إعادة الفحص', []), "adm_v18_jaib_check"), btn(render_bot_button('bot.services.v18_features.py:1086:button','↩️ رجوع', []), "adm_v18")]]),
        )
        return
    if data == "adm_v18_networks":
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1090:message','<b>🌐 شبكات USDT</b>\n\nاضغط شبكة لإدارتها.', []), parse_mode=ParseMode.HTML, reply_markup=_network_list_kb())
        return
    if data.startswith("adm_v18_network:"):
        network_id = int(data.split(":", 1)[1])
        with DBX.connect() as con:
            row = con.execute("SELECT * FROM managed_networks WHERE id=?", (network_id,)).fetchone()
        if not row:
            await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1097:message','الشبكة غير موجودة.', []), reply_markup=_network_list_kb())
            return
        status = bool(int(row["enabled"] or 0))
        fee = row["fee_override"] or "ديناميكية من Binance"
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1101:message','<b>🌐 إعداد الشبكة</b>\n\nالاسم: <b>{v0}</b>\nالرمز: <code>{v1}</code>\nالحالة: <b>{v2}</b>\nعنوان الإيداع: <code>{v3}</code>\nالرسوم الفعلية البديلة: <b>{v4}</b>', [f"{html(row['display_label'])}",f"{html(row['network_code'])}",f"{('مفعلة' if status else 'متوقفة')}",f"{html(row['wallet_address'] or 'غير محدد')}",f'{html(fee)}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(render_bot_button('bot.services.v18_features.py:1111:button','{v0}', [str('إيقاف الشبكة' if status else 'تفعيل الشبكة')]), f"adm_v18_network_toggle:{network_id}", "danger" if status else "success")],
                    [btn(render_bot_button('bot.services.v18_features.py:1112:button','✏️ عنوان المحفظة', []), f"adm_v18_network_wallet:{network_id}"), btn(render_bot_button('bot.services.v18_features.py:1112:button','💸 رسوم بديلة', []), f"adm_v18_network_fee:{network_id}")],
                    [btn(render_bot_button('bot.services.v18_features.py:1113:button','↩️ الشبكات', []), "adm_v18_networks")],
                ]
            ),
        )
        return
    if data.startswith("adm_v18_network_toggle:"):
        network_id = int(data.split(":", 1)[1])
        with DBX.connect() as con:
            row = con.execute("SELECT enabled FROM managed_networks WHERE id=?", (network_id,)).fetchone()
        if not row:
            raise ValueError("الشبكة غير موجودة")
        set_managed_network_enabled(network_id, not bool(int(row["enabled"] or 0)))
        DBX.audit(user.id, actor_name(user), "TOGGLE_MANAGED_NETWORK", "managed_network", network_id)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1126:message','✅ تم تحديث حالة الشبكة.', []), reply_markup=_network_list_kb())
        return
    if data.startswith("adm_v18_network_wallet:"):
        network_id = int(data.split(":", 1)[1])
        set_state(context, "await_v18_network_wallet", network_id=network_id)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1131:message','أرسل عنوان محفظة الإيداع لهذه الشبكة، أو أرسل - لمسحه.', []), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1131:button','إلغاء', []), f"adm_v18_network:{network_id}")]]))
        return
    if data.startswith("adm_v18_network_fee:"):
        network_id = int(data.split(":", 1)[1])
        set_state(context, "await_v18_network_fee", network_id=network_id)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1136:message','أرسل الرسوم الفعلية البديلة، أو أرسل - لاستخدام الرسوم الحية.', []), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_features.py:1136:button','إلغاء', []), f"adm_v18_network:{network_id}")]]))
        return
    if data == "adm_v18_assets":
        await query.edit_message_text(
            render_bot_text('bot.services.v18_features.py:1139:message','<b>🪙 العملات الرقمية</b>\n\nUSDT هي عملة المحفظة المالية الحالية. العملات الإضافية تُدار من الكتالوج وتظل متوقفة حتى تهيئة أرصدتها وأسعارها.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=_asset_list_kb(),
        )
        return
    if data.startswith("adm_v18_asset:"):
        symbol = data.split(":", 1)[1].upper()
        with DBX.connect() as con:
            row = con.execute("SELECT * FROM managed_assets WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1150:message','العملة غير موجودة.', []), reply_markup=_asset_list_kb())
            return
        if symbol == "USDT" and int(row["enabled"] or 0):
            await query.edit_message_text(
                render_bot_text('bot.services.v18_features.py:1153:message','⚠️ لا يمكن إيقاف USDT لأنها عملة أرصدة المستخدمين الحالية.', []),
                reply_markup=_asset_list_kb(),
            )
            return
        set_managed_asset_enabled(symbol, not bool(int(row["enabled"] or 0)))
        DBX.audit(user.id, actor_name(user), "TOGGLE_MANAGED_ASSET", "managed_asset", symbol)
        await query.edit_message_text(render_bot_text('bot.services.v18_features.py:1160:message','✅ تم تحديث كتالوج العملات.', []), reply_markup=_asset_list_kb())
        return

    await _v18_previous_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    state, payload = current_state(context)
    if user is not None and not DBX.is_admin(int(user.id)) and maintenance_enabled():
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_features.py:1170:message','{v0}', [str(maintenance_message())]), parse_mode=ParseMode.HTML)
        return
    if state not in {
        "await_v18_maintenance_reason",
        "await_v18_approval_delay",
        "await_v18_verification_max",
        "await_v18_channel_users",
        "await_v18_channel_admin",
        "await_v18_channel_backup",
        "await_v18_network_wallet",
        "await_v18_network_fee",
    }:
        await _v18_previous_on_message(update, context)
        return
    if not user or not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
        set_state(context, None)
        return

    text_value = str(update.effective_message.text or "").strip()
    try:
        if state == "await_v18_maintenance_reason":
            set_maintenance(True, text_value)
            DBX.audit(user.id, actor_name(user), "ENABLE_MAINTENANCE", "settings", "maintenance", text_value)
        elif state == "await_v18_approval_delay":
            seconds = int(text_value.translate(ARABIC_DIGITS))
            if not 0 <= seconds <= 86400:
                raise ValueError("المدة يجب أن تكون بين 0 و86400 ثانية")
            DBX.set_setting("account_auto_approval_delay_seconds", str(seconds))
            DBX.audit(user.id, actor_name(user), "SET_AUTO_APPROVAL_DELAY", "settings", "account_auto_approval_delay_seconds", seconds)
        elif state == "await_v18_verification_max":
            amount = int(text_value.translate(ARABIC_DIGITS))
            if not 100 <= amount <= 5000:
                raise ValueError("الحد يجب أن يكون بين 100 و5000 ريال")
            DBX.set_setting("account_verification_max_yer", str(amount))
            DBX.audit(user.id, actor_name(user), "SET_VERIFICATION_MAX", "settings", "account_verification_max_yer", amount)
        elif state in {
            "await_v18_channel_users",
            "await_v18_channel_admin",
            "await_v18_channel_backup",
        }:
            identifier: int | str = int(text_value) if text_value.lstrip("-").isdigit() else text_value
            if not isinstance(identifier, int) and not str(identifier).startswith("@"):
                raise ValueError("أرسل Chat ID رقميًا أو @username")
            chat = await context.bot.get_chat(identifier)
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            member_status = str(getattr(member, "status", "")).lower()
            if not any(value in member_status for value in ("administrator", "creator", "owner")):
                raise ValueError("يجب أن يكون البوت مشرفًا داخل القناة قبل حفظها")
            title = getattr(chat, "title", None) or str(chat.id)
            if state == "await_v18_channel_users":
                set_users_channel(str(chat.id), title)
                action = "SET_CHANNEL_USERS"
            elif state == "await_v18_channel_backup":
                set_project_backup_channel(str(chat.id), title)
                action = "SET_CHANNEL_BACKUP"
            else:
                set_admin_channel(str(chat.id), title)
                action = "SET_CHANNEL_ADMIN"
            DBX.audit(user.id, actor_name(user), action, "settings", str(chat.id), title)
        elif state == "await_v18_network_wallet":
            network_id = int(payload.get("network_id") or 0)
            update_managed_network(network_id, wallet_address="" if text_value == "-" else text_value)
            DBX.audit(user.id, actor_name(user), "SET_NETWORK_WALLET", "managed_network", network_id)
        elif state == "await_v18_network_fee":
            network_id = int(payload.get("network_id") or 0)
            update_managed_network(network_id, fee_override="" if text_value == "-" else text_value)
            DBX.audit(user.id, actor_name(user), "SET_NETWORK_FEE_OVERRIDE", "managed_network", network_id)
        set_state(context, None)
        await update.effective_message.reply_text(
            render_bot_text('bot.services.v18_features.py:1238:message','✅ تم حفظ الإعداد.', []),
            reply_markup=_v18_admin_kb(),
        )
    except (TypeError, ValueError) as exc:
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_features.py:1243:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("V18 admin message failed state=%s admin=%s", state, user.id)
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_features.py:1246:message','⚠️ تعذر حفظ الإعداد. تحقق من القيمة وصلاحيات البوت ثم حاول مجددًا.', []))


_export_globals(globals())

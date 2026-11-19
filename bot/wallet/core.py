"""Internal-wallet settings, schema, and balance access.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# طبقة المحفظة الداخلية: إيداع بالريال وسحب عبر المزود المفعّل
# ============================================================

# قواعد النظام الجديدة المتفق عليها:
# - لكل مستخدم رصيد متاح ورصيد محجوز بالدولار.
# - سعر الصرف يثبت عند إنشاء الإيداع.
# - حد Binance الأدنى ديناميكي، ورسوم العميل صفر.
# - مبلغ السحب يحجز قبل الإرسال ويثبت عند النجاح أو يعاد عند الفشل المؤكد.
# - Binance هو مزود السحب الوحيد على BSC/BEP20.
# - الأخطاء التقنية لا تظهر للعميل؛ التفاصيل تذهب إلى قناة تقنية مستقلة.

WALLET_MIN_WITHDRAW_USDT = max(Decimal("0"), Decimal(env("WALLET_MIN_WITHDRAW_USDT", "0")))
WALLET_CUSTOMER_FEE_USDT = Decimal("0")
WALLET_MAX_WITHDRAW_USDT = Decimal(env("WALLET_MAX_WITHDRAW_USDT", "1000"))
WALLET_MIN_DEPOSIT_YER = max(Decimal("5"), Decimal(env("WALLET_MIN_DEPOSIT_YER", "5")))
WALLET_RECENT_LIMIT = int(env("WALLET_RECENT_LIMIT", "10"))
WALLET_WITHDRAW_ENABLED_DEFAULT = env("WALLET_WITHDRAW_ENABLED_DEFAULT", "true").lower() == "true"
WALLET_DEPOSIT_ENABLED_DEFAULT = env("WALLET_DEPOSIT_ENABLED_DEFAULT", "true").lower() == "true"
WALLET_AUTO_RETRY_MINUTES = int(env("WALLET_AUTO_RETRY_MINUTES", "3"))

BEP20_NETWORK_CODE = env("BEP20_NETWORK_CODE", "BSC").upper()
BEP20_NETWORK_LABEL = env("BEP20_NETWORK_LABEL", "BNB Smart Chain (BEP20)")
BEP20_ENABLED = env("BEP20_ENABLED", "true").lower() == "true"
BEP20_MAX_SYSTEM_FEE_USDT = Decimal(env("BEP20_MAX_SYSTEM_FEE_USDT", "0.50"))

ADMIN_PERMISSION_LABELS.update(
    {
        "wallet_view": "💰 عرض المحافظ والأرصدة",
        "wallet_adjust": "➕➖ تعديل أرصدة العملاء",
        "wallet_transactions": "📒 عرض سجل المحفظة",
        "wallet_settings": "⚙️ إعدادات الإيداع والسحب",
        "wallet_retry": "🔁 إعادة محاولة السحب",
        "technical_channel_manage": "🚨 إدارة قناة الردود التقنية",
        "payout_wallet_manage": "👛 إدارة محفظة الصرف",
    }
)
ALL_ADMIN_PERMISSIONS = tuple(ADMIN_PERMISSION_LABELS)


class WalletUserError(Exception):
    """خطأ يمكن عرضه للعميل بصياغته الحالية."""


class WalletTechnicalError(Exception):
    """خطأ تقني يجب إخفاؤه عن العميل وإرساله للإدارة."""


class WalletUnknownResultError(WalletTechnicalError):
    """قد تكون المعاملة أرسلت، لذلك لا يعاد الرصيد تلقائياً."""


def wallet_ref(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%y%m%d%H%M')}-{uuid.uuid4().hex[:8].upper()}"


def wallet_decimal(value: Any) -> Decimal:
    result = decimal_or_zero(value)
    return result.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def wallet_decimal_text(value: Any) -> str:
    return q8(wallet_decimal(value))


def wallet_mask(value: Any, start: int = 6, end: int = 6) -> str:
    text = str(value or "")
    if len(text) <= start + end + 3:
        return text or "—"
    return f"{text[:start]}…{text[-end:]}"


def wallet_user_status_message(reference: str) -> str:
    return (
        "⚠️ تعذر إكمال العملية حالياً بسبب مشكلة تقنية.\n\n"
        "لم يتم فقدان رصيدك، وسيتم التحقق من العملية قبل اتخاذ أي إجراء.\n"
        f"رقم المرجع: <code>{html(reference)}</code>"
    )


def wallet_deposit_enabled() -> bool:
    return (DBX.get_setting("wallet_deposit_enabled") or ("1" if WALLET_DEPOSIT_ENABLED_DEFAULT else "0")) == "1"


def wallet_withdraw_enabled() -> bool:
    return (DBX.get_setting("wallet_withdraw_enabled") or ("1" if WALLET_WITHDRAW_ENABLED_DEFAULT else "0")) == "1"


def wallet_set_deposit_enabled(value: bool) -> None:
    DBX.set_setting("wallet_deposit_enabled", "1" if value else "0")


def wallet_set_withdraw_enabled(value: bool) -> None:
    DBX.set_setting("wallet_withdraw_enabled", "1" if value else "0")


def wallet_auto_withdraw_enabled() -> bool:
    return True


def wallet_set_auto_withdraw_enabled(value: bool) -> None:
    # احتفاظ بالاسم للتوافق مع النسخ القديمة؛ السحب في V17 تلقائي دائمًا.
    DBX.set_setting("wallet_auto_withdraw_enabled", "1")


def _wallet_enqueue_notification(
    con: sqlite3.Connection,
    *,
    event_key: str,
    telegram_user_id: int,
    notification_type: str,
    payload: dict[str, Any],
) -> int:
    """إضافة إشعار مالي إلى صندوق إرسال ذري داخل نفس المعاملة المالية."""
    cur = con.execute(
        """
        INSERT OR IGNORE INTO wallet_notification_outbox(
            event_key, telegram_user_id, notification_type, payload_json, status,
            attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
        """,
        (
            str(event_key),
            int(telegram_user_id),
            str(notification_type),
            json.dumps(payload, ensure_ascii=False, default=str),
            now_str(),
            now_str(),
        ),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = con.execute(
        "SELECT id FROM wallet_notification_outbox WHERE event_key=?",
        (str(event_key),),
    ).fetchone()
    return int(row["id"]) if row else 0


def wallet_enqueue_user_notification(
    *,
    event_key: str,
    telegram_user_id: int,
    notification_type: str,
    text: str,
) -> int:
    """Queue an idempotent Telegram notice from bot or Mini App decisions."""
    with DBX.connect() as con:
        notification_id = _wallet_enqueue_notification(
            con,
            event_key=event_key,
            telegram_user_id=int(telegram_user_id),
            notification_type=notification_type,
            payload={
                "text": render_bot_text('notice.bot.wallet.core.py:163','{v0}',[str(str(text))]),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        con.commit()
        return notification_id


def wallet_pending_notifications(*, limit: int = 100, telegram_user_id: Optional[int] = None) -> list[sqlite3.Row]:
    with DBX.connect() as con:
        # إعادة الإشعار الذي توقف أثناء الإرسال بسبب إعادة تشغيل مفاجئة.
        con.execute(
            "UPDATE wallet_notification_outbox SET status='PENDING', updated_at=? "
            "WHERE status='SENDING' AND updated_at < datetime('now', '-10 minutes')",
            (now_str(),),
        )
        con.commit()
        if telegram_user_id is None:
            return con.execute(
                "SELECT * FROM wallet_notification_outbox WHERE status='PENDING' ORDER BY id ASC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return con.execute(
            "SELECT * FROM wallet_notification_outbox WHERE status='PENDING' AND telegram_user_id=? ORDER BY id ASC LIMIT ?",
            (int(telegram_user_id), max(1, min(int(limit), 500))),
        ).fetchall()


def wallet_claim_notification(notification_id: int) -> bool:
    """حجز الإشعار ذريًا لمنع إرساله مرتين من البوت والميني آب."""
    with DBX.connect() as con:
        changed = con.execute(
            "UPDATE wallet_notification_outbox SET status='SENDING', updated_at=? WHERE id=? AND status='PENDING'",
            (now_str(), int(notification_id)),
        )
        con.commit()
        return changed.rowcount == 1


def wallet_mark_notification_sent(notification_id: int) -> None:
    with DBX.connect() as con:
        con.execute(
            "UPDATE wallet_notification_outbox SET status='SENT', sent_at=?, updated_at=?, last_error=NULL WHERE id=?",
            (now_str(), now_str(), int(notification_id)),
        )
        con.commit()


def wallet_mark_notification_failed(notification_id: int, error: Any) -> None:
    with DBX.connect() as con:
        con.execute(
            "UPDATE wallet_notification_outbox SET status='PENDING', attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
            (str(error)[:1000], now_str(), int(notification_id)),
        )
        con.commit()


def _wallet_schema() -> None:
    with DBX.connect() as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_wallets (
                telegram_user_id INTEGER PRIMARY KEY,
                customer_id INTEGER,
                available_balance TEXT NOT NULL DEFAULT '0',
                locked_balance TEXT NOT NULL DEFAULT '0',
                total_deposited TEXT NOT NULL DEFAULT '0',
                total_withdrawn TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deposit_code TEXT UNIQUE NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                customer_id INTEGER,
                fiat_amount_yer TEXT NOT NULL,
                exchange_rate_yer TEXT NOT NULL,
                usdt_amount TEXT NOT NULL,
                bank_transaction_id TEXT UNIQUE,
                payer_display TEXT,
                payer_phone TEXT,
                payer_match_source TEXT,
                payment_time TEXT,
                status TEXT NOT NULL,
                review_reason TEXT,
                raw_json TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            )
            """
        )
        deposit_columns = {row[1] for row in con.execute("PRAGMA table_info(wallet_deposits)").fetchall()}
        for column, declaration in {
            "payer_name": "TEXT",
            "payer_phone_alt": "TEXT",
            "payer_identifiers_json": "TEXT",
            "amount_source": "TEXT",
            "reference_submitted_at": "TEXT",
        }.items():
            if column not in deposit_columns:
                con.execute(f"ALTER TABLE wallet_deposits ADD COLUMN {column} {declaration}")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                withdrawal_code TEXT UNIQUE NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                customer_id INTEGER,
                amount_usdt TEXT NOT NULL,
                customer_fee_usdt TEXT NOT NULL DEFAULT '0',
                network_code TEXT NOT NULL,
                network_label TEXT NOT NULL,
                destination_address TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                request_id TEXT UNIQUE NOT NULL,
                provider_withdraw_id TEXT,
                txid TEXT,
                network_fee_asset TEXT,
                network_fee_amount TEXT,
                error_code TEXT,
                error_message TEXT,
                raw_json TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                submitted_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            )
            """
        )
        withdrawal_columns = {row[1] for row in con.execute("PRAGMA table_info(wallet_withdrawals)").fetchall()}
        submission_column_was_missing = "submission_started_at" not in withdrawal_columns
        for column, declaration in {
            "expires_at": "TEXT",
            "processing_started_at": "TEXT",
            "submission_started_at": "TEXT",
            "last_checked_at": "TEXT",
            "next_check_at": "TEXT",
            "monitor_error_count": "INTEGER NOT NULL DEFAULT 0",
            "last_monitor_error": "TEXT",
            "last_monitor_alert_at": "TEXT",
            "recovered_at": "TEXT",
            "channel_notified": "INTEGER NOT NULL DEFAULT 0",
            "channel_notify_attempts": "INTEGER NOT NULL DEFAULT 0",
            "channel_notify_last_error": "TEXT",
        }.items():
            if column not in withdrawal_columns:
                con.execute(f"ALTER TABLE wallet_withdrawals ADD COLUMN {column} {declaration}")

        if submission_column_was_missing:
            # حالات PROCESSING القديمة سبقت وجود علامة بدء الإرسال؛ نعاملها تحفظيًا
            # كعمليات قد تكون وصلت إلى المزود حتى لا يعاد رصيدها أو إرسالها تلقائيًا.
            con.execute(
                """
                UPDATE wallet_withdrawals
                SET submission_started_at=COALESCE(submitted_at, updated_at, created_at),
                    recovered_at=COALESCE(recovered_at, ?), updated_at=updated_at
                WHERE status IN ('PROCESSING','STATUS_UNKNOWN','MANUAL_REVIEW')
                  AND submission_started_at IS NULL
                """,
                (now_str(),),
            )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ledger_code TEXT UNIQUE NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                customer_id INTEGER,
                transaction_type TEXT NOT NULL,
                status TEXT NOT NULL,
                amount_usdt TEXT NOT NULL,
                fiat_amount_yer TEXT,
                exchange_rate_yer TEXT,
                available_before TEXT NOT NULL,
                available_after TEXT NOT NULL,
                locked_before TEXT NOT NULL,
                locked_after TEXT NOT NULL,
                deposit_id INTEGER,
                withdrawal_id INTEGER,
                bank_transaction_id TEXT,
                network_code TEXT,
                destination_address TEXT,
                txid TEXT,
                admin_id INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(deposit_id) REFERENCES wallet_deposits(id),
                FOREIGN KEY(withdrawal_id) REFERENCES wallet_withdrawals(id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                notification_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS technical_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_id TEXT,
                severity TEXT NOT NULL,
                event_type TEXT NOT NULL,
                telegram_user_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_deposits_user ON wallet_deposits(telegram_user_id, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_deposits_status ON wallet_deposits(status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_withdrawals_user ON wallet_withdrawals(telegram_user_id, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_withdrawals_status ON wallet_withdrawals(status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_withdrawals_due ON wallet_withdrawals(status, next_check_at, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_withdrawals_expiry ON wallet_withdrawals(status, expires_at, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ledger_user ON wallet_ledger(telegram_user_id, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_notification_status ON wallet_notification_outbox(status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wallet_notification_user ON wallet_notification_outbox(telegram_user_id, status, id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_technical_events_ref ON technical_events(reference_id, id DESC)")
        con.commit()

    defaults = {
        "wallet_deposit_enabled": "1" if WALLET_DEPOSIT_ENABLED_DEFAULT else "0",
        "wallet_withdraw_enabled": "1" if WALLET_WITHDRAW_ENABLED_DEFAULT else "0",
        "wallet_auto_withdraw_enabled": "1",
        "display_decimals": str(int(globals().get("DISPLAY_DECIMALS_DEFAULT", 4))),
        "technical_channel_enabled": "1",
    }
    for key, value in defaults.items():
        if DBX.get_setting(key) is None:
            DBX.set_setting(key, value)


_wallet_schema()


def wallet_ensure(telegram_user_id: int, customer_id: Optional[int] = None) -> sqlite3.Row:
    with DBX.connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO user_wallets(
                telegram_user_id, customer_id, available_balance, locked_balance,
                total_deposited, total_withdrawn, created_at, updated_at
            ) VALUES (?, ?, '0', '0', '0', '0', ?, ?)
            """,
            (int(telegram_user_id), customer_id, now_str(), now_str()),
        )
        if customer_id is not None:
            con.execute(
                "UPDATE user_wallets SET customer_id = COALESCE(customer_id, ?), updated_at = ? WHERE telegram_user_id = ?",
                (customer_id, now_str(), int(telegram_user_id)),
            )
        con.commit()
        return con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (int(telegram_user_id),)).fetchone()


def wallet_get(telegram_user_id: int) -> sqlite3.Row:
    customer = DBX.get_customer_by_telegram(int(telegram_user_id))
    return wallet_ensure(int(telegram_user_id), int(customer["id"]) if customer else None)


def wallet_balances(telegram_user_id: int) -> tuple[Decimal, Decimal]:
    row = wallet_get(telegram_user_id)
    return wallet_decimal(row["available_balance"]), wallet_decimal(row["locked_balance"])




_export_globals(globals())

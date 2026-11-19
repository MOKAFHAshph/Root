"""التسجيل الآلي وتحقق Jaib للمستخدمين الجدد.

تحتفظ هذه الوحدة بحالة التوثيق المشتركة بين بوت Telegram والتطبيق المصغر.
تُخزّن بيانات الهوية في SQLite بدل الاعتماد على ``context.user_data`` حتى لا
تضيع الطلبات الجارية عند إعادة تشغيل المشروع.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# مبلغ التحقق من 100 YER إلى حد أعلى يديره الأدمن (300 افتراضيًا).
ACCOUNT_VERIFICATION_MIN_YER = 100
ACCOUNT_VERIFICATION_MAX_YER = max(
    ACCOUNT_VERIFICATION_MIN_YER,
    int(env("ACCOUNT_VERIFICATION_MAX_YER", "300")),
)
ACCOUNT_VERIFICATION_STEP_YER = 50
ACCOUNT_VERIFICATION_CHANNEL_ID = env("ACCOUNT_VERIFICATION_CHANNEL_ID", "").strip()
ACCOUNT_VERIFICATION_RECIPIENT_PHONE = env(
    "ACCOUNT_VERIFICATION_RECIPIENT_PHONE", str(JAIB_SECONDARY_PHONE or JAIB_PRIMARY_PHONE or "")
).strip()
ACCOUNT_VERIFICATION_RECIPIENT_NAME = env(
    "ACCOUNT_VERIFICATION_RECIPIENT_NAME",
    str(globals().get("JAIB_SECONDARY_BENEFICIARY_NAME", "عبدالرحمن علي") or "عبدالرحمن علي"),
).strip()
NETWORK_ADDRESS_VIDEO_URL = env("NETWORK_ADDRESS_VIDEO_URL", "").strip()
BOT_USAGE_GUIDE_URL = env("BOT_USAGE_GUIDE_URL", "").strip()

_REQUEST_FINAL_STATES = {"APPROVED", "REJECTED"}
_REQUEST_LOCKED_STATES = {
    "PENDING_REVIEW",
    "APPROVED",
    "REJECTED_PENDING_REFUND",
    "REFUNDED",
}
_REQUEST_EDITABLE_STATES = {"DRAFT", "REJECTED", "NEW"}
_FIELD_LABELS = {
    "full_name": "الاسم الرباعي",
    "phone_primary": "الرقم الأساسي",
    "phone_alt": "رقم Jaib البديل",
    "verification": "عملية التحقق",
}


def verification_recipient_phone() -> str:
    return str(DBX.get_setting("account_verification_recipient_phone") or ACCOUNT_VERIFICATION_RECIPIENT_PHONE or "").strip()


def verification_recipient_name() -> str:
    return str(DBX.get_setting("account_verification_recipient_name") or ACCOUNT_VERIFICATION_RECIPIENT_NAME or "عبدالرحمن علي").strip()


def network_address_video_url() -> str:
    return str(DBX.get_setting("network_address_video_url") or NETWORK_ADDRESS_VIDEO_URL or "").strip()


def bot_usage_guide_url() -> str:
    return str(DBX.get_setting("bot_usage_guide_url") or BOT_USAGE_GUIDE_URL or "").strip()


def _verification_schema() -> None:
    with DBX.connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS account_verification_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_code TEXT UNIQUE NOT NULL,
                telegram_id INTEGER UNIQUE NOT NULL,
                customer_id INTEGER,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                full_name TEXT,
                phone_primary TEXT,
                phone_alt TEXT,
                verification_reference TEXT UNIQUE,
                verification_amount_yer TEXT,
                verification_received_amount_yer TEXT,
                verification_exchange_rate_yer TEXT,
                verification_credit_usdt TEXT,
                verification_credit_ledger_code TEXT,
                verification_credited_at TEXT,
                verification_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
                verification_started_at TEXT,
                payer_display TEXT,
                payer_phone TEXT,
                payment_time TEXT,
                payment_raw_json TEXT,
                requested_at TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                rejection_reason TEXT,
                needs_fields_json TEXT NOT NULL DEFAULT '[]',
                refund_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
                refund_proof_file_id TEXT,
                refunded_at TEXT,
                review_chat_id TEXT,
                review_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # ترقية قواعد البيانات القديمة دون حذف أي طلب موجود.
        existing_columns = {
            str(item["name"])
            for item in con.execute("PRAGMA table_info(account_verification_requests)").fetchall()
        }
        migrations = {
            "verification_received_amount_yer": "TEXT",
            "verification_exchange_rate_yer": "TEXT",
            "verification_credit_usdt": "TEXT",
            "verification_credit_ledger_code": "TEXT",
            "verification_credited_at": "TEXT",
        }
        for column, sql_type in migrations.items():
            if column not in existing_columns:
                con.execute(f"ALTER TABLE account_verification_requests ADD COLUMN {column} {sql_type}")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_verification_status "
            "ON account_verification_requests(status, id DESC)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_verification_credit_ledger "
            "ON account_verification_requests(verification_credit_ledger_code) "
            "WHERE verification_credit_ledger_code IS NOT NULL AND verification_credit_ledger_code != ''"
        )
        con.commit()


_verification_schema()


def _request_code() -> str:
    return wallet_ref("VR")


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row else None


def _verification_amount_choices() -> tuple[int, ...]:
    """إرجاع القيم المسموح بها لمبلغ التحقق بصورة آمنة."""
    minimum = ACCOUNT_VERIFICATION_MIN_YER
    maximum = (
        account_verification_max_yer()
        if callable(globals().get("account_verification_max_yer"))
        else ACCOUNT_VERIFICATION_MAX_YER
    )
    step = ACCOUNT_VERIFICATION_STEP_YER
    if minimum < 1 or maximum < minimum or step < 1:
        minimum, maximum, step = 100, 300, 50
    values = tuple(range(minimum, maximum + 1, step))
    return values or (100, 150, 200, 250, 300)


def _new_verification_amount() -> Decimal:
    return Decimal(secrets.choice(_verification_amount_choices())).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _current_verification_rate(telegram_id: Optional[int] = None) -> Decimal:
    rate = wallet_decimal(effective_customer_rate(telegram_id)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if rate <= 0:
        raise ValueError("سعر الصرف الحالي غير صالح لإنشاء طلب التحقق.")
    return rate


def _request_expected_amount(row: sqlite3.Row | dict[str, Any]) -> Decimal:
    value = wallet_decimal(row["verification_amount_yer"] or 0)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _request_exchange_rate(row: sqlite3.Row | dict[str, Any]) -> Decimal:
    value = wallet_decimal(row["verification_exchange_rate_yer"] or 0)
    if value <= 0:
        value = _current_verification_rate(int(row["telegram_id"]))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _request_credit_usdt(row: sqlite3.Row | dict[str, Any]) -> Decimal:
    amount = _request_expected_amount(row)
    rate = _request_exchange_rate(row)
    if amount <= 0 or rate <= 0:
        return Decimal("0")
    return (amount / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _ensure_request_terms(row: sqlite3.Row) -> sqlite3.Row:
    """تثبيت المبلغ العشوائي وسعر الصرف مرة واحدة لكل طلب."""
    amount = _request_expected_amount(row)
    rate = wallet_decimal(row["verification_exchange_rate_yer"] or 0)
    if amount > 0 and rate > 0:
        return row
    amount = amount if amount > 0 else _new_verification_amount()
    rate = rate if rate > 0 else _current_verification_rate(int(row["telegram_id"]))
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM account_verification_requests WHERE id=?",
            (int(row["id"]),),
        ).fetchone()
        if not current:
            con.rollback()
            raise ValueError("طلب التوثيق غير موجود.")
        current_amount = wallet_decimal(current["verification_amount_yer"] or 0)
        current_rate = wallet_decimal(current["verification_exchange_rate_yer"] or 0)
        if current_amount <= 0:
            current_amount = amount
        if current_rate <= 0:
            current_rate = rate
        con.execute(
            """
            UPDATE account_verification_requests
            SET verification_amount_yer=?, verification_exchange_rate_yer=?, updated_at=?
            WHERE id=?
            """,
            (q2(current_amount), q2(current_rate), now_str(), int(current["id"])),
        )
        con.commit()
    return get_account_verification_by_id(int(row["id"]))


def get_account_verification(telegram_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM account_verification_requests WHERE telegram_id=?",
            (int(telegram_id),),
        ).fetchone()


def get_account_verification_by_id(request_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM account_verification_requests WHERE id=?",
            (int(request_id),),
        ).fetchone()


def ensure_account_verification(user: Any) -> sqlite3.Row:
    telegram_id = int(user.id)
    current = get_account_verification(telegram_id)
    if current:
        return _ensure_request_terms(current)
    now = now_str()
    amount = _new_verification_amount()
    rate = _current_verification_rate(telegram_id)
    with DBX.connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO account_verification_requests(
                request_code, telegram_id, status, verification_amount_yer,
                verification_exchange_rate_yer, created_at, updated_at
            ) VALUES (?, ?, 'DRAFT', ?, ?, ?, ?)
            """,
            (_request_code(), telegram_id, q2(amount), q2(rate), now, now),
        )
        con.commit()
    current = get_account_verification(telegram_id)
    if not current:
        raise ValueError("تعذر إنشاء طلب التوثيق.")
    return _ensure_request_terms(current)


def _needs_fields(row: sqlite3.Row | dict[str, Any]) -> list[str]:
    try:
        return [str(x) for x in json.loads(row["needs_fields_json"] or "[]") if str(x) in _FIELD_LABELS]
    except Exception:
        return []


def _is_field_editable(row: sqlite3.Row, field: str) -> bool:
    status = str(row["status"] or "DRAFT").upper()
    if status in {"DRAFT", "READY_TO_SUBMIT", "NEW", "NEEDS_INFORMATION", "PENDING_REVIEW", "REJECTED"}:
        if field == "verification":
            return str(row["verification_status"] or "NOT_STARTED").upper() != "VERIFIED"
        return True
    return False


def normalize_yemen_mobile(value: str) -> str:
    digits = normalize_phone(value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("967"):
        digits = digits[3:]
    if digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"7[1378]\d{7}", digits):
        raise ValueError("أدخل رقمًا يمنيًا صحيحًا من 9 خانات يبدأ بـ 71 أو 73 أو 77 أو 78.")
    return digits


def normalize_jaib_alternate(value: str) -> str:
    """التحقق من رقم Jaib مع حفظ الصفر البادئ حرفيًا."""
    digits = normalize_phone(value)
    if len(digits) == 9 and not digits.startswith("0"):
        if not re.fullmatch(r"7[1378]\d{7}", digits):
            raise ValueError("رقم Jaib البديل بصيغة الهاتف يجب أن يبدأ بـ 71 أو 73 أو 77 أو 78.")
    elif not re.fullmatch(r"\d{5,20}", digits):
        raise ValueError("أدخل رقم Jaib البديل الصحيح (من 5 إلى 20 خانة رقمية).")
    return digits


def normalize_full_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    parts = [part for part in cleaned.split(" ") if part]
    if len(parts) < 4:
        raise ValueError("يرجى إعادة المحاولة – الاسم يجب أن يكون رباعيًا.")
    if any(any(ch.isdigit() for ch in part) for part in parts):
        raise ValueError("الاسم الرباعي لا يجب أن يحتوي على أرقام.")
    if len(cleaned) > 140:
        raise ValueError("الاسم طويل جدًا.")
    return cleaned


def _phone_identity_key(value: Any) -> str:
    """توحيد الأرقام القديمة والجديدة قبل مقارنة التكرار."""
    digits = normalize_phone(value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("967"):
        digits = digits[3:]
    if digits.startswith("0") and len(digits) > 5:
        digits = digits[1:]
    return digits


def _phone_used_by_other(con: sqlite3.Connection, value: str, telegram_id: int) -> bool:
    key = _phone_identity_key(value)
    if not key:
        return False
    customer_rows = con.execute(
        """
        SELECT telegram_id, phone_primary, phone_alt
        FROM customers
        WHERE COALESCE(phone_primary, '') != '' OR COALESCE(phone_alt, '') != ''
        """
    ).fetchall()
    for item in customer_rows:
        owner = int(item["telegram_id"] or 0)
        if owner == int(telegram_id):
            continue
        if key in {_phone_identity_key(item["phone_primary"]), _phone_identity_key(item["phone_alt"])}:
            return True

    request_rows = con.execute(
        """
        SELECT telegram_id, phone_primary, phone_alt
        FROM account_verification_requests
        WHERE telegram_id != ?
          AND (COALESCE(phone_primary, '') != '' OR COALESCE(phone_alt, '') != '')
        """,
        (int(telegram_id),),
    ).fetchall()
    for item in request_rows:
        if key in {_phone_identity_key(item["phone_primary"]), _phone_identity_key(item["phone_alt"])}:
            return True
    return False


def _save_field(telegram_id: int, field: str, value: str) -> sqlite3.Row:
    if field not in {"full_name", "phone_primary", "phone_alt"}:
        raise ValueError("حقل غير مدعوم")
    row = get_account_verification(telegram_id)
    if not row:
        raise ValueError("طلب التوثيق غير موجود")
    if not _is_field_editable(row, field):
        raise ValueError("هذا الحقل محفوظ ومقفل حاليًا.")

    needs = [item for item in _needs_fields(row) if item != field]
    status = str(row["status"] or "DRAFT").upper()
    value_changed = str(row[field] or "") != str(value)
    reset_payment = bool(
        field == "phone_alt"
        and value_changed
        and str(row["verification_status"] or "").upper() == "VERIFIED"
    )
    if status in {"PENDING_REVIEW", "REJECTED"} or (status == "NEEDS_INFORMATION" and not needs):
        status = "DRAFT"
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if field in {"phone_primary", "phone_alt"} and _phone_used_by_other(con, value, int(telegram_id)):
            con.rollback()
            raise ValueError("هذا الرقم مستخدم من قبل. يرجى إدخال رقم آخر.")
        if reset_payment:
            con.execute(
                f"""
                UPDATE account_verification_requests
                SET {field}=?, status='DRAFT', needs_fields_json=?,
                    verification_reference=NULL, verification_received_amount_yer=NULL,
                    verification_status='NOT_STARTED', payer_display=NULL, payer_phone=NULL,
                    payment_time=NULL, payment_raw_json=NULL, review_chat_id=NULL,
                    review_message_id=NULL, updated_at=?
                WHERE telegram_id=?
                """,
                (value, json.dumps(needs, ensure_ascii=False), now_str(), int(telegram_id)),
            )
        else:
            con.execute(
                f"""
                UPDATE account_verification_requests
                SET {field}=?, status=?, needs_fields_json=?,
                    review_chat_id=CASE WHEN ? THEN NULL ELSE review_chat_id END,
                    review_message_id=CASE WHEN ? THEN NULL ELSE review_message_id END,
                    updated_at=? WHERE telegram_id=?
                """,
                (
                    value,
                    status,
                    json.dumps(needs, ensure_ascii=False),
                    1 if value_changed and str(row["status"] or "").upper() == "PENDING_REVIEW" else 0,
                    1 if value_changed and str(row["status"] or "").upper() == "PENDING_REVIEW" else 0,
                    now_str(),
                    int(telegram_id),
                ),
            )
        con.commit()
    return get_account_verification(telegram_id)


def save_verification_full_name(telegram_id: int, value: str) -> sqlite3.Row:
    return _save_field(telegram_id, "full_name", normalize_full_name(value))


def save_verification_primary_phone(telegram_id: int, value: str) -> sqlite3.Row:
    return _save_field(telegram_id, "phone_primary", normalize_yemen_mobile(value))


def save_verification_alt_phone(telegram_id: int, value: str) -> sqlite3.Row:
    return _save_field(telegram_id, "phone_alt", normalize_jaib_alternate(value))


def _verification_ready(row: sqlite3.Row) -> bool:
    return bool(
        str(row["full_name"] or "").strip()
        and str(row["phone_primary"] or "").strip()
        and str(row["phone_alt"] or "").strip()
        and str(row["verification_status"] or "").upper() == "VERIFIED"
    )


def _payment_duplicate(reference: str, request_id: int) -> Optional[str]:
    with DBX.connect() as con:
        row = con.execute(
            "SELECT request_code FROM account_verification_requests WHERE verification_reference=? AND id!=?",
            (reference, int(request_id)),
        ).fetchone()
        if row:
            return str(row["request_code"])
        row = con.execute(
            "SELECT deposit_code FROM wallet_deposits WHERE bank_transaction_id=? LIMIT 1",
            (reference,),
        ).fetchone()
        if row:
            return str(row["deposit_code"])
        row = con.execute(
            "SELECT order_code FROM orders_v2 WHERE payment_reference=? LIMIT 1",
            (reference,),
        ).fetchone()
        if row:
            return str(row["order_code"])
    return None


def _payment_candidates(details: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (getattr(details, "phone_candidates", None) or []) + (getattr(details, "identifiers", None) or []):
        value = normalize_phone(item.get("value"))
        if value and value not in seen:
            seen.add(value)
            result.append({"value": value, "source": str(item.get("source") or "")})
    return result


def _is_debit(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    value = raw.get("isDebitTranID")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "debit", "outgoing"}


async def verify_account_payment(telegram_id: int, reference: str) -> dict[str, Any]:
    row = get_account_verification(telegram_id)
    if not row:
        raise ValueError("ابدأ طلب التوثيق أولًا.")
    row = _ensure_request_terms(row)
    if str(row["status"] or "").upper() in _REQUEST_LOCKED_STATES:
        raise ValueError("طلبك مقفل حاليًا ولا يقبل عملية تحقق جديدة.")
    if not row["full_name"] or not row["phone_primary"] or not row["phone_alt"]:
        raise ValueError("أكمل الاسم والرقمين قبل التحقق من التحويل.")
    reference = str(reference or "").strip().translate(ARABIC_DIGITS)
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", reference):
        raise ValueError("أدخل رقم عملية Jaib الصحيح فقط.")
    duplicate = _payment_duplicate(reference, int(row["id"]))
    if duplicate:
        raise ValueError(f"رقم العملية مستخدم مسبقًا في المرجع {duplicate}.")

    with DBX.connect() as con:
        con.execute(
            "UPDATE account_verification_requests SET verification_status='VERIFYING', verification_started_at=?, updated_at=? WHERE id=?",
            (now_str(), now_str(), int(row["id"])),
        )
        con.commit()

    def _query() -> tuple[bool, Any]:
        if not JaibClient.enabled():
            return False, "إعدادات Jaib غير مكتملة"
        client = JaibClient()
        if not client.init():
            return False, "تعذر الاتصال بخوادم Jaib"
        login_ok, login_message = client.login()
        if not login_ok:
            return False, f"تعذر تسجيل الدخول إلى Jaib: {login_message}"
        return client.query_ref(reference)

    ok = False
    result: Any = "تعذر العثور على رقم العملية"
    for attempt in range(1, JAIB_VERIFY_RETRY_ATTEMPTS + 1):
        started = time.monotonic()
        ok, result = await asyncio.to_thread(_query)
        elapsed = time.monotonic() - started
        log.info(
            "Account verification Jaib attempt=%s/%s user=%s reference=%s ok=%s elapsed=%.2fs result=%s",
            attempt,
            JAIB_VERIFY_RETRY_ATTEMPTS,
            int(telegram_id),
            reference,
            bool(ok and isinstance(result, JaibTx)),
            elapsed,
            type(result).__name__ if isinstance(result, JaibTx) else str(result)[:180],
        )
        if ok and isinstance(result, JaibTx):
            break
        if attempt < JAIB_VERIFY_RETRY_ATTEMPTS and JAIB_VERIFY_RETRY_DELAY_SECONDS:
            await asyncio.sleep(JAIB_VERIFY_RETRY_DELAY_SECONDS)
    if not ok or not isinstance(result, JaibTx):
        with DBX.connect() as con:
            con.execute(
                "UPDATE account_verification_requests SET verification_status='NOT_FOUND', updated_at=? WHERE id=?",
                (now_str(), int(row["id"])),
            )
            con.commit()
        return {"ok": False, "status": "NOT_FOUND", "message": str(result)}

    tx = result
    details = getattr(tx, "api_details", None) or extract_payment_api_details(tx.raw)
    amount = Decimal(str(tx.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    expected_amount = _request_expected_amount(row)
    if amount != expected_amount:
        status = "AMOUNT_MISMATCH"
        message = f"يجب أن تكون قيمة التحويل {format_yer_amount(expected_amount)} YER بالضبط."
    elif _is_debit(tx.raw):
        status = "DEBIT_TRANSACTION"
        message = "العملية المشار إليها عملية صادرة وليست تحويلًا مستلمًا."
    else:
        status = "VERIFIED"
        message = "تم التحقق من عملية التحويل بنجاح."

    matched_phone = ""
    if status == "VERIFIED":
        for item in _payment_candidates(details):
            if phones_match(item["value"], row["phone_alt"]):
                matched_phone = item["value"]
                break
        candidates = _payment_candidates(details)
        if candidates and not matched_phone:
            status = "PHONE_MISMATCH"
            message = "رقم المحفظة المرسلة لا يطابق رقم Jaib البديل المسجل."

    raw_json = json.dumps(tx.raw, ensure_ascii=False, default=str)
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        duplicate = con.execute(
            "SELECT request_code FROM account_verification_requests WHERE verification_reference=? AND id!=?",
            (reference, int(row["id"])),
        ).fetchone()
        if duplicate:
            con.rollback()
            raise ValueError(f"رقم العملية مستخدم مسبقًا في المرجع {duplicate['request_code']}.")
        new_request_status = "READY_TO_SUBMIT" if status == "VERIFIED" else "DRAFT"
        con.execute(
            """
            UPDATE account_verification_requests
            SET verification_reference=?, verification_received_amount_yer=?, verification_status=?,
                payer_display=?, payer_phone=?, payment_time=?, payment_raw_json=?,
                status=?, updated_at=?
            WHERE id=?
            """,
            (
                reference,
                q2(amount),
                status,
                str(getattr(details, "payer_display", "") or ""),
                matched_phone or (_payment_candidates(details)[0]["value"] if _payment_candidates(details) else ""),
                str(getattr(details, "tx_date", "") or ""),
                raw_json,
                new_request_status,
                now_str(),
                int(row["id"]),
            ),
        )
        con.commit()
    return {
        "ok": status == "VERIFIED",
        "status": status,
        "message": message,
        "amount_yer": q2(amount),
        "request": account_verification_public(get_account_verification(telegram_id)),
    }


def account_verification_public(row: Optional[sqlite3.Row]) -> dict[str, Any]:
    if not row:
        return {
            "status": "NEW",
            "steps": {},
            "can_submit": False,
            "needs_fields": [],
        }
    row = _ensure_request_terms(row)
    needs = _needs_fields(row)
    status = str(row["status"] or "DRAFT").upper()
    return {
        "id": int(row["id"]),
        "request_code": row["request_code"],
        "status": status,
        "full_name": row["full_name"] or "",
        "phone_primary": row["phone_primary"] or "",
        "phone_alt": row["phone_alt"] or "",
        "verification_reference": row["verification_reference"] or "",
        "verification_amount_yer": format_yer_amount(_request_expected_amount(row)),
        "verification_received_amount_yer": row["verification_received_amount_yer"] or "",
        "verification_exchange_rate_yer": format_yer_amount(_request_exchange_rate(row)),
        "verification_credit_usdt": row["verification_credit_usdt"] or q8(_request_credit_usdt(row)),
        "verification_credited_at": row["verification_credited_at"] or "",
        "verification_status": row["verification_status"] or "NOT_STARTED",
        "requested_at": row["requested_at"] or "",
        "rejection_reason": row["rejection_reason"] or "",
        "refund_status": row["refund_status"] or "NOT_REQUIRED",
        "refund_proof_attached": bool(row["refund_proof_file_id"]),
        "needs_fields": needs,
        "can_submit": _verification_ready(row) and status in {"DRAFT", "READY_TO_SUBMIT", "NEEDS_INFORMATION"},
        "editable": {
            field: _is_field_editable(row, field)
            for field in ("full_name", "phone_primary", "phone_alt", "verification")
        },
        "steps": {
            "full_name": bool(row["full_name"]),
            "phone_primary": bool(row["phone_primary"]),
            "phone_alt": bool(row["phone_alt"]),
            "verification": str(row["verification_status"] or "").upper() == "VERIFIED",
        },
        "recipient": {
            "phone": verification_recipient_phone(),
            "name": verification_recipient_name(),
            "amount_yer": format_yer_amount(_request_expected_amount(row)),
            "exchange_rate_yer": format_yer_amount(_request_exchange_rate(row)),
            "credit_usdt": q8(_request_credit_usdt(row)),
        },
    }


def verification_channel_id() -> Optional[int | str]:
    value = str(DBX.get_setting("account_verification_channel_id") or ACCOUNT_VERIFICATION_CHANNEL_ID or "").strip()
    if not value and callable(globals().get("unified_notification_channel_id")):
        unified = unified_notification_channel_id()
        value = str(unified or "").strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else f"@{value.lstrip('@')}"


def _request_message(row: sqlite3.Row) -> str:
    return (
        "<b>🆕 طلب تفعيل حساب جديد</b>\n\n"
        f"🧾 رقم الطلب: <code>{html(row['request_code'])}</code>\n"
        f"👤 الاسم: <b>{html(row['full_name'] or '—')}</b>\n"
        f"🆔 Telegram ID: <code>{int(row['telegram_id'])}</code>\n"
        f"📱 الرقم الأساسي: <code>{html(row['phone_primary'] or '—')}</code>\n"
        f"💳 رقم Jaib البديل: <code>{html(row['phone_alt'] or '—')}</code>\n\n"
        f"✅ تحقق تحويل <b>{html(format_yer_amount(_request_expected_amount(row)))} YER</b>\n"
        f"💱 سعر الطلب: <b>{html(format_yer_amount(_request_exchange_rate(row)))} YER = 1 USDT</b>\n"
        f"💰 الرصيد المتوقع عند القبول: <b>{html(format_display_amount(_request_credit_usdt(row)))} USDT</b>\n"
        f"رقم العملية: <code>{html(row['verification_reference'] or '—')}</code>\n"
        f"وقت العملية: <code>{html(row['payment_time'] or '—')}</code>\n"
        f"الحالة: <b>{html(row['status'])}</b>"
    )


def _request_admin_kb(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                btn(render_bot_button('bot.services.account_verification.py:717:button','✅ قبول الحساب', []), f"av_admin_accept:{request_id}", "success"),
                btn(render_bot_button('bot.services.account_verification.py:718:button','❌ رفض الطلب', []), f"av_admin_reject:{request_id}", "danger"),
            ],
            [
                btn(render_bot_button('bot.services.account_verification.py:721:button','📝 استكمال البيانات', []), f"av_admin_needs:{request_id}"),
                btn(render_bot_button('bot.services.account_verification.py:722:button','🔍 فتح التفاصيل', []), f"av_admin_details:{request_id}"),
            ],
        ]
    )


async def _send_request_to_review(bot: Any, row: sqlite3.Row) -> tuple[Optional[str], Optional[int]]:
    channel = verification_channel_id()
    automatic = account_approval_mode() == "AUTO" if callable(globals().get("account_approval_mode")) else False
    markup = None if automatic else _request_admin_kb(int(row["id"]))
    suffix = "\n\n🤖 سيُقبل هذا الطلب تلقائيًا بعد مدة الانتظار." if automatic else ""
    if channel is None:
        recipients = {int(ADMIN_ID)}
        for admin in DBX.list_admins():
            if admin["status"] == "ACTIVE":
                recipients.add(int(admin["telegram_id"]))
        message_id = None
        for recipient in recipients:
            try:
                sent = await bot.send_message(
                    recipient,
                    render_bot_text('bot.services.account_verification.py:741:message','{v0}{v1}\n\n⚠️ لم يتم ضبط قناة طلبات التفعيل.', [str(_request_message(row)),str(suffix)]),
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                message_id = getattr(sent, "message_id", None)
            except Exception:
                log.exception("Failed to send verification request admin=%s", recipient)
        return None, message_id
    sent = await bot.send_message(
        channel,
        render_bot_text('bot.services.account_verification.py:751:message','{v0}{v1}', [str(_request_message(row)),str(suffix)]),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )
    return str(channel), getattr(sent, "message_id", None)


async def submit_account_verification(telegram_id: int, bot: Any) -> sqlite3.Row:
    row = get_account_verification(telegram_id)
    if not row:
        raise ValueError("طلب التوثيق غير موجود.")
    if not _verification_ready(row):
        raise ValueError("أكمل جميع خطوات التوثيق قبل تقديم الطلب.")
    if str(row["status"] or "").upper() == "PENDING_REVIEW":
        return row
    if str(row["status"] or "").upper() in {"APPROVED", "REJECTED_PENDING_REFUND", "REFUNDED"}:
        raise ValueError("لا يمكن تقديم هذا الطلب في حالته الحالية.")

    with DBX.connect() as con:
        if _phone_used_by_other(con, str(row["phone_primary"] or ""), int(telegram_id)):
            raise ValueError("الرقم الأساسي مستخدم من قبل في حساب آخر.")
        if _phone_used_by_other(con, str(row["phone_alt"] or ""), int(telegram_id)):
            raise ValueError("رقم Jaib البديل مستخدم من قبل في حساب آخر.")
        bot_user = con.execute("SELECT username FROM bot_users WHERE telegram_id=?", (int(telegram_id),)).fetchone()
    user_stub = SimpleNamespace(
        id=int(telegram_id),
        full_name=row["full_name"],
        first_name=str(row["full_name"]).split()[0],
        username=(bot_user["username"] if bot_user else "") or "",
    )
    customer = ensure_pending_customer(user_stub)
    DBX.update_customer(
        int(customer["id"]),
        full_name=row["full_name"],
        phone_primary=row["phone_primary"],
        phone_alt=row["phone_alt"],
        status="PENDING",
        telegram_username=user_stub.username,
        notes="طلب توثيق آلي مكتمل عبر Jaib",
    )
    now = now_str()
    mode = account_approval_mode() if callable(globals().get("account_approval_mode")) else "MANUAL"
    delay = account_auto_approval_delay_seconds() if callable(globals().get("account_auto_approval_delay_seconds")) else 30
    auto_at = time.time() + delay if mode == "AUTO" else None
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE account_verification_requests
            SET customer_id=?, status='PENDING_REVIEW', requested_at=?,
                rejection_reason=NULL, needs_fields_json='[]', refund_status='NOT_REQUIRED',
                approval_mode=?, auto_approve_at_epoch=?, auto_approval_attempts=0,
                auto_approval_error=NULL, updated_at=?
            WHERE id=?
            """,
            (int(customer["id"]), now, mode, auto_at, now, int(row["id"])),
        )
        con.commit()
    row = get_account_verification(telegram_id)
    chat_id, message_id = await _send_request_to_review(bot, row)
    with DBX.connect() as con:
        con.execute(
            "UPDATE account_verification_requests SET review_chat_id=?, review_message_id=?, updated_at=? WHERE id=?",
            (chat_id, message_id, now_str(), int(row["id"])),
        )
        con.commit()
    DBX.audit(telegram_id, row["full_name"], "SUBMIT_ACCOUNT_VERIFICATION", "account_verification", row["request_code"])
    return get_account_verification(telegram_id)


async def _edit_review_message(bot: Any, row: sqlite3.Row, suffix: str) -> None:
    chat_id = row["review_chat_id"]
    message_id = row["review_message_id"]
    if not chat_id or not message_id:
        return
    target: int | str = int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id)
    try:
        await bot.edit_message_text(
            render_bot_text('bot.services.account_verification.py:829:message','{v0}\n\n{v1}', [str(_request_message(row)),f'{suffix}']),
            chat_id=target,
            message_id=int(message_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("Failed to edit account verification review message request=%s", row["request_code"])


async def approve_account_verification(bot: Any, request_id: int, admin: Any) -> tuple[sqlite3.Row, Decimal]:
    """تفعيل الحساب وإضافة مبلغ التحقق إلى الرصيد في معاملة ذرية واحدة."""
    request_id = int(request_id)
    now = now_str()
    credited_usdt = Decimal("0")
    existing = get_account_verification_by_id(request_id)
    if not existing:
        raise ValueError("طلب التفعيل غير موجود.")
    _ensure_request_terms(existing)

    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM account_verification_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if not row:
            con.rollback()
            raise ValueError("طلب التفعيل غير موجود.")

        current_status = str(row["status"] or "").upper()
        if current_status == "APPROVED":
            credited_usdt = wallet_decimal(row["verification_credit_usdt"] or 0)
            con.commit()
            return row, Decimal("0")
        if current_status != "PENDING_REVIEW":
            con.rollback()
            raise ValueError("الطلب ليس بانتظار المراجعة.")
        if str(row["verification_status"] or "").upper() != "VERIFIED":
            con.rollback()
            raise ValueError("لا يمكن تفعيل الحساب قبل نجاح عملية التحقق.")

        customer = con.execute(
            "SELECT * FROM customers WHERE id=?",
            (int(row["customer_id"] or 0),),
        ).fetchone()
        if not customer:
            con.rollback()
            raise ValueError("العميل المرتبط بالطلب غير موجود.")

        amount_yer = _request_expected_amount(row)
        exchange_rate = _request_exchange_rate(row)
        credited_usdt = _request_credit_usdt(row)
        if amount_yer <= 0 or exchange_rate <= 0 or credited_usdt <= 0:
            con.rollback()
            raise ValueError("تعذر حساب رصيد مبلغ التحقق.")

        telegram_id = int(row["telegram_id"])
        already_credited = bool(str(row["verification_credited_at"] or "").strip())
        ledger_code = str(row["verification_credit_ledger_code"] or "").strip()
        if not already_credited:
            wallet = con.execute(
                "SELECT * FROM user_wallets WHERE telegram_user_id=?",
                (telegram_id,),
            ).fetchone()
            if not wallet:
                con.execute(
                    """
                    INSERT INTO user_wallets(
                        telegram_user_id, customer_id, available_balance, locked_balance,
                        total_deposited, total_withdrawn, created_at, updated_at
                    ) VALUES (?, ?, '0', '0', '0', '0', ?, ?)
                    """,
                    (telegram_id, int(customer["id"]), now, now),
                )
                wallet = con.execute(
                    "SELECT * FROM user_wallets WHERE telegram_user_id=?",
                    (telegram_id,),
                ).fetchone()

            available_before = wallet_decimal(wallet["available_balance"])
            locked_before = wallet_decimal(wallet["locked_balance"])
            total_deposited_before = wallet_decimal(wallet["total_deposited"])
            available_after = available_before + credited_usdt
            total_deposited_after = total_deposited_before + credited_usdt
            ledger_code = wallet_ref("AVC")

            con.execute(
                """
                UPDATE user_wallets
                SET customer_id=COALESCE(customer_id, ?), available_balance=?,
                    total_deposited=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (
                    int(customer["id"]),
                    q8(available_after),
                    q8(total_deposited_after),
                    now,
                    telegram_id,
                ),
            )
            con.execute(
                """
                INSERT INTO wallet_ledger(
                    ledger_code, telegram_user_id, customer_id, transaction_type, status,
                    amount_usdt, fiat_amount_yer, exchange_rate_yer,
                    available_before, available_after, locked_before, locked_after,
                    bank_transaction_id, admin_id, notes, created_at, completed_at
                ) VALUES (?, ?, ?, 'ACCOUNT_VERIFICATION_CREDIT', 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_code,
                    telegram_id,
                    int(customer["id"]),
                    q8(credited_usdt),
                    q2(amount_yer),
                    q2(exchange_rate),
                    q8(available_before),
                    q8(available_after),
                    q8(locked_before),
                    q8(locked_before),
                    row["verification_reference"],
                    int(admin.id),
                    f"رصيد مبلغ توثيق الحساب للطلب {row['request_code']}",
                    now,
                    now,
                ),
            )

        con.execute(
            "UPDATE customers SET status='ACTIVE', updated_at=? WHERE id=?",
            (now, int(customer["id"])),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO user_registrations(
                telegram_id, status, created_at, updated_at
            ) VALUES (?, 'NEW', ?, ?)
            """,
            (telegram_id, now, now),
        )
        con.execute(
            """
            UPDATE user_registrations
            SET status='ACTIVE', activated_at=COALESCE(activated_at, ?), updated_at=?
            WHERE telegram_id=?
            """,
            (now, now, telegram_id),
        )
        con.execute(
            """
            UPDATE account_verification_requests
            SET status='APPROVED', reviewed_by=?, reviewed_at=?,
                verification_credit_usdt=?, verification_credit_ledger_code=?,
                verification_credited_at=COALESCE(verification_credited_at, ?), updated_at=?
            WHERE id=?
            """,
            (
                int(admin.id),
                now,
                q8(credited_usdt),
                ledger_code,
                now,
                now,
                request_id,
            ),
        )
        con.commit()

    # مكافأة الإحالة مستقلة وذرية في خدمتها، ولا تؤثر على اعتماد رصيد التحقق.
    reward = await complete_referral_after_activation(SimpleNamespace(bot=bot), int(row["telegram_id"]))
    updated = get_account_verification_by_id(request_id)
    try:
        await bot.send_message(
            int(row["telegram_id"]),
            render_bot_text('bot.services.account_verification.py:1003:message','✅ <b>تم قبول وتفعيل حسابك</b>\n\nيمكنك الآن استخدام الدفع التلقائي وبقية خدمات البوت.\n\n💰 أضيف مبلغ التحقق إلى رصيدك: <b>{v0} USDT</b>\n💱 الحساب: <b>{v1} YER</b> بسعر <b>{v2} YER = 1 USDT</b>.', [f'{html(format_display_amount(credited_usdt))}',f'{html(format_yer_amount(amount_yer))}',f'{html(format_yer_amount(exchange_rate))}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1011:button','🏠 فتح القائمة الرئيسية', []), "main_menu", "success")]]),
        )
    except Exception:
        log.exception("Failed to notify approved verification user=%s", row["telegram_id"])
    await _edit_review_message(
        bot,
        updated,
        f"✅ تم القبول بواسطة {html(actor_name(admin))}\n"
        f"💰 أضيف {html(format_display_amount(credited_usdt))} USDT إلى رصيد المستخدم",
    )
    DBX.audit(
        int(admin.id),
        actor_name(admin),
        "APPROVE_ACCOUNT_VERIFICATION",
        "account_verification",
        row["request_code"],
        f"verification_credit_usdt={q8(credited_usdt)}",
    )
    return updated, reward


async def reject_account_verification(bot: Any, request_id: int, admin: Any, reason: str) -> sqlite3.Row:
    row = get_account_verification_by_id(request_id)
    if not row or str(row["status"]).upper() != "PENDING_REVIEW":
        raise ValueError("الطلب غير متاح للرفض.")
    reason = str(reason or "").strip()
    if len(reason) < 3:
        raise ValueError("اكتب سبب رفض واضحًا.")
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE account_verification_requests
            SET status='REJECTED_PENDING_REFUND', rejection_reason=?, refund_status='PENDING',
                reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?
            """,
            (reason, int(admin.id), now_str(), now_str(), int(row["id"])),
        )
        con.commit()
    updated = get_account_verification_by_id(request_id)
    try:
        await bot.send_message(
            int(row["telegram_id"]),
            render_bot_text('bot.services.account_verification.py:1051:message','❌ <b>تم رفض طلب تفعيل الحساب</b>\n\nالسبب: {v0}\nيجري الآن إرجاع مبلغ التحقق، وسيصلك إشعار بعد تأكيد الإرجاع.', [f'{html(reason)}']),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("Failed to notify rejected verification user=%s", row["telegram_id"])
    await _edit_review_message(bot, updated, f"❌ تم الرفض بواسطة {html(actor_name(admin))}\nالسبب: {html(reason)}\n⏳ بانتظار إرجاع المبلغ")
    DBX.audit(int(admin.id), actor_name(admin), "REJECT_ACCOUNT_VERIFICATION", "account_verification", row["request_code"], reason)
    return updated


def refund_admin_text(row: sqlite3.Row) -> str:
    return (
        render_bot_text('fragment.bot.services.account_verification.py:refund_admin_text:1062','<b>💸 إرجاع مبلغ تحقق الحساب</b>\n\nالطلب: <code>{v0}</code>\nالمستخدم: <code>{v1}</code>\nرقم Jaib البديل: <code>{v2}</code>\nالمبلغ: <b>{v3} YER</b>\nسبب الرفض: {v4}\n\nإرفاق صورة إثبات اختياري، ويمكن تأكيد الإرجاع بدونه.',[f"{html(row['request_code'])}",f"{int(row['telegram_id'])}",f"{html(row['phone_alt'] or '—')}",f'{html(format_yer_amount(_request_expected_amount(row)))}',f"{html(row['rejection_reason'] or '—')}"])
    )


def refund_admin_kb(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.account_verification.py:1080:button','💸 تم إرجاع المبلغ', []), f"av_admin_refunded:{request_id}", "success")],
            [btn(render_bot_button('bot.services.account_verification.py:1081:button','📎 إرفاق صورة إثبات', []), f"av_admin_refund_proof:{request_id}")],
        ]
    )


async def mark_account_verification_refunded(bot: Any, request_id: int, admin: Any) -> sqlite3.Row:
    row = get_account_verification_by_id(request_id)
    if not row or str(row["status"]).upper() != "REJECTED_PENDING_REFUND":
        raise ValueError("الطلب لا ينتظر إرجاع مبلغ.")
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE account_verification_requests
            SET status='REJECTED', refund_status='REFUNDED', refunded_at=?, updated_at=? WHERE id=?
            """,
            (now_str(), now_str(), int(row["id"])),
        )
        con.commit()
    updated = get_account_verification_by_id(request_id)
    try:
        await bot.send_message(
            int(row["telegram_id"]),
            render_bot_text('bot.services.account_verification.py:1101:message','💸 <b>تم إرجاع مبلغ التحقق</b>\n\nالمبلغ: <b>{v0} YER</b>\nسبب رفض الطلب: {v1}\n\nيمكنك بدء طلب جديد وتصحيح بياناتك.', [f'{html(format_yer_amount(_request_expected_amount(row)))}',f"{html(row['rejection_reason'] or '—')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1108:button','🔄 بدء طلب جديد', []), "av_restart", "primary")]]),
        )
    except Exception:
        log.exception("Failed to notify refunded verification user=%s", row["telegram_id"])
    await _edit_review_message(bot, updated, f"💸 تم إرجاع المبلغ بواسطة {html(actor_name(admin))}")
    DBX.audit(int(admin.id), actor_name(admin), "REFUND_ACCOUNT_VERIFICATION", "account_verification", row["request_code"])
    return updated


async def request_missing_information(bot: Any, request_id: int, admin: Any, field: str) -> sqlite3.Row:
    row = get_account_verification_by_id(request_id)
    if not row or str(row["status"]).upper() != "PENDING_REVIEW":
        raise ValueError("الطلب ليس بانتظار المراجعة.")
    if field not in _FIELD_LABELS:
        raise ValueError("الحقل غير صالح.")
    if field != "verification" and row[field]:
        raise ValueError("هذا الحقل مكتمل بالفعل؛ استخدم الرفض إذا كانت البيانات غير صحيحة.")
    if field == "verification" and str(row["verification_status"]).upper() == "VERIFIED":
        raise ValueError("عملية التحقق مكتملة بالفعل.")
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE account_verification_requests
            SET status='NEEDS_INFORMATION', needs_fields_json=?, updated_at=? WHERE id=?
            """,
            (json.dumps([field], ensure_ascii=False), now_str(), int(row["id"])),
        )
        con.commit()
    updated = get_account_verification_by_id(request_id)
    try:
        await bot.send_message(
            int(row["telegram_id"]),
            render_bot_text('bot.services.account_verification.py:1138:message','📝 <b>طلبت الإدارة استكمال بياناتك</b>\n\nالمطلوب: <b>{v0}</b>\nتم فتح هذا الحقل فقط، وبقية البيانات ما زالت مقفلة.', [f'{html(_FIELD_LABELS[field])}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1144:button','📝 استكمال الآن', []), "av_refresh", "primary")]]),
        )
    except Exception:
        log.exception("Failed to notify missing-info user=%s", row["telegram_id"])
    await _edit_review_message(bot, updated, f"📝 مطلوب استكمال: {html(_FIELD_LABELS[field])}")
    DBX.audit(int(admin.id), actor_name(admin), "REQUEST_ACCOUNT_INFORMATION", "account_verification", row["request_code"], field)
    return updated


def restart_account_verification(telegram_id: int) -> sqlite3.Row:
    row = get_account_verification(telegram_id)
    if not row or str(row["status"]).upper() != "REJECTED":
        raise ValueError("لا يمكن بدء طلب جديد في الحالة الحالية.")
    amount = _new_verification_amount()
    rate = _current_verification_rate(telegram_id)
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE account_verification_requests
            SET request_code=?, status='DRAFT', full_name=NULL, phone_primary=NULL, phone_alt=NULL,
                verification_reference=NULL, verification_amount_yer=?,
                verification_received_amount_yer=NULL, verification_exchange_rate_yer=?,
                verification_credit_usdt=NULL, verification_credit_ledger_code=NULL,
                verification_credited_at=NULL, verification_status='NOT_STARTED',
                verification_started_at=NULL, payer_display=NULL, payer_phone=NULL,
                payment_time=NULL, payment_raw_json=NULL, requested_at=NULL,
                reviewed_by=NULL, reviewed_at=NULL, rejection_reason=NULL,
                needs_fields_json='[]', refund_status='NOT_REQUIRED',
                refund_proof_file_id=NULL, refunded_at=NULL, review_chat_id=NULL,
                review_message_id=NULL, updated_at=? WHERE id=?
            """,
            (_request_code(), q2(amount), q2(rate), now_str(), int(row["id"])),
        )
        con.commit()
    customer = DBX.get_customer_by_telegram(int(telegram_id))
    if customer and customer["status"] != "ACTIVE":
        DBX.update_customer(int(customer["id"]), status="PENDING")
    return get_account_verification(telegram_id)



def onboarding_welcome_text() -> str:
    return (
        render_bot_text('fragment.bot.services.account_verification.py:onboarding_welcome_text:1177','<b>مرحبًا بك في {v0}</b>\n\nيمكنك شراء وإدارة USDT تلقائيًا عبر محفظة Jaib، ثم السحب إلى عنوان الشبكة المدعومة. التحقق من دفع Jaib يتم آليًا بالكامل، أما طرق الدفع الأخرى فتتم بالتواصل المباشر مع الإدارة.\n\nلبدء الاستخدام، أكمل خطوات توثيق الحساب مرة واحدة.',[f'{html(APP_NAME)}'])
    )


def onboarding_welcome_keyboard() -> InlineKeyboardMarkup:
    rows = [[btn(render_bot_button('bot.services.account_verification.py:1195:button','🔐 تسجيل الدخول / تفعيل الحساب', []), "av_start", "success")]]
    guide_url = bot_usage_guide_url()
    if guide_url:
        rows.append([InlineKeyboardButton(render_bot_button('bot.services.account_verification.py:1198:button','📘 كيفية استخدام البوت', []), url=guide_url)])
    rows.append([InlineKeyboardButton(render_bot_button('bot.services.account_verification.py:1199:button','💬 طرق دفع أخرى / الإدارة', []), url=wallet_admin_contact_url())])
    return InlineKeyboardMarkup(rows)

def onboarding_status_text(row: sqlite3.Row) -> str:
    status = str(row["status"] or "DRAFT").upper()
    labels = {
        "DRAFT": "📝 أكمل خطوات التوثيق التالية.",
        "READY_TO_SUBMIT": "✅ اكتملت البيانات. يمكنك تقديم الطلب الآن.",
        "PENDING_REVIEW": "⏳ تم تقديم طلبك. حسابك الآن قيد المراجعة.",
        "NEEDS_INFORMATION": "📝 طلبت الإدارة استكمال بيانات محددة.",
        "APPROVED": "✅ تم تفعيل حسابك.",
        "REJECTED_PENDING_REFUND": "⏳ تم رفض الطلب ويجري إرجاع مبلغ التحقق.",
        "REJECTED": "❌ تم رفض الطلب وإرجاع مبلغ التحقق. يمكنك بدء طلب جديد.",
    }
    text = labels.get(status, labels["DRAFT"])
    if row["rejection_reason"]:
        text += render_bot_text('fragment.bot.services.account_verification.py:onboarding_status_text:1205','\nسبب الرفض: {v0}',[f"{html(row['rejection_reason'])}"])
    return text


def onboarding_keyboard(row: sqlite3.Row) -> InlineKeyboardMarkup:
    status = str(row["status"] or "DRAFT").upper()
    if status == "APPROVED":
        return InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1222:button','🏠 فتح القائمة الرئيسية', []), "main_menu", "success")]])
    if status == "REJECTED":
        return InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1224:button','🔄 بدء طلب جديد', []), "av_restart", "primary")]])
    if status == "REJECTED_PENDING_REFUND":
        return InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1226:button','🔄 تحديث الحالة', []), "av_refresh")]])

    rows: list[list[InlineKeyboardButton]] = []
    verification_amount = format_yer_amount(_request_expected_amount(row))
    steps = [
        ("full_name", "1️⃣ الاسم الرباعي", "av_name"),
        ("phone_primary", "2️⃣ الرقم الأساسي", "av_phone_primary"),
        ("phone_alt", "3️⃣ رقم Jaib البديل", "av_phone_alt"),
        ("verification", f"4️⃣ تحقق تحويل {verification_amount} YER", "av_verify"),
    ]
    for field, label, callback in steps:
        completed = bool(row[field]) if field != "verification" else str(row["verification_status"]).upper() == "VERIFIED"
        editable = _is_field_editable(row, field)
        icon = "✏️" if completed and editable and field != "verification" else ("✅" if completed else "▫️")
        if editable:
            rows.append([btn(render_bot_button('bot.services.account_verification.py:1241:button','{v0} {v1}', [f'{icon}',f'{label}']), callback, "primary" if not completed else None)])
        else:
            rows.append([btn(render_bot_button('bot.services.account_verification.py:1243:button','🔒 {v0}', [f'{label}']), f"av_locked:{field}")])
    if status == "PENDING_REVIEW":
        rows.append([btn(render_bot_button('bot.services.account_verification.py:1245:button','🔄 تحديث الحالة', []), "av_refresh")])
    elif _verification_ready(row):
        rows.append([btn(render_bot_button('bot.services.account_verification.py:1247:button','📨 تقديم طلب التفعيل', []), "av_submit", "success")])
    else:
        rows.append([btn(render_bot_button('bot.services.account_verification.py:1249:button','🔒 تقديم الطلب — أكمل الخطوات', []), "av_locked:submit")])
    guide_url = bot_usage_guide_url()
    if guide_url:
        rows.append([InlineKeyboardButton(render_bot_button('bot.services.account_verification.py:1252:button','📘 كيفية استخدام البوت', []), url=guide_url)])
    rows.append([InlineKeyboardButton(render_bot_button('bot.services.account_verification.py:1253:button','💬 طرق دفع أخرى / الإدارة', []), url=wallet_admin_contact_url())])
    return InlineKeyboardMarkup(rows)


def onboarding_text(user: Any, row: sqlite3.Row) -> str:
    def mark(done: bool) -> str:
        return "✅" if done else "▫️"
    return (
        render_bot_text('fragment.bot.services.account_verification.py:onboarding_text:1251','<b>مرحبًا بك في {v0}</b>\n\nخدمة شراء وإدارة USDT تعمل تلقائيًا عبر محفظة Jaib وعناوين الشبكات المدعومة. طرق الدفع الأخرى متاحة بالتواصل المباشر مع الإدارة.\n\n{v1}\n\n{v2} الاسم الرباعي: <b>{v3}</b>\n{v4} الرقم الأساسي: <code>{v5}</code>\n{v6} رقم Jaib البديل: <code>{v7}</code>\n{v8} تحقق تحويل <b>{v9} YER</b>\n\nيمكنك تعديل الاسم والرقمين حتى قبول الحساب نهائيًا. تغيير رقم Jaib بعد التحقق يلغي التحقق السابق لحماية حسابك، ثم يمكنك التحقق وإرسال الطلب مجددًا.',[f'{html(APP_NAME)}',f'{onboarding_status_text(row)}',f"{mark(bool(row['full_name']))}",f"{html(row['full_name'] or 'غير مكتمل')}",f"{mark(bool(row['phone_primary']))}",f"{html(row['phone_primary'] or 'غير مكتمل')}",f"{mark(bool(row['phone_alt']))}",f"{html(row['phone_alt'] or 'غير مكتمل')}",f"{mark(str(row['verification_status']).upper() == 'VERIFIED')}",f'{html(format_yer_amount(_request_expected_amount(row)))}'])
    )


async def show_account_onboarding(target: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    row = ensure_account_verification(user)
    await send_or_edit(target, render_bot_text('bot.services.account_verification.py:1278:message','{v0}', [str(onboarding_text(user, row))]), onboarding_keyboard(row))


# حفظ المعالجات المجمّعة قبل استبدالها بمعالجات التوثيق الجديدة.
_account_legacy_show_main = show_main
_account_legacy_on_callback = on_callback
_account_legacy_on_message = on_message


async def show_main(target: Any, context: ContextTypes.DEFAULT_TYPE):
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    if user is None:
        raise RuntimeError("تعذر تحديد المستخدم")
    register_bot_user(user)
    if not await ensure_required_subscription(target, context, int(user.id)):
        return
    # منع واجهة المحفظة من تكرار فحص القنوات في الطلب نفسه.
    if context.user_data is not None:
        context.user_data["_subscription_checked"] = True
    customer = DBX.get_customer_by_telegram(int(user.id))
    if DBX.is_admin(int(user.id)) or (customer and customer["status"] == "ACTIVE"):
        legacy_wallet_main = globals().get("_registration_legacy_show_main") or _account_legacy_show_main
        await legacy_wallet_main(target, context)
        return
    if customer and customer["status"] == "SUSPENDED":
        await send_or_edit(
            target,
            render_bot_text('bot.services.account_verification.py:1303:message','⛔ <b>حسابك موقوف حاليًا</b>\n\nتواصل مع الإدارة لمراجعة حالة الحساب.', []),
            InlineKeyboardMarkup([[InlineKeyboardButton(render_bot_button('bot.services.account_verification.py:1306:button','📩 التواصل مع الإدارة', []), url=wallet_admin_contact_url())]]),
        )
        return
    if get_account_verification(int(user.id)) is None:
        await send_or_edit(target, render_bot_text('bot.services.account_verification.py:1310:message','{v0}', [str(onboarding_welcome_text())]), onboarding_welcome_keyboard())
        return
    await show_account_onboarding(target, context)


async def _safe_answer(query: Any, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest as exc:
        if "query is too old" not in str(exc).lower() and "query id is invalid" not in str(exc).lower():
            raise


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = str(query.data or "")
    user = query.from_user

    if data.startswith("av_"):
        await _safe_answer(query)
        if data == "av_start" or data == "av_refresh":
            set_state(context, None)
            await show_account_onboarding(query, context)
            return
        if data == "av_restart":
            try:
                restart_account_verification(int(user.id))
                await show_account_onboarding(query, context)
            except ValueError as exc:
                await _safe_answer(query, str(exc), show_alert=True)
            return
        if data.startswith("av_locked:"):
            await _safe_answer(query, "هذا الحقل مقفل أو لم يحن دوره بعد.", show_alert=True)
            return
        if data in {"av_name", "av_phone_primary", "av_phone_alt"}:
            row = ensure_account_verification(user)
            field = {"av_name": "full_name", "av_phone_primary": "phone_primary", "av_phone_alt": "phone_alt"}[data]
            if not _is_field_editable(row, field):
                await _safe_answer(query, "هذا الحقل محفوظ ومقفل.", show_alert=True)
                return
            state_name = {
                "full_name": "await_av_full_name",
                "phone_primary": "await_av_phone_primary",
                "phone_alt": "await_av_phone_alt",
            }[field]
            set_state(context, state_name)
            prompt = {
                "full_name": "<b>أدخل اسمك الرباعي كاملًا</b>\n\nمثال: عبدالرحمن علي محمد أحمد",
                "phone_primary": "<b>أدخل رقمك الأساسي</b>\n\nيقبل 77XXXXXXX أو +96777XXXXXXX.",
                "phone_alt": "<b>أدخل رقم Jaib البديل</b>\n\nأدخل الرقم المرتبط بمحفظتك في Jaib.",
            }[field]
            await query.edit_message_text(
                render_bot_text('bot.services.account_verification.py:1361:message','{v0}', [str(prompt)]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1364:button','↩️ رجوع', []), "av_refresh")]]),
            )
            return
        if data == "av_verify":
            row = ensure_account_verification(user)
            if not _is_field_editable(row, "verification"):
                await _safe_answer(query, "عملية التحقق مقفلة حاليًا.", show_alert=True)
                return
            if not row["full_name"] or not row["phone_primary"] or not row["phone_alt"]:
                await _safe_answer(query, "أكمل الاسم والرقمين أولًا.", show_alert=True)
                return
            set_state(context, "await_av_reference")
            await query.edit_message_text(
                render_bot_text('bot.services.account_verification.py:1376:message','<b>✅ عملية تحقق الحساب</b>\n\nحوّل <b>{v0} YER بالضبط</b> إلى:\nالرقم: <code>{v1}</code>\nاسم المستفيد: <b>{v2}</b>\n\nيجب أن يكون التحويل من رقم Jaib البديل الذي أدخلته. بعد التحويل أرسل رقم العملية فقط.', [f'{html(format_yer_amount(_request_expected_amount(row)))}',f"{html(verification_recipient_phone() or 'غير مضبوط')}",f'{html(verification_recipient_name())}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1383:button','↩️ رجوع', []), "av_refresh")]]),
            )
            return
        if data == "av_submit":
            try:
                row = await submit_account_verification(int(user.id), context.bot)
                await query.edit_message_text(
                    render_bot_text('bot.services.account_verification.py:1389:message','✅ <b>تم إرسال طلبك بنجاح</b>\n\nسيتم مراجعته وإبلاغك بالرد في أقرب وقت. يمكنك تحديث الحالة من زر /start.', []),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1393:button','🔄 تحديث الحالة', []), "av_refresh")]]),
                )
            except ValueError as exc:
                await _safe_answer(query, str(exc), show_alert=True)
            return

        if data.startswith("av_admin_"):
            if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "customers_status"):
                await _safe_answer(query, "لا تملك صلاحية مراجعة طلبات التفعيل.", show_alert=True)
                return
            parts = data.split(":")
            action = parts[0]
            request_id = int(parts[1]) if len(parts) > 1 else 0
            if action == "av_admin_accept":
                try:
                    row, reward = await approve_account_verification(context.bot, request_id, user)
                    await _safe_answer(query, "تم تفعيل الحساب")
                except ValueError as exc:
                    await _safe_answer(query, str(exc), show_alert=True)
                return
            if action == "av_admin_reject":
                set_state(context, "await_av_reject_reason", request_id=request_id)
                try:
                    await context.bot.send_message(
                        int(user.id),
                        render_bot_text('bot.services.account_verification.py:1416:message','اكتب سبب رفض طلب التفعيل في رسالة واحدة.', []),
                        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.account_verification.py:1419:button','إلغاء', []), "av_admin_details:%s" % request_id)]]),
                    )
                    await _safe_answer(query, "أرسل سبب الرفض في المحادثة الخاصة", show_alert=True)
                except Exception:
                    await _safe_answer(query, "ابدأ محادثة خاصة مع البوت ثم حاول مجددًا.", show_alert=True)
                return
            if action == "av_admin_needs":
                row = get_account_verification_by_id(request_id)
                if not row:
                    await _safe_answer(query, "الطلب غير موجود", show_alert=True)
                    return
                choices = []
                for field in ("full_name", "phone_primary", "phone_alt"):
                    if not row[field]:
                        choices.append([btn(render_bot_button('bot.services.account_verification.py:1433:button','{v0}', [str(_FIELD_LABELS[field])]), f"av_admin_need_field:{request_id}:{field}")])
                if str(row["verification_status"]).upper() != "VERIFIED":
                    choices.append([btn(render_bot_button('bot.services.account_verification.py:1435:button','{v0}', [str(_FIELD_LABELS['verification'])]), f"av_admin_need_field:{request_id}:verification")])
                if not choices:
                    await _safe_answer(query, "لا توجد حقول ناقصة. استخدم الرفض إذا كانت البيانات غير صحيحة.", show_alert=True)
                    return
                try:
                    await context.bot.send_message(
                        int(user.id),
                        render_bot_text('bot.services.account_verification.py:1440:message','اختر الحقل الناقص الذي تريد فتحه للمستخدم:', []),
                        reply_markup=InlineKeyboardMarkup(choices),
                    )
                    await _safe_answer(query, "تم إرسال الخيارات في الخاص", show_alert=True)
                except Exception:
                    await _safe_answer(query, "تعذر إرسال الخيارات في الخاص", show_alert=True)
                return
            if action == "av_admin_need_field" and len(parts) >= 3:
                field = parts[2]
                try:
                    await request_missing_information(context.bot, request_id, user, field)
                    await query.edit_message_text(render_bot_text('bot.services.account_verification.py:1453:message','✅ تم فتح الحقل المطلوب وإشعار المستخدم.', []))
                except ValueError as exc:
                    await _safe_answer(query, str(exc), show_alert=True)
                return
            if action == "av_admin_details":
                row = get_account_verification_by_id(request_id)
                if not row:
                    await _safe_answer(query, "الطلب غير موجود", show_alert=True)
                    return
                try:
                    await context.bot.send_message(
                        int(user.id),
                        render_bot_text('bot.services.account_verification.py:1463:message','{v0}', [str(_request_message(row))]),
                        parse_mode=ParseMode.HTML,
                        reply_markup=_request_admin_kb(request_id) if row["status"] == "PENDING_REVIEW" else None,
                    )
                    await _safe_answer(query, "تم إرسال التفاصيل في الخاص", show_alert=True)
                except Exception:
                    await _safe_answer(query, "تعذر إرسال التفاصيل", show_alert=True)
                return
            if action == "av_admin_refunded":
                try:
                    await mark_account_verification_refunded(context.bot, request_id, user)
                    await query.edit_message_text(render_bot_text('bot.services.account_verification.py:1476:message','✅ تم تأكيد إرجاع المبلغ وإشعار المستخدم.', []))
                except ValueError as exc:
                    await _safe_answer(query, str(exc), show_alert=True)
                return
            if action == "av_admin_refund_proof":
                set_state(context, "await_av_refund_proof", request_id=request_id)
                await query.edit_message_text(render_bot_text('bot.services.account_verification.py:1482:message','📎 أرسل صورة إثبات الإرجاع الآن. الإرفاق اختياري ويمكنك العودة دون صورة.', []))
                return

    await _account_legacy_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state, payload = current_state(context)
    user = update.effective_user
    if state not in {
        "await_av_full_name",
        "await_av_phone_primary",
        "await_av_phone_alt",
        "await_av_reference",
        "await_av_reject_reason",
        "await_av_refund_proof",
    }:
        await _account_legacy_on_message(update, context)
        return

    if user is None:
        return
    message = update.effective_message
    text = str(getattr(message, "text", "") or "").strip()
    try:
        if state == "await_av_full_name":
            value = normalize_full_name(text)
            set_state(context, "await_av_confirm_field", field="full_name", value=value)
            await message.reply_text(
                render_bot_text('bot.services.account_verification.py:1510:message','راجع الاسم قبل الحفظ:\n<b>{v0}</b>', [f'{html(value)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[btn(render_bot_button('bot.services.account_verification.py:1514:button','✅ تأكيد', []), "av_confirm_field:full_name", "success")], [btn(render_bot_button('bot.services.account_verification.py:1514:button','↩️ تعديل', []), "av_name")]]
                ),
            )
            return
        if state == "await_av_phone_primary":
            value = normalize_yemen_mobile(text)
            set_state(context, "await_av_confirm_field", field="phone_primary", value=value)
            await message.reply_text(
                render_bot_text('bot.services.account_verification.py:1521:message','راجع الرقم الأساسي قبل الحفظ:\n<code>{v0}</code>', [f'{html(value)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[btn(render_bot_button('bot.services.account_verification.py:1525:button','✅ تأكيد', []), "av_confirm_field:phone_primary", "success")], [btn(render_bot_button('bot.services.account_verification.py:1525:button','↩️ تعديل', []), "av_phone_primary")]]
                ),
            )
            return
        if state == "await_av_phone_alt":
            value = normalize_jaib_alternate(text)
            set_state(context, "await_av_confirm_field", field="phone_alt", value=value)
            await message.reply_text(
                render_bot_text('bot.services.account_verification.py:1532:message','راجع رقم Jaib البديل قبل الحفظ:\n<code>{v0}</code>', [f'{html(value)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[btn(render_bot_button('bot.services.account_verification.py:1536:button','✅ تأكيد', []), "av_confirm_field:phone_alt", "success")], [btn(render_bot_button('bot.services.account_verification.py:1536:button','↩️ تعديل', []), "av_phone_alt")]]
                ),
            )
            return
        if state == "await_av_reference":
            progress = await message.reply_text(render_bot_text('bot.services.account_verification.py:1541:message','⏳ جارٍ التحقق من عملية Jaib...', []))
            result = await verify_account_payment(int(user.id), text)
            set_state(context, None)
            try:
                await progress.edit_text(
                    ("✅ " if result["ok"] else "❌ ") + html(result["message"]),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await show_account_onboarding(update, context)
            return
        if state == "await_av_reject_reason":
            if not DBX.is_admin(int(user.id)):
                set_state(context, None)
                return
            request_id = int(payload.get("request_id") or 0)
            row = await reject_account_verification(context.bot, request_id, user, text)
            set_state(context, None)
            await message.reply_text(
                render_bot_text('bot.services.account_verification.py:1560:message','{v0}', [str(refund_admin_text(row))]),
                parse_mode=ParseMode.HTML,
                reply_markup=refund_admin_kb(request_id),
            )
            return
        if state == "await_av_refund_proof":
            if not DBX.is_admin(int(user.id)):
                set_state(context, None)
                return
            request_id = int(payload.get("request_id") or 0)
            photos = getattr(message, "photo", None) or []
            if not photos:
                await message.reply_text(render_bot_text('bot.services.account_verification.py:1573:message','أرسل صورة، أو ارجع واستخدم زر «تم إرجاع المبلغ» بدون صورة.', []))
                return
            file_id = photos[-1].file_id
            with DBX.connect() as con:
                con.execute(
                    "UPDATE account_verification_requests SET refund_proof_file_id=?, updated_at=? WHERE id=?",
                    (file_id, now_str(), request_id),
                )
                con.commit()
            set_state(context, None)
            row = get_account_verification_by_id(request_id)
            await message.reply_text(
                render_bot_text('bot.services.account_verification.py:1584:message','✅ تم حفظ صورة الإثبات. أكّد الإرجاع بعد اكتمال التحويل.', []),
                reply_markup=refund_admin_kb(request_id),
            )
            return
    except ValueError as exc:
        await message.reply_text(render_bot_text('bot.services.account_verification.py:1590:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Account verification message handling failed state=%s user=%s", state, user.id)
        await message.reply_text(render_bot_text('bot.services.account_verification.py:1593:message','⚠️ تعذر تنفيذ الخطوة مؤقتًا. حاول مرة أخرى.', []))


_export_globals(globals())

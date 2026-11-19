"""User registry, forced subscription, Jaib response parsing, and payment matching.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# طبقة الاستقرار والميزات النهائية
# ============================================================
# هذه الطبقة تبقي الهيكل السابق متوافقاً مع قاعدة البيانات الحالية،
# وتضيف التحقق الشامل، المراجعة اليدوية، الإذاعة، الاشتراك الإجباري،
# قناة الإشعارات، ورسوم الشبكة التي يتحملها النظام.

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

MIN_USDT_ORDER = max(Decimal("5"), MIN_USDT_ORDER)
SYSTEM_NETWORK_FEE_USDT = Decimal(env("SYSTEM_NETWORK_FEE_USDT", "0.01"))
BROADCAST_DELAY_SECONDS = float(env("BROADCAST_DELAY_SECONDS", "0.06"))
MAX_API_FIELDS_IN_MESSAGE = int(env("MAX_API_FIELDS_IN_MESSAGE", "12"))
REQUIRE_LIVE_FEE_MATCH = env("REQUIRE_LIVE_FEE_MATCH", "true").lower() == "true"
WITHDRAW_AMOUNT_INCLUDES_FEE = env("WITHDRAW_AMOUNT_INCLUDES_FEE", "true").lower() == "true"
PAYMENT_TIME_TOLERANCE_MINUTES = int(env("PAYMENT_TIME_TOLERANCE_MINUTES", "10"))
REQUIRE_PAYMENT_TIME_VALIDATION = env("REQUIRE_PAYMENT_TIME_VALIDATION", "true").lower() == "true"

ADMIN_PERMISSION_LABELS.update(
    {
        "broadcast_send": "📢 إذاعة للكل",
        "subscriptions_manage": "✅ إدارة الاشتراك الإجباري",
        "notification_channel_manage": "📺 إدارة قناة الإشعارات",
        "settings_view": "⚙️ عرض الإعدادات",
    }
)
ALL_ADMIN_PERMISSIONS = tuple(ADMIN_PERMISSION_LABELS)


def _ensure_column(con: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _enhance_database_schema() -> None:
    with DBX.connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                welcome_sent_at TEXT,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                blocked_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                username TEXT,
                title TEXT,
                invite_link TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                added_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                total_users INTEGER NOT NULL,
                sent_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            )
            """
        )
        for column, declaration in {
            "payment_raw_json": "TEXT",
            "payer_display": "TEXT",
            "payer_identifiers": "TEXT",
            "review_reason": "TEXT",
            "admin_reviewed_by": "INTEGER",
            "admin_reviewed_at": "TEXT",
            "withdraw_amount": "TEXT",
            "network_fee": "TEXT",
            "channel_notified": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            _ensure_column(con, "orders_v2", column, declaration)
        con.execute("CREATE INDEX IF NOT EXISTS idx_bot_users_blocked ON bot_users(is_blocked)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_required_channels_active ON required_channels(active)")
        con.commit()


_enhance_database_schema()


def register_bot_user(user: Any) -> bool:
    """Register every person who interacts with the bot. Returns True on first sight."""
    if not user:
        return False
    telegram_id = int(user.id)
    username = getattr(user, "username", None)
    full_name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(telegram_id)
    with DBX.connect() as con:
        existing = con.execute("SELECT telegram_id FROM bot_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existing:
            con.execute(
                """
                UPDATE bot_users
                SET username = ?, full_name = ?, last_seen_at = ?, is_blocked = 0, blocked_at = NULL
                WHERE telegram_id = ?
                """,
                (username, full_name, now_str(), telegram_id),
            )
            first = False
        else:
            con.execute(
                """
                INSERT INTO bot_users(
                    telegram_id, username, full_name, first_seen_at, last_seen_at,
                    welcome_sent_at, is_blocked, blocked_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, NULL)
                """,
                (telegram_id, username, full_name, now_str(), now_str()),
            )
            first = True
        con.commit()
    if first:
        log.info(
            "New Telegram user registered user=%s username=%s full_name=%s",
            telegram_id,
            username,
            full_name,
        )
    return first


def bot_user_welcome_sent(telegram_id: int) -> bool:
    with DBX.connect() as con:
        row = con.execute(
            "SELECT welcome_sent_at FROM bot_users WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
    return bool(row and row["welcome_sent_at"])


def mark_bot_user_welcome_sent(telegram_id: int) -> None:
    with DBX.connect() as con:
        con.execute(
            "UPDATE bot_users SET welcome_sent_at = ?, last_seen_at = ? WHERE telegram_id = ?",
            (now_str(), now_str(), int(telegram_id)),
        )
        con.commit()


def mark_bot_user_blocked(telegram_id: int, blocked: bool = True) -> None:
    with DBX.connect() as con:
        con.execute(
            "UPDATE bot_users SET is_blocked = ?, blocked_at = ? WHERE telegram_id = ?",
            (1 if blocked else 0, now_str() if blocked else None, int(telegram_id)),
        )
        con.commit()


def list_broadcast_targets() -> list[int]:
    targets: set[int] = set()
    with DBX.connect() as con:
        for row in con.execute("SELECT telegram_id FROM bot_users WHERE is_blocked = 0").fetchall():
            if row["telegram_id"]:
                targets.add(int(row["telegram_id"]))
        for row in con.execute(
            "SELECT telegram_id FROM customers WHERE telegram_id IS NOT NULL AND status = 'ACTIVE'"
        ).fetchall():
            targets.add(int(row["telegram_id"]))
    return sorted(targets)


def list_required_channels(active_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM required_channels"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id DESC"
    with DBX.connect() as con:
        return con.execute(sql).fetchall()


def get_required_channel(channel_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute("SELECT * FROM required_channels WHERE id = ?", (int(channel_id),)).fetchone()


def save_required_channel(
    chat_id: str,
    username: str,
    title: str,
    invite_link: str,
    added_by: int,
) -> None:
    with DBX.connect() as con:
        con.execute(
            """
            INSERT INTO required_channels(
                chat_id, username, title, invite_link, active, added_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                title = excluded.title,
                invite_link = excluded.invite_link,
                active = 1,
                added_by = excluded.added_by,
                updated_at = excluded.updated_at
            """,
            (chat_id, username or None, title or None, invite_link or None, int(added_by), now_str(), now_str()),
        )
        con.commit()


def delete_required_channel(channel_id: int) -> None:
    with DBX.connect() as con:
        con.execute("DELETE FROM required_channels WHERE id = ?", (int(channel_id),))
        con.commit()


def _as_chat_identifier(value: Any) -> Any:
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text if text.startswith("@") else f"@{text.lstrip('@')}"


def _parse_channel_input(text: str) -> tuple[Any, str]:
    parts = [part.strip() for part in (text or "").split("|", 1)]
    identifier = _as_chat_identifier(parts[0])
    invite_link = parts[1] if len(parts) > 1 else ""
    return identifier, invite_link


def _channel_join_url(row: sqlite3.Row) -> str:
    if row["invite_link"]:
        return str(row["invite_link"])
    if row["username"]:
        return f"https://t.me/{str(row['username']).lstrip('@')}"
    return ""


def _member_status_value(member: Any) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status)).lower()


async def missing_required_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[sqlite3.Row]:
    if DBX.is_admin(user_id):
        return []
    missing: list[sqlite3.Row] = []
    for row in list_required_channels(True):
        try:
            member = await context.bot.get_chat_member(
                chat_id=_as_chat_identifier(row["chat_id"]),
                user_id=int(user_id),
            )
            status = _member_status_value(member)
            valid = status in {"member", "administrator", "creator", "owner"}
            if status == "restricted":
                valid = bool(getattr(member, "is_member", False))
            if not valid:
                missing.append(row)
        except TelegramError as exc:
            log.warning(
                "Subscription check failed channel=%s user=%s: %s",
                row["chat_id"],
                user_id,
                exc,
                exc_info=True,
            )
            missing.append(row)
    return missing


def subscription_kb(channels: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        url = _channel_join_url(channel)
        title = channel["title"] or channel["username"] or channel["chat_id"]
        if url:
            rows.append([InlineKeyboardButton(text=render_bot_button('bot.services.registration_payment.py:302:button','📢 انضم إلى {v0}', [f'{title}']), url=url)])
    rows.append([btn(render_bot_button('bot.services.registration_payment.py:303:button','✅ تحقق من الاشتراك', []), "check_subscription", "success")])
    return InlineKeyboardMarkup(rows)


async def show_subscription_gate(target: Any, context: ContextTypes.DEFAULT_TYPE, channels: list[sqlite3.Row]) -> None:
    text = (
        render_bot_text('fragment.bot.services.registration_payment.py:show_subscription_gate:310','<b>🔒 الاشتراك الإجباري</b>\n\nيجب الاشتراك في جميع القنوات التالية قبل استخدام البوت، ثم اضغط زر التحقق.',[])
    )
    markup = subscription_kb(channels)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(render_bot_text('bot.services.registration_payment.py:314:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=markup)
    elif hasattr(target, "effective_message") and target.effective_message:
        await target.effective_message.reply_text(render_bot_text('bot.services.registration_payment.py:316:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=markup)
    elif hasattr(target, "reply_text"):
        await target.reply_text(render_bot_text('bot.services.registration_payment.py:318:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=markup)


async def ensure_required_subscription(target: Any, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    missing = await missing_required_channels(context, user_id)
    if not missing:
        return True
    await show_subscription_gate(target, context, missing)
    return False


def welcome_text(user: Any) -> str:
    first_name = getattr(user, "first_name", None) or "عميلنا"
    return (
        render_bot_text('fragment.bot.services.registration_payment.py:welcome_text:333','👋 أهلاً <b>{v0}</b> في <b>{v1}</b>\n\n<b>طريقة الاستخدام:</b>\n1️⃣ اضغط شراء USDT.\n2️⃣ أدخل الكمية المطلوبة.\n3️⃣ حوّل المبلغ إلى رقم محفظة Jaib الظاهر.\n4️⃣ أرسل رقم العملية للتحقق.\n5️⃣ اختر الشبكة وأرسل عنوان محفظتك بدقة.\n\n💵 الحد الأدنى: <b>{v2} USDT</b>\n💸 رسوم الشبكة التي يتحملها النظام: <b>{v3} USDT</b>\n\n⚠️ استخدم رقم العملية مرة واحدة فقط، وراجع الشبكة والعنوان قبل الإرسال.\n📌 عند إدخال قيمة خاطئة لديك {v4} محاولات للتصحيح.',[f'{html(first_name)}',f'{html(APP_NAME)}',f'{html(format_display_amount(MIN_USDT_ORDER))}',f'{html(format_display_amount(SYSTEM_NETWORK_FEE_USDT))}',f'{MAX_INPUT_ATTEMPTS}'])
    )


# ---------------- استخراج جميع بيانات عملية Jaib ----------------

PAYER_LABEL_WORDS = (
    "المودع", "المرسل", "المحول", "المحوّل", "الدافع", "صاحب الحساب",
    "sender", "payer", "depositor", "from account", "from phone",
)
BENEFICIARY_LABEL_WORDS = (
    "المستفيد", "المستلم", "المستقبل", "beneficiary", "recipient", "receiver", "destination",
)
DIRECT_PHONE_KEYS = {
    "senderphone", "sender_phone", "payerphone", "payer_phone", "fromphone", "from_phone",
    "mobile", "phone", "phonenumber", "phone_number", "alternatenumber", "alternate_number",
}


@dataclass
class PaymentApiDetails:
    payer_display: str
    phone_candidates: list[dict[str, str]]
    identifiers: list[dict[str, str]]
    fields: list[dict[str, str]]
    tx_date: str
    tx_type: str
    note: str


def _label_lower(value: Any) -> str:
    return str(value or "").strip().lower().replace("ـ", "")


def _is_payer_label(label: str) -> bool:
    lowered = _label_lower(label)
    if any(word in lowered for word in BENEFICIARY_LABEL_WORDS):
        return False
    return any(word in lowered for word in PAYER_LABEL_WORDS)


def _all_numeric_values(value: Any) -> list[str]:
    converted = str(value or "").translate(ARABIC_DIGITS)
    found: list[str] = []
    for match in re.findall(r"(?<!\d)\+?\d(?:[\d\s().-]{1,24}\d)?(?!\d)", converted):
        digits = normalize_phone(match)
        if 3 <= len(digits) <= 20 and digits not in found:
            found.append(digits)
    direct = normalize_phone(converted)
    if 3 <= len(direct) <= 20 and direct not in found:
        found.append(direct)
    return found


def _phone_like_values(value: Any) -> list[str]:
    # أرقام الهواتف اليمنية تكون غالباً 9 أرقام أو 12 مع مفتاح الدولة.
    # نستبعد المعرفات القصيرة مثل 102777 من المطابقة، لكن نحفظها كبيانات إضافية.
    return [digits for digits in _all_numeric_values(value) if 7 <= len(digits) <= 15]


def extract_payment_api_details(raw: dict[str, Any]) -> PaymentApiDetails:
    fields: list[dict[str, str]] = []
    phones: list[dict[str, str]] = []
    identifiers: list[dict[str, str]] = []
    seen_phone: set[str] = set()
    seen_identifier: set[tuple[str, str]] = set()
    payer_display = ""
    tx_date = ""
    tx_type = ""
    note = ""

    def add_field(label: Any, value: Any, path: str) -> None:
        nonlocal payer_display, tx_date, tx_type, note
        label_text = str(label or path).strip()
        value_text = str(value if value is not None else "").strip()
        if not value_text and not label_text:
            return
        fields.append({"label": label_text, "value": value_text, "path": path})
        lowered = _label_lower(label_text)
        payer_field = _is_payer_label(label_text)
        if payer_field and value_text and not payer_display:
            payer_display = value_text
        if "تاريخ" in lowered or "وقت" in lowered or "date" in lowered or "time" in lowered:
            if not tx_date and value_text:
                tx_date = value_text
        if "العملية" in lowered or "نوع" in lowered or "operation" in lowered or "type" in lowered:
            if not tx_type and value_text:
                tx_type = value_text
        if "ملاحظ" in lowered or "note" in lowered or "description" in lowered:
            if not note and value_text:
                note = value_text

        direct_key = _label_lower(label_text).replace(" ", "").replace("-", "_")
        is_direct_phone = direct_key in DIRECT_PHONE_KEYS or _sender_key(label_text)
        if payer_field or is_direct_phone:
            for number in _all_numeric_values(value_text):
                marker = (number, path)
                if marker not in seen_identifier:
                    seen_identifier.add(marker)
                    identifiers.append({"value": number, "source": f"{path}:{label_text}"})
            for phone in _phone_like_values(value_text):
                if phone not in seen_phone:
                    seen_phone.add(phone)
                    phones.append({"value": phone, "source": f"{path}:{label_text}"})

    def walk(value: Any, path: str = "root") -> None:
        if isinstance(value, dict):
            if "name" in value and ("value" in value or "fieldValue" in value):
                add_field(
                    value.get("name") or value.get("label") or value.get("fieldName"),
                    value.get("value") if "value" in value else value.get("fieldValue"),
                    path,
                )
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if isinstance(child, (dict, list)):
                    walk(child, child_path)
                elif key not in {"name", "value", "fieldValue", "isCopy", "type"}:
                    add_field(key, child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(raw)

    # حقول مباشرة قد لا تكون ضمن fields.
    for key in (
        "senderPhone", "payerPhone", "fromPhone", "alternateNumber", "phone", "mobile",
        "sender_name", "senderName", "payerName", "customerName",
    ):
        if raw.get(key) not in (None, ""):
            add_field(key, raw.get(key), f"root.{key}")
            if "name" in key.lower() and not payer_display:
                payer_display = str(raw.get(key)).strip()

    return PaymentApiDetails(
        payer_display=payer_display,
        phone_candidates=phones,
        identifiers=identifiers,
        fields=fields,
        tx_date=tx_date,
        tx_type=tx_type,
        note=note,
    )


def match_customer_payment_details(
    customer: sqlite3.Row,
    details: PaymentApiDetails,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    # الترتيب المطلوب: الرقم الأساسي أولاً عبر جميع القيم، ثم الرقم البديل.
    for candidate in details.phone_candidates:
        if phones_match(candidate["value"], customer["phone_primary"]):
            return normalize_phone(customer["phone_primary"]), candidate["value"], candidate["source"]
    if customer["phone_alt"]:
        for candidate in details.phone_candidates:
            if phones_match(candidate["value"], customer["phone_alt"]):
                return normalize_phone(customer["phone_alt"]), candidate["value"], candidate["source"]
    return None, None, None


def payment_api_fields_text(details: PaymentApiDetails, limit: int = MAX_API_FIELDS_IN_MESSAGE) -> str:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in details.fields:
        label = item["label"].strip(": ") or item["path"]
        value = item["value"] or "—"
        marker = (label, value)
        if marker in seen:
            continue
        seen.add(marker)
        lines.append(f"• {html(label)}: <code>{html(value)}</code>")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "• لا توجد حقول إضافية"


def _enhanced_jaib_query_ref(self: JaibClient, ref_id: str) -> tuple[bool, Any]:
    ok, dec = self._execute({"code": "", "opType": JAIB_OP_QUERY_REF, "ReferenceID": ref_id})
    if not ok:
        return False, dec
    if not isinstance(dec, dict):
        return False, "رد Jaib ليس بصيغة صحيحة"
    details = extract_payment_api_details(dec)
    amount = decimal_or_zero(dec.get("amount"))
    currency = dec.get("currencyName") or dec.get("currency") or "ريال يمني"
    reference_id = str(dec.get("referenceID") or ref_id)
    tx_type = details.tx_type or dec.get("typeName") or dec.get("operationType")
    note = details.note or dec.get("note") or dec.get("description")
    tx = JaibTx(
        amount=amount,
        currency=str(currency),
        reference_id=reference_id,
        sender_name=details.payer_display or None,
        sender_phone=details.phone_candidates[0]["value"] if details.phone_candidates else None,
        tx_date=details.tx_date or None,
        tx_type=str(tx_type) if tx_type else None,
        note=str(note) if note else None,
        raw=dec,
    )
    setattr(tx, "api_details", details)
    return True, tx


JaibClient.query_ref = _enhanced_jaib_query_ref


def parse_payment_datetime(value: Any) -> Optional[datetime]:
    text_value = str(value or "").translate(ARABIC_DIGITS).strip()
    if not text_value:
        return None
    formats = (
        "%Y/%m/%d (%H:%M)",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y (%H:%M)",
        "%d/%m/%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    # Try extracting the most common date/time fragment from a longer API value.
    match = re.search(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*\(?\s*(\d{1,2}:\d{2})(?::\d{2})?\s*\)?", text_value)
    if match:
        normalized = match.group(1).replace("-", "/") + " " + match.group(2)
        try:
            return datetime.strptime(normalized, "%Y/%m/%d %H:%M")
        except ValueError:
            return None
    return None


def payment_time_validation(order_created_at: str, api_time: str) -> tuple[bool, str]:
    if not REQUIRE_PAYMENT_TIME_VALIDATION:
        return True, "DISABLED"
    tx_time = parse_payment_datetime(api_time)
    order_time = parse_payment_datetime(order_created_at)
    if tx_time is None:
        return False, "PAYMENT_TIME_NOT_AVAILABLE"
    if order_time is None:
        return False, "ORDER_TIME_NOT_AVAILABLE"
    # Jaib sends wall-clock time in Yemen by default; normalize it to the UTC
    # timestamps stored in SQLite. The offset remains configurable for API changes.
    tx_time = tx_time - timedelta(hours=JAIB_API_UTC_OFFSET_HOURS)
    tolerance = timedelta(minutes=max(0, PAYMENT_TIME_TOLERANCE_MINUTES))
    if tx_time < order_time - tolerance:
        return False, "OLD_PAYMENT_REFERENCE"
    if tx_time > utc_now() + timedelta(minutes=15):
        return False, "PAYMENT_TIME_IN_FUTURE"
    return True, "OK"


def claim_payment_reference(order_id: int, reference: str) -> tuple[bool, Optional[sqlite3.Row]]:
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM orders_v2 WHERE payment_reference = ? AND id != ?",
            (str(reference), int(order_id)),
        ).fetchone()
        if existing:
            con.rollback()
            return False, existing
        con.execute(
            "UPDATE orders_v2 SET payment_reference = ?, updated_at = ? WHERE id = ?",
            (str(reference), now_str(), int(order_id)),
        )
        con.commit()
    return True, None


async def _notify_payment_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order: sqlite3.Row,
    customer: sqlite3.Row,
    tx: JaibTx,
    details: PaymentApiDetails,
    reason: str,
) -> None:
    candidate_text = ", ".join(item["value"] for item in details.phone_candidates) or "لا يوجد رقم هاتف ظاهر"
    reason_text = {
        "PHONE_NOT_VISIBLE": "رقم الهاتف غير ظاهر في بيانات العملية",
        "PHONE_MISMATCH": "رقم الهاتف الظاهر لا يطابق الرقم الأساسي أو البديل المسجل",
        "PAYMENT_TIME_NOT_AVAILABLE": "وقت العملية غير ظاهر أو لم يمكن قراءته",
        "ORDER_TIME_NOT_AVAILABLE": "تعذر مقارنة وقت العملية بوقت إنشاء الطلب",
        "PAYMENT_TIME_IN_FUTURE": "وقت العملية الظاهر غير منطقي ويحتاج مراجعة",
    }.get(reason, reason)
    await update.message.reply_text(
        render_bot_text('bot.services.registration_payment.py:632:message','<b>⚠️ تم العثور على عملية الدفع</b>\n\n🔢 رقم العملية: <code>{v0}</code>\n💰 المبلغ: <b>{v1} {v2}</b>\n👤 بيانات المرسل: <code>{v3}</code>\n📱 الرقم الموجود: <code>{v4}</code>\n🕒 الوقت: <code>{v5}</code>\n\nسبب إيقاف الصرف التلقائي: <b>{v6}</b>\n\nتم العثور على عملية الدفع بنجاح، ولكن لم تستوفِ شروط المطابقة التلقائية، لذلك لا يمكن تنفيذ عملية الصرف قبل مراجعة الإدارة.\n\nإذا كنت تعتقد أن هناك خطأ، ارجع إلى القائمة الرئيسية وانتظر مراجعة الإدارة.', [f'{html(tx.reference_id)}',f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}',f"{html(details.payer_display or 'غير ظاهرة')}",f'{html(candidate_text)}',f"{html(details.tx_date or 'غير ظاهر')}",f'{html(reason_text)}']),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.registration_payment.py:644:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
    )
    api_text = payment_api_fields_text(details)
    username = f"@{update.effective_user.username}" if update.effective_user.username else "—"
    await notify_admin(
        context,
        render_bot_text('notice.bot.services.registration_payment.py:631','<b>🛡️ عملية صحيحة تحتاج مراجعة مطابقة</b>\n\n👤 مستخدم تيليجرام: {v0} ({v1})\n🆔 Telegram ID: <code>{v2}</code>\n🧾 الطلب: <code>{v3}</code>\n🔢 رقم العملية: <code>{v4}</code>\n💰 المبلغ: <b>{v5} {v6}</b>\n\n<b>سبب المراجعة:</b> {v7}\n\n<b>بيانات API:</b>\n{v8}\n\n<b>بيانات العميل المسجلة:</b>\n👤 {v9}\n📱 الأساسي: <code>{v10}</code>\n🔁 البديل: <code>{v11}</code>',[f'{html(update.effective_user.full_name)}',f'{html(username)}',f'{html(update.effective_user.id)}',f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}',f'{html(reason_text)}',f'{api_text}',f"{html(customer['full_name'])}",f"{html(customer['phone_primary'])}",f"{html(customer['phone_alt'] or '—')}"]),
        reply_markup=InlineKeyboardMarkup(
            [[
                btn(render_bot_button('bot.services.registration_payment.py:665:button','✅ قبول', []), f"adm_payment_approve:{order['id']}", "success"),
                btn(render_bot_button('bot.services.registration_payment.py:666:button','❌ رفض', []), f"adm_payment_reject:{order['id']}", "danger"),
            ]]
        ),
    )


async def verify_payment_reference(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: int,
    ref_id: str,
):
    order = DBX.get_order(order_id)
    if not order:
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:680:message','❌ الطلب غير موجود.', []))
        set_state(context, None)
        return
    customer = DBX.get_customer(order["customer_id"])
    if not customer or customer["status"] != "ACTIVE":
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:685:message','⛔ هذا العميل غير نشط.', []))
        set_state(context, None)
        return
    if int(order["telegram_user_id"]) != int(update.effective_user.id):
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:689:message','⛔ هذا الطلب لا يخص حسابك.', []))
        set_state(context, None)
        return
    if order["status"] != "WAITING_PAYMENT":
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:693:message','⚠️ هذا الطلب ليس في حالة انتظار دفع.', []))
        set_state(context, None)
        return
    if not ref_id.isdigit() or not (8 <= len(ref_id) <= 20):
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:697:message','❌ رقم العملية غير صالح. أرسل أرقامًا فقط.', []))
        return

    existing = DBX.get_order_by_payment_reference(ref_id)
    if existing and int(existing["id"]) != int(order_id):
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "duplicate_payment_reference",
            "HIGH",
            f"reference={ref_id} used_by={existing['order_code']}",
            order["order_code"],
        )
        attempts, remaining = failed_attempt(context)
        if attempts >= MAX_INPUT_ATTEMPTS:
            set_state(context, None)
            await update.message.reply_text(
                render_bot_text('bot.services.registration_payment.py:713:message','❌ رقم العملية مستخدم مسبقًا. انتهت المحاولات ({v0}).', [f'{MAX_INPUT_ATTEMPTS}']),
                reply_markup=user_main_kb(False),
            )
        else:
            await update.message.reply_text(
                render_bot_text('bot.services.registration_payment.py:718:message','❌ رقم العملية مستخدم مسبقًا. المحاولة {v0}/{v1} — المتبقي {v2}.', [f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}'])
            )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.registration_payment.py:704','<b>🚨 محاولة تكرار رقم عملية</b>\n\nالطلب الحالي: <code>{v0}</code>\nرقم العملية: <code>{v1}</code>\nمستخدم سابقًا في: <code>{v2}</code>',[f"{html(order['order_code'])}",f'{html(ref_id)}',f"{html(existing['order_code'])}"]),
        )
        return

    if not JaibClient.enabled():
        await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:731:message','❌ إعدادات Jaib غير مكتملة في ملف .env', []))
        set_state(context, None)
        return

    wait = await update.message.reply_text(render_bot_text('bot.services.registration_payment.py:735:message','⏳ جاري التحقق من العملية واستخراج جميع بياناتها...', []))

    def _lookup_jaib_reference():
        client = JaibClient()
        if not client.init():
            return "INIT_FAILED", "فشل الاتصال بخوادم Jaib"
        login_ok, login_message = client.login()
        if not login_ok:
            return "LOGIN_FAILED", login_message
        query_ok, query_result = client.query_ref(ref_id)
        return ("OK", query_result) if query_ok else ("QUERY_FAILED", query_result)

    lookup_status, result = await asyncio.to_thread(_lookup_jaib_reference)
    if lookup_status == "INIT_FAILED":
        await wait.edit_text("❌ فشل الاتصال بخوادم Jaib. حاول لاحقًا.")
        set_state(context, None)
        return
    if lookup_status == "LOGIN_FAILED":
        await wait.edit_text(
            f"❌ تعذر تسجيل الدخول إلى Jaib:\n<code>{html(result)}</code>",
            parse_mode=ParseMode.HTML,
        )
        set_state(context, None)
        return
    if lookup_status == "QUERY_FAILED":
        attempts, remaining = failed_attempt(context)
        if attempts >= MAX_INPUT_ATTEMPTS:
            set_state(context, None)
            await wait.edit_text(
                f"❌ لم يتم التحقق بعد {MAX_INPUT_ATTEMPTS} محاولات.\n\nالسبب: <code>{html(result)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=user_main_kb(False),
            )
        else:
            await wait.edit_text(
                f"❌ لم يتم العثور على العملية أو تعذر التحقق منها:\n<code>{html(result)}</code>\n\n"
                f"المحاولة {attempts}/{MAX_INPUT_ATTEMPTS} — المتبقي {remaining}.\n"
                "أرسل رقم العملية الصحيح مرة أخرى.",
                parse_mode=ParseMode.HTML,
            )
        return

    tx: JaibTx = result
    details: PaymentApiDetails = getattr(tx, "api_details", extract_payment_api_details(tx.raw))
    expected = Decimal(order["expected_yer_amount"])
    amount_matches = tx.amount == expected if JAIB_STRICT_EXACT_AMOUNT else tx.amount >= expected

    claimed, duplicate = claim_payment_reference(order_id, tx.reference_id)
    if not claimed:
        set_state(context, None)
        await wait.edit_text(
            "❌ رقم العملية تم استخدامه مسبقًا ولا يمكن استخدامه مرة أخرى.",
            reply_markup=user_main_kb(False),
        )
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "duplicate_payment_reference_race",
            "HIGH",
            f"reference={tx.reference_id} used_by={duplicate['order_code'] if duplicate else 'unknown'}",
            order["order_code"],
        )
        return

    raw_json = json.dumps(tx.raw, ensure_ascii=False, default=str)
    phones_json = json.dumps(details.phone_candidates, ensure_ascii=False)
    identifiers_json = json.dumps(details.identifiers, ensure_ascii=False)
    payer_phone = details.phone_candidates[0]["value"] if details.phone_candidates else ""

    # سجل الاستجابة الخام فور استلامها، حتى لو فشل المبلغ أو الوقت أو المطابقة لاحقًا.
    log_jaib_raw_transaction(
        order=order,
        customer=customer,
        telegram_user=update.effective_user,
        tx=tx,
        details=details,
        matched_registered=None,
        actual_candidate=None,
        match_source=None,
        stage="API_RESPONSE_RECEIVED",
    )

    if not amount_matches:
        DBX.update_order(
            order_id,
            payer_name=details.payer_display,
            payer_display=details.payer_display,
            payer_phone=payer_phone,
            payer_phone_candidates=phones_json,
            payer_identifiers=identifiers_json,
            payment_raw_json=raw_json,
            payment_raw_summary=json.dumps(
                {"date": details.tx_date, "type": details.tx_type, "note": details.note},
                ensure_ascii=False,
            ),
            paid_yer_amount=q2(tx.amount),
            status="REJECTED",
            rejection_reason="AMOUNT_MISMATCH",
            review_reason=None,
            error_message=f"expected={expected} actual={tx.amount}",
        )
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "amount_mismatch",
            "HIGH",
            f"expected={expected} actual={tx.amount}",
            order["order_code"],
        )
        set_state(context, None)
        await wait.edit_text(
            "<b>❌ المبلغ لا يطابق الطلب</b>\n\n"
            f"المتوقع: <b>{html(format_display_amount(expected))} ريال</b>\n"
            f"الواصل: <b>{html(format_display_amount(tx.amount))} {html(tx.currency)}</b>\n\n"
            "تم حجز رقم العملية لمنع إعادة استخدامه، وتم إرسال الحالة للإدارة.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_kb(False),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.registration_payment.py:836','<b>🚫 اختلاف مبلغ في عملية صحيحة</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>\nالمتوقع: {v2} ريال\nالواصل: {v3} {v4}',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f'{html(format_display_amount(expected))}',f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}']),
        )
        return

    # رفض أي عملية مدينة/صادرة؛ المطلوب عملية إيداع واردة إلى محفظة الإدارة.
    if bool(tx.raw.get("isDebitTranID")):
        DBX.update_order(
            order_id,
            payer_name=details.payer_display,
            payer_display=details.payer_display,
            payer_phone=payer_phone,
            payer_phone_candidates=phones_json,
            payer_identifiers=identifiers_json,
            payment_raw_json=raw_json,
            paid_yer_amount=q2(tx.amount),
            status="REJECTED",
            rejection_reason="DEBIT_TRANSACTION",
            error_message="The referenced transaction is debit/outgoing",
        )
        set_state(context, None)
        await wait.edit_text(
            "❌ العملية المشار إليها ليست عملية إيداع واردة، لذلك تم رفضها وحجز رقمها لمنع إعادة استخدامها.",
            reply_markup=user_main_kb(False),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.registration_payment.py:866','<b>🚫 محاولة استخدام عملية صادرة</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}']),
        )
        return

    time_valid, time_reason = payment_time_validation(order["created_at"], details.tx_date)
    if not time_valid:
        base_time_update = dict(
            payer_name=details.payer_display,
            payer_display=details.payer_display,
            payer_phone=payer_phone,
            payer_phone_candidates=phones_json,
            payer_identifiers=identifiers_json,
            payment_raw_json=raw_json,
            payment_raw_summary=json.dumps(
                {"date": details.tx_date, "type": details.tx_type, "note": details.note, "fields": details.fields},
                ensure_ascii=False,
                default=str,
            ),
            paid_yer_amount=q2(tx.amount),
        )
        if time_reason == "OLD_PAYMENT_REFERENCE":
            DBX.update_order(
                order_id,
                **base_time_update,
                status="REJECTED",
                rejection_reason="OLD_PAYMENT_REFERENCE",
                review_reason=None,
                error_message=f"payment_time={details.tx_date}; order_time={order['created_at']}",
            )
            DBX.security(
                update.effective_user.id,
                update.effective_user.username,
                "old_payment_reference",
                "HIGH",
                f"payment_time={details.tx_date}; order_time={order['created_at']}",
                order["order_code"],
            )
            set_state(context, None)
            await wait.edit_text(
                "<b>❌ تم رفض رقم العملية</b>\n\n"
                f"🕒 وقت العملية: <code>{html(details.tx_date or 'غير ظاهر')}</code>\n"
                f"🧾 وقت إنشاء الطلب: <code>{html(order['created_at'])}</code>\n\n"
                "هذه العملية أقدم من الطلب الحالي، وتم حجز رقمها لمنع إعادة استخدامها.",
                parse_mode=ParseMode.HTML,
                reply_markup=user_main_kb(False),
            )
            await notify_admin(
                context,
                render_bot_text('notice.bot.services.registration_payment.py:916','<b>🚨 محاولة استخدام عملية سابقة</b>\n\n👤 المستخدم: {v0}\n🆔 <code>{v1}</code>\n🧾 الطلب: <code>{v2}</code>\n🔢 العملية: <code>{v3}</code>\n🕒 وقت العملية: <code>{v4}</code>\n🕒 إنشاء الطلب: <code>{v5}</code>',[f'{html(update.effective_user.full_name)}',f'{html(update.effective_user.id)}',f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f"{html(details.tx_date or '—')}",f"{html(order['created_at'])}"]),
            )
            return
        DBX.update_order(
            order_id,
            **base_time_update,
            status="PAYMENT_REVIEW",
            review_reason=time_reason,
            rejection_reason=None,
            error_message=f"Payment time validation failed: {time_reason}",
        )
        set_state(context, None)
        try:
            await wait.delete()
        except TelegramError:
            log.debug("Could not delete payment verification progress message", exc_info=True)
        await _notify_payment_review(
            update,
            context,
            DBX.get_order(order_id),
            customer,
            tx,
            details,
            time_reason,
        )
        return

    matched_registered, actual_candidate, match_source = match_customer_payment_details(customer, details)
    trusted_customer = bool(customer["is_trusted"]) if "is_trusted" in customer.keys() else False
    if trusted_customer:
        matched_registered = "TRUSTED_CUSTOMER"
        actual_candidate = details.phone_candidates[0]["value"] if details.phone_candidates else ""
        match_source = "TRUSTED_CUSTOMER_BYPASS"

    log_jaib_raw_transaction(
        order=order,
        customer=customer,
        telegram_user=update.effective_user,
        tx=tx,
        details=details,
        matched_registered=matched_registered,
        actual_candidate=actual_candidate,
        match_source=match_source,
        stage="MATCH_RESULT",
    )

    common_update = dict(
        payer_name=details.payer_display,
        payer_display=details.payer_display,
        payer_phone_candidates=phones_json,
        payer_identifiers=identifiers_json,
        payment_raw_json=raw_json,
        payment_raw_summary=json.dumps(
            {
                "date": details.tx_date,
                "type": details.tx_type,
                "note": details.note,
                "fields": details.fields,
            },
            ensure_ascii=False,
            default=str,
        ),
        paid_yer_amount=q2(tx.amount),
    )

    if not matched_registered:
        reason = "PHONE_NOT_VISIBLE" if not details.phone_candidates else "PHONE_MISMATCH"
        DBX.update_order(
            order_id,
            **common_update,
            payer_phone=payer_phone,
            status="PAYMENT_REVIEW",
            review_reason=reason,
            rejection_reason=None,
            error_message=(
                "No phone number was visible in the payment API response"
                if reason == "PHONE_NOT_VISIBLE"
                else "Visible phone did not match primary or alternate customer phone"
            ),
        )
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "payment_manual_review",
            "HIGH",
            f"reason={reason}; candidates={phones_json}; primary={customer['phone_primary']}; alt={customer['phone_alt'] or ''}",
            order["order_code"],
        )
        set_state(context, None)
        try:
            await wait.delete()
        except TelegramError:
            log.debug("Could not delete payment review progress message", exc_info=True)
        order = DBX.get_order(order_id)
        await _notify_payment_review(update, context, order, customer, tx, details, reason)
        return

    match_result_message = (
        "⭐ العميل موثوق؛ تم تجاوز شرط مطابقة الهاتف بعد نجاح بقية فحوصات الأمان."
        if trusted_customer
        else (
            f"📱 الرقم الذي تم العثور عليه: <code>{html(actual_candidate or '—')}</code>\n"
            f"✅ الرقم المطابق: <code>{html(matched_registered or '—')}</code>"
        )
    )

    DBX.update_order(
        order_id,
        **common_update,
        payer_phone=actual_candidate,
        payer_phone_matched=matched_registered,
        payer_match_source=match_source,
        status="PAYMENT_VERIFIED",
        paid_at=now_str(),
        review_reason=None,
        rejection_reason=None,
        error_message=None,
    )
    DBX.audit(
        update.effective_user.id,
        actor_name(update.effective_user),
        "VERIFY_PAYMENT",
        "order",
        order["order_code"],
        f"reference={tx.reference_id}; matched={matched_registered}; source={match_source}",
    )
    set_state(context, None)
    order = DBX.get_order(order_id)
    await wait.edit_text(
        "<b>✅ تم التحقق من العملية ومطابقة بيانات العميل</b>\n\n"
        f"🔢 رقم العملية: <code>{html(tx.reference_id)}</code>\n"
        f"💰 المبلغ المستلم: <b>{html(format_display_amount(tx.amount))} {html(tx.currency)}</b>\n"
        f"💵 الكمية المستحقة: <b>{html(order['crypto_amount'])} USDT</b>\n"
        f"👤 بيانات المرسل: <code>{html(details.payer_display or 'غير ظاهرة')}</code>\n"
        f"{match_result_message}\n"
        f"🕒 وقت العملية: <code>{html(details.tx_date or 'غير ظاهر')}</code>\n"
        f"🔄 نوع العملية: <code>{html(details.tx_type or 'غير ظاهر')}</code>\n\n"
        "<b>البيانات الواردة من API:</b>\n"
        f"{payment_api_fields_text(details)}\n\n"
        "اختر السحب عبر عنوان المحفظة للمتابعة.",
        parse_mode=ParseMode.HTML,
        reply_markup=delivery_kb(order_id),
    )
    await notify_admin(
        context,
        render_bot_text('notice.bot.services.registration_payment.py:1067','<b>✅ تم التحقق والمطابقة تلقائيًا</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>\n👤 العميل: {v2}\n💰 {v3} {v4}\n👤 بيانات المرسل: <code>{v5}</code>\n📱 الرقم المستخرج: <code>{v6}</code>\n📱 الرقم المطابق: <code>{v7}</code>',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f"{html(customer['full_name'])}",f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}',f"{html(details.payer_display or '—')}",f"{html(actual_candidate or '—')}",f'{html(matched_registered)}']),
    )




_export_globals(globals())

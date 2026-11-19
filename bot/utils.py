"""Shared parsing, formatting, validation, state, and permission helpers.

"""
from __future__ import annotations

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# أدوات عامة
# ============================================================

SPAM_SECONDS = 0.6


def now_str() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def short_now() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M")


def utc_now() -> datetime:
    """UTC as a naive datetime for compatibility with existing SQLite text columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def q2(value: Decimal) -> str:
    return format(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def q8(value: Decimal) -> str:
    return format(Decimal(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), ".8f")


def display_decimals() -> int:
    """عدد الخانات المعروضة للمستخدم، مع إبقاء دقة التخزين الداخلية كما هي."""
    default = int(globals().get("DISPLAY_DECIMALS_DEFAULT", 4))
    try:
        raw = DBX.get_setting("display_decimals")
    except Exception:
        raw = None
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return min(8, max(0, value))


def set_display_decimals(value: Any) -> int:
    try:
        decimals = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("عدد الخانات العشرية غير صالح") from exc
    if decimals < 0 or decimals > 8:
        raise ValueError("عدد الخانات العشرية يجب أن يكون بين 0 و8")
    DBX.set_setting("display_decimals", str(decimals))
    return decimals


def format_display_amount(value: Any, decimals: Optional[int] = None) -> str:
    """تنسيق موحد للعرض فقط؛ لا يستخدم لتخزين القيم المالية."""
    selected = display_decimals() if decimals is None else min(8, max(0, int(decimals)))
    quantum = Decimal("1") if selected == 0 else Decimal("1").scaleb(-selected)
    amount = decimal_or_zero(value).quantize(quantum, rounding=ROUND_HALF_UP)
    return format(amount, f".{selected}f")


def display_money(value: Any, currency: str = "USDT", decimals: Optional[int] = None) -> str:
    return f"{format_display_amount(value, decimals)} {currency}"


def decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def parse_decimal(text: str) -> Decimal:
    cleaned = (text or "").strip().replace(",", "").replace("$", "")
    return Decimal(cleaned)


def html(text: Any) -> str:
    return escape(str(text))


def btn(text: str, callback_data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    """Build an inline button using the native PTB style field when available."""
    kwargs: dict[str, Any] = {"text": text, "callback_data": callback_data}
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        # Compatibility with older PTB releases while keeping one code path.
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs, api_kwargs={"style": style} if style else None)


ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def normalize_phone(value: Optional[str]) -> str:
    if value is None:
        return ""
    converted = str(value).translate(ARABIC_DIGITS)
    return re.sub(r"\D+", "", converted)


def phone_variants(value: Optional[str]) -> set[str]:
    digits = normalize_phone(value)
    if not digits:
        return set()
    variants = {digits}
    current = digits
    if current.startswith("00") and len(current) > 2:
        current = current[2:]
        variants.add(current)
    if current.startswith("967") and len(current) > 3:
        variants.add(current[3:])
    if current.startswith("0") and len(current) > 9:
        variants.add(current[1:])
    for item in tuple(variants):
        if len(item) >= 9:
            variants.add(item[-9:])
    return variants


def phones_match(a: Optional[str], b: Optional[str]) -> bool:
    left = phone_variants(a)
    right = phone_variants(b)
    return bool(left and right and left.intersection(right))


def _sender_key(label: str) -> bool:
    """Return True only for fields that can reasonably contain the payer phone.

    Do not treat a generic Arabic word such as "رقم" as a payer field because it
    can be the transaction/reference number. This prevents false mismatches.
    """
    key = (label or "").strip().lower()
    receiver_words = (
        "receiver", "recipient", "beneficiary", "destination", "to phone",
        "المستلم", "المستفيد", "المستقبل", "الوجهة", "وجهة", "رقم المستلم",
    )
    if any(word in key for word in receiver_words):
        return False

    explicit_sender_words = (
        "sender", "payer", "fromphone", "from phone", "source phone",
        "senderphone", "payerphone", "customerphone", "مرسل", "المرسل",
        "المحول", "المحوّل", "رقم المرسل", "رقم المحول", "رقم المحوّل",
    )
    phone_words = (
        "phone", "mobile", "alternate", "alternatenumber", "هاتف", "جوال",
        "موبايل", "بديل", "الرقم البديل",
    )
    return any(word in key for word in explicit_sender_words + phone_words)


def _phone_values(value: Any) -> list[str]:
    """Extract one or more phone-like values without joining unrelated numbers."""
    if value is None:
        return []
    converted = str(value).translate(ARABIC_DIGITS)
    matches = re.findall(r"(?<!\d)\+?\d(?:[\d\s().-]{1,22}\d)?(?!\d)", converted)
    values: list[str] = []
    for match in matches:
        digits = normalize_phone(match)
        if 3 <= len(digits) <= 20 and digits not in values:
            values.append(digits)
    # Plain API values sometimes contain no separators and are best normalized directly.
    direct = normalize_phone(converted)
    if 3 <= len(direct) <= 20 and direct not in values:
        values.append(direct)
    return values


def extract_sender_phone_candidates(tx: "JaibTx") -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, source: str):
        for digits in _phone_values(value):
            if digits in seen:
                continue
            seen.add(digits)
            candidates.append({"value": digits, "source": source})

    add(tx.sender_phone, "sender_phone")
    # بعض ردود Jaib تضع الرقم أو الرقم البديل داخل حقل اسم/بيانات المرسل.
    add(tx.sender_name, "sender_name")

    def walk(value: Any, path: str = "raw"):
        if isinstance(value, dict):
            label = str(value.get("name") or value.get("label") or value.get("fieldName") or "")
            field_value = value.get("value") if "value" in value else value.get("fieldValue")
            if label and field_value is not None and _sender_key(label):
                add(field_value, f"{path}:{label}")
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if not isinstance(child, (dict, list)) and _sender_key(str(key)):
                    add(child, child_path)
                else:
                    walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(tx.raw)
    return candidates


def match_customer_sender(
    customer: sqlite3.Row,
    tx: "JaibTx",
) -> tuple[Optional[str], Optional[str], Optional[str], list[dict[str, str]]]:
    candidates = extract_sender_phone_candidates(tx)
    for item in candidates:
        candidate = item["value"]
        if phones_match(candidate, customer["phone_primary"]):
            return normalize_phone(customer["phone_primary"]), candidate, item["source"], candidates
        if customer["phone_alt"] and phones_match(candidate, customer["phone_alt"]):
            return normalize_phone(customer["phone_alt"]), candidate, item["source"], candidates
    return None, None, None, candidates

def safe_b64decode(value: str) -> bytes:
    cleaned = (value or "").strip().strip('"').strip("'")
    missing = len(cleaned) % 4
    if missing:
        cleaned += "=" * (4 - missing)
    return base64.b64decode(cleaned)


def _context_user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    data = getattr(context, "user_data", None)
    return data if isinstance(data, dict) else {}


def set_state(context: ContextTypes.DEFAULT_TYPE, state: Optional[str], **payload: Any):
    data = _context_user_data(context)
    if state is None:
        data.pop("state", None)
        data.pop("payload", None)
        data.pop("input_attempts", None)
        return
    data["state"] = state
    data["payload"] = payload
    data["input_attempts"] = 0


def failed_attempt(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int]:
    data = _context_user_data(context)
    attempts = int(data.get("input_attempts", 0)) + 1
    data["input_attempts"] = attempts
    return attempts, max(0, MAX_INPUT_ATTEMPTS - attempts)


def current_state(context: ContextTypes.DEFAULT_TYPE) -> tuple[Optional[str], dict[str, Any]]:
    data = _context_user_data(context)
    payload = data.get("payload")
    return data.get("state"), payload if isinstance(payload, dict) else {}


async def safe_query_answer(query: Any, text: Optional[str] = None, *, show_alert: bool = False) -> bool:
    """Answer a callback without turning an expired Telegram query into a bot error."""
    try:
        await query.answer(text=text, show_alert=show_alert)
        return True
    except Exception as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message or "response timeout expired" in message:
            return False
        raise


def throttle_allowed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    bucket = context.application.bot_data.setdefault("spam_guard", {})
    now = time.time()
    last = bucket.get(user_id, 0)
    if now - last < SPAM_SECONDS:
        return False
    bucket[user_id] = now
    return True


def network_map() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for piece in ALLOWED_NETWORKS_RAW.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            code, label = piece.split(":", 1)
        else:
            code, label = piece, piece
        items.append({"code": code.strip().upper(), "label": label.strip()})
    return items


ALLOWED_NETWORKS = network_map()
NETWORK_LOOKUP = {item["code"]: item["label"] for item in ALLOWED_NETWORKS}


ADMIN_PERMISSION_LABELS: dict[str, str] = {
    "customers_view": "👥 عرض وبحث العملاء",
    "customers_add": "➕ إضافة عميل",
    "customers_edit": "✏️ تعديل العملاء",
    "customers_status": "⛔ إيقاف وتفعيل العملاء",
    "customers_delete": "🗑️ حذف العملاء",
    "customers_link": "🔗 ربط Telegram ID",
    "orders_view": "📝 عرض العمليات",
    "orders_manage": "✅ إدارة العمليات والمراجعات",
    "stats_view": "📊 عرض الإحصائيات",
    "security_view": "🛡️ عرض السجل الأمني",
    "settings_rate": "💲 تعديل السعر",
    "settings_sales": "🟢 تشغيل وإيقاف البيع",
    "settings_payout": "🚀 تشغيل وإيقاف الصرف التلقائي",
    "backup_create": "💾 إنشاء نسخة احتياطية",
    "backup_export": "📤 تصدير البيانات",
    "backup_import": "📥 استعادة نسخة احتياطية",
}
ALL_ADMIN_PERMISSIONS = tuple(ADMIN_PERMISSION_LABELS)


def callback_permission(data: str) -> Optional[str]:
    exact = {
        "adm_add_customer": "customers_add",
        "adm_list_customers": "customers_view",
        "adm_search_customer": "customers_view",
        "adm_set_rate": "settings_rate",
        "adm_toggle_sales": "settings_sales",
        "adm_toggle_payout": "settings_payout",
        "adm_stats": "stats_view",
        "adm_recent_orders": "orders_view",
        "adm_pending_orders": "orders_view",
        "adm_security_log": "security_view",
        "adm_backup": "backup_create",
        "adm_export_csv": "backup_export",
        "adm_import_backup": "backup_import",
    }
    if data in exact:
        return exact[data]
    prefixes = (
        ("cust_add_", "customers_add"),
        ("cust_page:", "customers_view"),
        ("cust_view:", "customers_view"),
        ("cust_edit_menu:", "customers_edit"),
        ("cust_edit_field:", "customers_edit"),
        ("cust_suspend:", "customers_status"),
        ("cust_activate:", "customers_status"),
        ("cust_delete_", "customers_delete"),
        ("cust_link:", "customers_link"),
        ("adm_complete:", "orders_manage"),
        ("adm_fail:", "orders_manage"),
        ("adm_payment_approve:", "orders_manage"),
        ("adm_payment_reject:", "orders_manage"),
    )
    for prefix, permission in prefixes:
        if data.startswith(prefix):
            return permission
    return None


def state_permission(state: Optional[str]) -> Optional[str]:
    return {
        "await_rate": "settings_rate",
        "await_customer_field": "customers_add",
        "await_search_customer": "customers_view",
        "await_edit_customer_field": "customers_edit",
        "await_link_customer_id": "customers_link",
        "await_import_backup": "backup_import",
    }.get(state or "")

def validate_address_for_network(network_code: str, address: str) -> bool:
    patterns = {
        "TRX": r"^T[1-9A-HJ-NP-Za-km-z]{33}$",
        "BSC": r"^(0x)[0-9A-Fa-f]{40}$",
        "ETH": r"^(0x)[0-9A-Fa-f]{40}$",
        "MATIC": r"^(0x)[0-9A-Fa-f]{40}$",
        "ARBITRUM": r"^(0x)[0-9A-Fa-f]{40}$",
        "OPTIMISM": r"^(0x)[0-9A-Fa-f]{40}$",
        "AVAXC": r"^(0x)[0-9A-Fa-f]{40}$",
        "SOL": r"^[1-9A-HJ-NP-Za-km-z]{32,44}$",
    }
    pattern = patterns.get((network_code or "").upper())
    address = (address or "").strip()
    if not pattern:
        return len(address) >= 10 and " " not in address
    return re.fullmatch(pattern, address) is not None




_export_globals(globals())

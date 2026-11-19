"""Core messages, keyboards, customer/admin views, and UI formatting.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# واجهات
# ============================================================


def status_label(status: str) -> str:
    labels = {
        "WAITING_PAYMENT": "⏳ بانتظار الدفع",
        "PAYMENT_VERIFIED": "✅ تم التحقق من الدفع",
        "PAYMENT_REVIEW": "🛡️ بانتظار مراجعة بيانات المرسل",
        "WAITING_DESTINATION": "📥 بانتظار بيانات الاستلام",
        "WAITING_ADMIN_PAYOUT": "🛡️ بانتظار الأدمن",
        "READY_FOR_PAYOUT": "🚀 جاهز للإرسال",
        "PROCESSING_PAYOUT": "🔄 جاري التنفيذ",
        "WITHDRAW_SUBMITTED": "📨 تم إرسال السحب",
        "WITHDRAW_STATUS_UNKNOWN": "⚠️ حالة السحب غير مؤكدة",
        "COMPLETED": "✅ مكتمل",
        "FAILED": "❌ فشل",
        "REJECTED": "🚫 مرفوض",
        "CANCELLED": "🚫 ملغي",
    }
    return labels.get(status, status)


def customer_status_label(status: str) -> str:
    return "🟢 نشط" if status == "ACTIVE" else "⛔ موقوف"


def payment_numbers_text() -> str:
    lines = []
    if JAIB_PRIMARY_PHONE:
        lines.append(render_bot_text('fragment.bot.ui.keyboards.py:payment_numbers_text:41','• الرقم الأساسي: <code>{v0}</code>',[f'{html(JAIB_PRIMARY_PHONE)}']))
    if JAIB_SECONDARY_PHONE:
        lines.append(render_bot_text('fragment.bot.ui.keyboards.py:payment_numbers_text:43','• الرقم البديل: <code>{v0}</code>',[f'{html(JAIB_SECONDARY_PHONE)}']))
    return "\n".join(lines) if lines else "• أضف أرقام الدفع في ملف .env"


def user_main_kb(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [[btn(render_bot_button('bot.ui.keyboards.py:47:button','💵 شراء USDT', []), "buy_usdt", "primary")], [btn(render_bot_button('bot.ui.keyboards.py:47:button','📋 طلباتي', []), "my_orders"), btn(render_bot_button('bot.ui.keyboards.py:47:button','ℹ️ التعليمات', []), "help")]]
    if is_admin:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:49:button','⚙️ لوحة الأدمن', []), "admin_panel", "success")])
    return InlineKeyboardMarkup(rows)


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    permissions = DBX.get_admin_permissions(admin_id)
    rows: list[list[InlineKeyboardButton]] = []

    first: list[InlineKeyboardButton] = []
    if "customers_add" in permissions:
        first.append(btn(render_bot_button('bot.ui.keyboards.py:59:button','➕ إضافة عميل', []), "adm_add_customer", "success"))
    if "customers_view" in permissions:
        first.append(btn(render_bot_button('bot.ui.keyboards.py:61:button','👥 العملاء', []), "adm_list_customers", "primary"))
    if first:
        rows.append(first)
    if "customers_view" in permissions:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:65:button','🔍 بحث عميل', []), "adm_search_customer")])
    if "settings_rate" in permissions:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:67:button','💲 تحديد سعر الدولار', []), "adm_set_rate", "primary")])
    if "settings_sales" in permissions:
        label = "🟢 إيقاف البيع" if DBX.sales_enabled() else "🔴 تفعيل البيع"
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:70:button','{v0}', [str(label)]), "adm_toggle_sales", "danger")])
    if "settings_payout" in permissions:
        label = "🟢 إيقاف الصرف التلقائي" if DBX.auto_payout_enabled() else "🔴 تفعيل الصرف التلقائي"
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:73:button','{v0}', [str(label)]), "adm_toggle_payout", "danger")])

    row: list[InlineKeyboardButton] = []
    if "stats_view" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:77:button','📊 الإحصائيات', []), "adm_stats"))
    if "orders_view" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:79:button','📝 آخر العمليات', []), "adm_recent_orders"))
    if row:
        rows.append(row)

    row = []
    if "security_view" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:85:button','🛡️ السجل الأمني', []), "adm_security_log"))
    if "orders_view" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:87:button','⏳ المعلّقة', []), "adm_pending_orders"))
    if row:
        rows.append(row)

    row = []
    if "backup_create" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:93:button','💾 نسخة احتياطية', []), "adm_backup"))
    if "backup_export" in permissions:
        row.append(btn(render_bot_button('bot.ui.keyboards.py:95:button','📤 تصدير CSV', []), "adm_export_csv"))
    if row:
        rows.append(row)
    if "backup_import" in permissions:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:99:button','📥 استيراد نسخة', []), "adm_import_backup")])
    if int(admin_id) == int(ADMIN_ID):
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:101:button','🛡️ إدارة الأدمن', []), "adm_admins", "primary")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:102:button','🏠 رجوع', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


def customer_detail_text(row: sqlite3.Row) -> str:
    return (
        render_bot_text('fragment.bot.ui.keyboards.py:customer_detail_text:109','<b>بيانات العميل</b>\n\n🆔 رقم العميل: <code>{v0}</code>\n👤 الاسم: <b>{v1}</b>\n📱 الرقم الأساسي: <code>{v2}</code>\n🔁 الرقم البديل: <code>{v3}</code>\n💬 Telegram ID: <code>{v4}</code>\n🔗 المستخدم: @{v5}\n📌 الحالة: <b>{v6}</b>\n📝 الملاحظات: {v7}\n📅 تاريخ الإضافة: <code>{v8}</code>',[f"{row['id']}",f"{html(row['full_name'])}",f"{html(row['phone_primary'])}",f"{html(row['phone_alt'] or 'لا يوجد')}",f"{html(row['telegram_id'] or 'غير مربوط')}",f"{html(row['telegram_username'] or '—')}",f"{customer_status_label(row['status'])}",f"{html(row['notes'] or 'لا توجد')}",f"{html(row['created_at'] or '—')}"])
    )


def customers_list_kb(rows: list[sqlite3.Row], page: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    current = rows[page * page_size:(page + 1) * page_size]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in current:
        icon = "🟢" if row["status"] == "ACTIVE" else "⛔"
        buttons.append([btn(render_bot_button('bot.ui.keyboards.py:128:button','{v0} {v1} | {v2}', [f'{icon}',f"{row['full_name']}",f"{row['phone_primary']}"]), f"cust_view:{row['id']}", "primary")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn(render_bot_button('bot.ui.keyboards.py:131:button','⬅️ السابق', []), f"cust_page:{page - 1}"))
    if page + 1 < pages:
        nav.append(btn(render_bot_button('bot.ui.keyboards.py:133:button','التالي ➡️', []), f"cust_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn(render_bot_button('bot.ui.keyboards.py:136:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(buttons)


def customer_actions_kb(row: sqlite3.Row, admin_id: int) -> InlineKeyboardMarkup:
    permissions = DBX.get_admin_permissions(admin_id)
    rows: list[list[InlineKeyboardButton]] = []
    if "customers_edit" in permissions:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:144:button','✏️ تعديل البيانات', []), f"cust_edit_menu:{row['id']}", "primary")])
    actions: list[InlineKeyboardButton] = []
    if "customers_status" in permissions:
        if row["status"] == "ACTIVE":
            actions.append(btn(render_bot_button('bot.ui.keyboards.py:148:button','⛔ إيقاف', []), f"cust_suspend:{row['id']}", "danger"))
        else:
            actions.append(btn(render_bot_button('bot.ui.keyboards.py:150:button','✅ تفعيل', []), f"cust_activate:{row['id']}", "success"))
    if "customers_link" in permissions:
        actions.append(btn(render_bot_button('bot.ui.keyboards.py:152:button','🔗 ربط تيليجرام', []), f"cust_link:{row['id']}"))
    if actions:
        rows.append(actions)
    if "customers_delete" in permissions:
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:156:button','🗑️ حذف العميل', []), f"cust_delete_confirm:{row['id']}", "danger")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:157:button','↩️ العملاء', []), "adm_list_customers"), btn(render_bot_button('bot.ui.keyboards.py:157:button','🏠 اللوحة', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def customer_edit_kb(customer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(render_bot_button('bot.ui.keyboards.py:163:button','👤 الاسم', []), f"cust_edit_field:{customer_id}:full_name", "primary")],
        [btn(render_bot_button('bot.ui.keyboards.py:164:button','📱 الرقم الأساسي', []), f"cust_edit_field:{customer_id}:phone_primary", "primary")],
        [btn(render_bot_button('bot.ui.keyboards.py:165:button','🔁 الرقم البديل', []), f"cust_edit_field:{customer_id}:phone_alt")],
        [btn(render_bot_button('bot.ui.keyboards.py:166:button','🆔 Telegram ID', []), f"cust_edit_field:{customer_id}:telegram_id")],
        [btn(render_bot_button('bot.ui.keyboards.py:167:button','📝 الملاحظات', []), f"cust_edit_field:{customer_id}:notes")],
        [btn(render_bot_button('bot.ui.keyboards.py:168:button','↩️ رجوع', []), f"cust_view:{customer_id}")],
    ])


def admin_list_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[btn(render_bot_button('bot.ui.keyboards.py:173:button','➕ إضافة أدمن', []), "adm_admin_add", "success")]]
    for row in DBX.list_admins():
        icon = "🟢" if row["status"] == "ACTIVE" else "⛔"
        name = row["display_name"] or row["username"] or str(row["telegram_id"])
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:177:button','{v0} {v1} | {v2}', [f'{icon}',f'{name}',f"{row['telegram_id']}"]), f"adm_admin_view:{row['telegram_id']}", "primary")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:178:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def admin_detail_text(row: sqlite3.Row) -> str:
    permissions = DBX.get_admin_permissions(int(row["telegram_id"]))
    lines = [f"• {ADMIN_PERMISSION_LABELS[key]}" for key in ALL_ADMIN_PERMISSIONS if key in permissions]
    permission_text = "\n".join(lines) or "• لا توجد صلاحيات"
    status = "🟢 نشط" if row["status"] == "ACTIVE" else "⛔ موقوف"
    return (
        render_bot_text('fragment.bot.ui.keyboards.py:admin_detail_text:189','<b>بيانات الأدمن</b>\n\n🆔 Telegram ID: <code>{v0}</code>\n👤 الاسم: <b>{v1}</b>\n🔗 المستخدم: @{v2}\n📌 الحالة: <b>{v3}</b>\n\n<b>الصلاحيات:</b>\n{v4}',[f"{row['telegram_id']}",f"{html(row['display_name'] or '—')}",f"{html(row['username'] or '—')}",f'{status}',f'{permission_text}'])
    )


def admin_permissions_kb(
    permissions: set[str],
    mode: str,
    target_id: Optional[int] = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    suffix = str(target_id or 0)
    for key, label in ADMIN_PERMISSION_LABELS.items():
        mark = "✅" if key in permissions else "▫️"
        rows.append([btn(render_bot_button('bot.ui.keyboards.py:206:button','{v0} {v1}', [f'{mark}',f'{label}']), f"adm_admin_perm:{mode}:{key}:{suffix}")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:207:button','✅ منح جميع الصلاحيات', []), f"adm_admin_all:{mode}:{suffix}", "success")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:208:button','🧹 إلغاء جميع الصلاحيات', []), f"adm_admin_none:{mode}:{suffix}", "danger")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:209:button','💾 حفظ', []), f"adm_admin_save:{mode}:{suffix}", "success")])
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:210:button','❌ إلغاء', []), "adm_admins", "danger")])
    return InlineKeyboardMarkup(rows)


def admin_detail_kb(row: sqlite3.Row) -> InlineKeyboardMarkup:
    target = int(row["telegram_id"])
    status_button = (
        btn(render_bot_button('bot.ui.keyboards.py:217:button','⛔ إيقاف الأدمن', []), f"adm_admin_suspend:{target}", "danger")
        if row["status"] == "ACTIVE"
        else btn(render_bot_button('bot.ui.keyboards.py:219:button','✅ تفعيل الأدمن', []), f"adm_admin_activate:{target}", "success")
    )
    return InlineKeyboardMarkup([
        [btn(render_bot_button('bot.ui.keyboards.py:222:button','🔐 تعديل الصلاحيات', []), f"adm_admin_edit:{target}", "primary")],
        [status_button],
        [btn(render_bot_button('bot.ui.keyboards.py:224:button','🗑️ حذف الأدمن', []), f"adm_admin_delete_confirm:{target}", "danger")],
        [btn(render_bot_button('bot.ui.keyboards.py:225:button','↩️ قائمة الأدمن', []), "adm_admins")],
    ])


def order_payment_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(render_bot_button('bot.ui.keyboards.py:230:button','✅ أرسلت رقم العملية', []), f"pay_ref:{order_id}", "success")], [btn(render_bot_button('bot.ui.keyboards.py:230:button','❌ إلغاء الطلب', []), f"cancel_order:{order_id}", "danger")], [btn(render_bot_button('bot.ui.keyboards.py:230:button','🏠 القائمة الرئيسية', []), "main_menu")]])


def delivery_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(render_bot_button('bot.ui.keyboards.py:234:button','🟡 Binance ID', []), f"delivery_binance:{order_id}", "primary")], [btn(render_bot_button('bot.ui.keyboards.py:234:button','🌐 عنوان شبكة', []), f"delivery_chain:{order_id}", "success")], [btn(render_bot_button('bot.ui.keyboards.py:234:button','🏠 القائمة الرئيسية', []), "main_menu")]])


def network_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [[btn(render_bot_button('bot.ui.keyboards.py:238:button','{v0}', [str(item['label'])]), f"net:{order_id}:{item['code']}", "primary")] for item in ALLOWED_NETWORKS]
    rows.append([btn(render_bot_button('bot.ui.keyboards.py:239:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


def admin_manual_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(render_bot_button('bot.ui.keyboards.py:244:button','✅ تم التنفيذ يدويًا', []), f"adm_complete:{order_id}", "success"), btn(render_bot_button('bot.ui.keyboards.py:244:button','❌ رفض / فشل', []), f"adm_fail:{order_id}", "danger")]])


def backup_file_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(render_bot_button('bot.ui.keyboards.py:248:button','🏠 القائمة الرئيسية', []), "main_menu")]])


def customer_line(row: sqlite3.Row) -> str:
    return (
        f"• ID <code>{row['id']}</code> | {html(row['full_name'])}\n"
        f"  📱 {html(row['phone_primary'])}"
        + (f" | بديل: {html(row['phone_alt'])}" if row['phone_alt'] else "")
        + f"\n  {customer_status_label(row['status'])} | Telegram: <code>{html(row['telegram_id'] or '—')}</code>"
    )


CUSTOMER_FIELD_LABELS = {
    "full_name": "👤 الاسم",
    "phone_primary": "📱 الرقم الأساسي",
    "phone_alt": "🔁 الرقم البديل",
    "telegram_id": "🆔 Telegram ID",
    "notes": "📝 الملاحظات",
}


def customer_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault(
        "customer_draft",
        {
            "full_name": "",
            "phone_primary": "",
            "phone_alt": "",
            "telegram_id": "",
            "notes": "",
        },
    )


def customer_draft_text(draft: dict[str, Any], title: str = "إضافة عميل جديد") -> str:
    return (
        render_bot_text('fragment.bot.ui.keyboards.py:customer_draft_text:285','<b>{v0}</b>\n\n👤 الاسم: <b>{v1}</b>\n📱 الرقم الأساسي: <code>{v2}</code>\n🔁 الرقم البديل: <code>{v3}</code>\n🆔 Telegram ID: <code>{v4}</code>\n📝 الملاحظات: {v5}\n\nاضغط على كل زر لإدخال الحقل، ثم اضغط مراجعة وحفظ.',[f'{html(title)}',f"{html(draft.get('full_name') or '—')}",f"{html(draft.get('phone_primary') or '—')}",f"{html(draft.get('phone_alt') or 'لا يوجد')}",f"{html(draft.get('telegram_id') or '—')}",f"{html(draft.get('notes') or 'لا توجد')}"])
    )


def customer_draft_kb(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    def mark(key: str) -> str:
        return "✅" if str(draft.get(key) or "").strip() else "▫️"

    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.ui.keyboards.py:300:button','{v0} الاسم', [f"{mark('full_name')}"]), "cust_add_field:full_name", "primary")],
            [btn(render_bot_button('bot.ui.keyboards.py:301:button','{v0} الرقم الأساسي', [f"{mark('phone_primary')}"]), "cust_add_field:phone_primary", "primary")],
            [btn(render_bot_button('bot.ui.keyboards.py:302:button','{v0} الرقم البديل', [f"{mark('phone_alt')}"]), "cust_add_field:phone_alt")],
            [btn(render_bot_button('bot.ui.keyboards.py:303:button','{v0} Telegram ID', [f"{mark('telegram_id')}"]), "cust_add_field:telegram_id", "primary")],
            [btn(render_bot_button('bot.ui.keyboards.py:304:button','{v0} الملاحظات', [f"{mark('notes')}"]), "cust_add_field:notes")],
            [btn(render_bot_button('bot.ui.keyboards.py:305:button','🔎 مراجعة البيانات', []), "cust_add_review", "success")],
            [btn(render_bot_button('bot.ui.keyboards.py:306:button','❌ إلغاء الإضافة', []), "cust_add_cancel", "danger")],
        ]
    )


def customer_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.ui.keyboards.py:314:button','✅ تأكيد وحفظ', []), "cust_add_save", "success")],
            [btn(render_bot_button('bot.ui.keyboards.py:315:button','✏️ تعديل الحقول', []), "cust_add_back", "primary")],
            [btn(render_bot_button('bot.ui.keyboards.py:316:button','❌ إلغاء', []), "cust_add_cancel", "danger")],
        ]
    )


def validate_customer_draft(draft: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = str(draft.get("full_name") or "").strip()
    primary = normalize_phone(str(draft.get("phone_primary") or ""))
    alt = normalize_phone(str(draft.get("phone_alt") or ""))
    tg_text = str(draft.get("telegram_id") or "").strip()

    if len(name) < 2 or len(name) > 80:
        errors.append("الاسم يجب أن يكون بين حرفين و80 حرفًا")
    if not primary or not (7 <= len(primary) <= 15):
        errors.append("الرقم الأساسي يجب أن يكون من 7 إلى 15 رقمًا")
    if alt and not (3 <= len(alt) <= 20):
        errors.append("الرقم البديل يجب أن يكون من 3 إلى 20 رقمًا")
    if alt and primary == alt:
        errors.append("الرقم البديل يجب أن يختلف عن الرقم الأساسي")
    if not tg_text.isdigit() or not (5 <= len(tg_text) <= 20):
        errors.append("Telegram ID غير صالح")

    if primary:
        duplicate = DBX.get_customer_by_any_phone(primary)
        if duplicate:
            errors.append(f"الرقم الأساسي مستخدم للعميل ID {duplicate['id']}")
    if alt:
        duplicate = DBX.get_customer_by_any_phone(alt)
        if duplicate:
            errors.append(f"الرقم البديل مستخدم للعميل ID {duplicate['id']}")
    if tg_text.isdigit():
        duplicate_tg = DBX.get_customer_by_telegram(int(tg_text))
        if duplicate_tg:
            errors.append(f"Telegram ID مربوط مسبقًا بالعميل ID {duplicate_tg['id']}")
    return errors


async def show_customer_draft(target: Any, context: ContextTypes.DEFAULT_TYPE, notice: str = ""):
    draft = customer_draft(context)
    text_value = customer_draft_text(draft)
    if notice:
        text_value = f"{notice}\n\n{text_value}"
    await send_or_edit(target, render_bot_text('bot.ui.keyboards.py:359:message','{v0}', [str(text_value)]), customer_draft_kb(draft))


async def send_customer_welcome(context: ContextTypes.DEFAULT_TYPE, row: sqlite3.Row) -> tuple[bool, str]:
    telegram_id = row["telegram_id"]
    if not telegram_id:
        return False, "لا يوجد Telegram ID"
    message = (
        render_bot_text('fragment.bot.ui.keyboards.py:send_customer_welcome:368','👋 أهلاً <b>{v0}</b>\n\nتمت إضافتك إلى <b>{v1}</b> بنجاح ✅\n\n<b>البيانات المسجلة لك:</b>\n👤 الاسم: <b>{v2}</b>\n📱 الرقم الأساسي: <code>{v3}</code>\n🔁 الرقم البديل: <code>{v4}</code>\n🆔 Telegram ID: <code>{v5}</code>\n🟢 حالة الحساب: <b>نشط</b>\n📝 الملاحظات: {v6}\n\n⚠️ يرجى التأكد من صحة جميع بياناتك. إذا وجدت أي خطأ، تواصل مع الأدمن لتعديله:\n{v7}',[f"{html(row['full_name'])}",f'{html(APP_NAME)}',f"{html(row['full_name'])}",f"{html(row['phone_primary'])}",f"{html(row['phone_alt'] or 'لا يوجد')}",f"{html(row['telegram_id'])}",f"{html(row['notes'] or 'لا توجد')}",f'{html(ADMIN_CONTACT)}'])
    )
    try:
        await context.bot.send_message(int(telegram_id), render_bot_text('bot.ui.keyboards.py:380:message','{v0}', [str(message)]), parse_mode=ParseMode.HTML)
        return True, "تم إرسال رسالة الترحيب"
    except Exception as exc:
        log.warning("تعذر إرسال رسالة الترحيب للعميل %s: %s", telegram_id, exc)
        return False, str(exc)


def payment_message(order: sqlite3.Row) -> str:
    return (
        f"<b>طلب شراء USDT</b>\n\n"
        f"🧾 رقم الطلب: <code>{html(order['order_code'])}</code>\n"
        f"💵 الكمية: <b>{html(order['crypto_amount'])} USDT</b>\n"
        f"💱 السعر الحالي: <b>{html(order['exchange_rate_yer'])} ريال</b> لكل 1 USDT\n"
        f"💰 المطلوب تحويله: <b>{html(order['expected_yer_amount'])} ريال يمني</b>\n"
        f"🏦 طريقة الدفع: <b>{html(PAYMENT_METHOD_LABEL)}</b>\n\n"
        f"<b>أرقام الدفع:</b>\n{payment_numbers_text()}\n\n"
        f"<b>تعليمات:</b>\n"
        f"1) حوّل المبلغ نفسه تمامًا.\n"
        f"2) يجب أن تكون العملية صادرة من رقمك الأساسي أو البديل المسجل لدينا.\n"
        f"3) بعد التحويل اضغط <b>أرسلت رقم العملية</b>.\n"
        f"4) أرسل رقم العملية فقط.\n\n"
        f"⚠️ أي عملية لا تطابق رقمك المسجل سيتم رفضها تلقائيًا."
    )


def verify_success_message(order: sqlite3.Row, tx: JaibTx) -> str:
    lines = [
        "<b>✅ وصلت الدفعة وتم التحقق منها بنجاح</b>",
        "",
        f"🧾 رقم الطلب: <code>{html(order['order_code'])}</code>",
        f"🔢 رقم العملية: <code>{html(tx.reference_id)}</code>",
        f"💰 المبلغ الواصل: <b>{html(format_display_amount(tx.amount))} {html(tx.currency)}</b>",
        f"📱 الرقم المسجل المطابق: <code>{html(order['payer_phone_matched'] or '—')}</code>",
        f"🔎 مصدر المطابقة: <code>{html(order['payer_match_source'] or '—')}</code>",
    ]
    if tx.sender_name:
        lines.append(f"👤 اسم/بيانات المرسل: <code>{html(tx.sender_name)}</code>")
    if tx.sender_phone:
        lines.append(f"📞 رقم المرسل الظاهر: <code>{html(tx.sender_phone)}</code>")
    if tx.tx_date:
        lines.append(f"🕒 وقت العملية: <code>{html(tx.tx_date)}</code>")
    lines.extend([
        "",
        "اختر شبكة الاستلام ثم أرسل العنوان. بعد قبول العنوان سيُرسل طلب السحب تلقائيًا.",
    ])
    return "\n".join(lines)


def help_text() -> str:
    return (
        render_bot_text('fragment.bot.ui.keyboards.py:help_text:431','<b>طريقة الاستخدام</b>\n\n1) اضغط <b>شراء USDT</b>.\n2) أرسل كمية لا تقل عن <b>{v0} USDT</b>.\n3) حوّل المبلغ المطلوب إلى محفظة Jaib.\n4) أرسل رقم العملية للتحقق واستخراج بيانات الدفع.\n5) عند مطابقة الرقم الأساسي أو البديل اختر الشبكة.\n6) أرسل عنوان المحفظة، وسيتم السحب عبر العنوان فقط.\n\n⚠️ الاسم للعرض فقط ولا يعتمد عليه البوت في المطابقة. إذا لم يظهر رقم أو لم يتطابق، تتوقف العملية حتى يراجعها الأدمن.',[f'{html(format_display_amount(MIN_USDT_ORDER))}'])
    )




_export_globals(globals())

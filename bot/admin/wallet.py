"""Wallet administration panels and settings UI.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- لوحة الإدارة الجديدة ----------------


_wallet_legacy_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _wallet_legacy_admin_kb(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    if rows and any((button.callback_data or "") == "main_menu" for button in rows[-1]):
        back = rows.pop()
    else:
        back = [btn(render_bot_button('bot.admin.wallet.py:21:button','🏠 رجوع', []), "main_menu")]
    permissions = DBX.get_admin_permissions(admin_id)
    if "wallet_view" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:24:button','💰 نظام المحافظ', []), "adm_wallet_panel", "primary")])
    channel_row: list[InlineKeyboardButton] = []
    if "notification_channel_manage" in permissions:
        channel_row.append(btn(render_bot_button('bot.admin.wallet.py:27:button','📡 إعدادات القنوات', []), "adm_channels_settings"))
    if channel_row:
        rows.append(channel_row)
    rows.append(back)
    return InlineKeyboardMarkup(rows)


def wallet_admin_panel_kb(admin_id: int) -> InlineKeyboardMarkup:
    permissions = DBX.get_admin_permissions(admin_id)
    rows: list[list[InlineKeyboardButton]] = []
    if "wallet_view" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:38:button','📊 ملخص المحافظ', []), "adm_wallet_stats", "primary")])
    if "wallet_transactions" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:40:button','📒 آخر الحركات', []), "adm_wallet_ledger"), btn(render_bot_button('bot.admin.wallet.py:40:button','⏳ العمليات المعلقة', []), "adm_wallet_pending")])
    if "wallet_adjust" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:42:button','➕➖ تعديل رصيد مستخدم', []), "adm_wallet_adjust", "success")])
    if "wallet_settings" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:44:button','⚙️ إعدادات الإيداع والسحب', []), "adm_wallet_settings")])
    if "payout_wallet_manage" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:46:button','👛 حالة Binance', []), "adm_payout_wallet")])
    if "wallet_transactions" in permissions:
        rows.append([btn(render_bot_button('bot.admin.wallet.py:48:button','📤 تصدير سجل المحفظة', []), "adm_wallet_export")])
    rows.append([btn(render_bot_button('bot.admin.wallet.py:49:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def wallet_admin_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.admin.wallet.py:56:button','🟢/🔴 الإيداع', []), "adm_wallet_toggle_deposit"), btn(render_bot_button('bot.admin.wallet.py:56:button','🟢/🔴 السحب', []), "adm_wallet_toggle_withdraw")],
            [btn(render_bot_button('bot.admin.wallet.py:57:button','💧 إظهار/إخفاء السيولة', []), "adm_wallet_toggle_liquidity")],
            [btn(render_bot_button('bot.admin.wallet.py:58:button','🚨 تشغيل/إيقاف تنبيه السيولة', []), "adm_wallet_toggle_liquidity_alert")],
            [btn(render_bot_button('bot.admin.wallet.py:59:button','🔔 تعديل حد تنبيه السيولة', []), "adm_wallet_set_liquidity_alert")],
            [btn(render_bot_button('bot.admin.wallet.py:60:button','✅ تعديل حد استعادة السيولة', []), "adm_wallet_set_liquidity_recovery")],
            [btn(render_bot_button('bot.admin.wallet.py:61:button','🔢 تعديل الخانات العشرية', []), "adm_wallet_set_display_decimals")],
            [btn(render_bot_button('bot.admin.wallet.py:62:button','📈 تشغيل/إيقاف حد العملية', []), "adm_wallet_toggle_withdraw_limit")],
            [btn(render_bot_button('bot.admin.wallet.py:63:button','✏️ تعديل الحد الأقصى', []), "adm_wallet_set_withdraw_limit", "primary")],
            [btn(render_bot_button('bot.admin.wallet.py:64:button','🛡️ تعديل احتياطي السيولة', []), "adm_wallet_set_liquidity_reserve")],
            [btn(render_bot_button('bot.admin.wallet.py:65:button','💲 تعديل سعر الصرف', []), "adm_set_rate", "primary")],
            [btn(render_bot_button('bot.admin.wallet.py:66:button','↩️ نظام المحافظ', []), "adm_wallet_panel")],
        ]
    )


def wallet_admin_settings_text() -> str:
    return (
        render_bot_text('fragment.bot.admin.wallet.py:wallet_admin_settings_text:74','<b>⚙️ إعدادات المحفظة الداخلية</b>\n\n💲 سعر 1 USDT: <b>{v0} YER</b>\n📥 الإيداع: <b>{v1}</b>\n📤 السحب: <b>{v2}</b>\n🤖 سحب Binance: <b>تلقائي</b>\n🔢 الخانات العشرية المعروضة: <b>{v3}</b>\n💧 عرض السيولة للمستخدم: <b>{v4}</b>\n🛡️ احتياطي غير معروض: <b>{v5} USDT</b>\n🚨 تنبيه انخفاض السيولة: <b>{v6}</b>\n🔔 حد التنبيه: <b>{v7} USDT</b>\n✅ حد الاستعادة: <b>{v8} USDT</b>\n📈 حد العملية: <b>{v9}</b>\n🔢 قيمة الحد: <b>{v10} USDT</b>\n💵 أقل سحب: <b>ديناميكي من Binance</b>\n🛡️ رسوم العميل: <b>الرسوم الحية ناقص 0.01 USDT</b>\n🌐 شبكات Binance المفعلة: <b>{v11}</b> — <b>{v12}</b>',[f'{html(format_yer_amount(DBX.get_rate()))}',f"{('مفعل' if wallet_deposit_enabled() else 'متوقف')}",f"{('مفعل' if wallet_withdraw_enabled() else 'متوقف')}",f'{display_decimals()}',f"{('مفعل' if public_liquidity_enabled() else 'متوقف')}",f'{html(format_display_amount(public_liquidity_reserve()))}',f"{('مفعل' if liquidity_alert_enabled() else 'متوقف')}",f'{html(format_display_amount(liquidity_alert_threshold()))}',f'{html(format_display_amount(liquidity_alert_recovery()))}',f"{('مفعل' if withdrawal_operation_limit_enabled() else 'متوقف')}",f'{html(format_display_amount(withdrawal_operation_limit()))}',f'{len(wallet_network_routes())}',f"{('جاهزة' if BINANCE_PROVIDER.enabled() and (not binance_auth_state()['blocked']) else 'متوقفة مؤقتًا')}"])
    )



def wallet_withdraw_detail_text(row: sqlite3.Row) -> str:
    status_labels = {
        "AUTO_PENDING": "قيد بدء التنفيذ التلقائي",
        "PENDING_REVIEW": "قيد المراجعة",
        "PROCESSING": "قيد التنفيذ",
        "SUBMITTED": "أُرسلت وتنتظر تأكيد الشبكة",
        "STATUS_UNKNOWN": "النتيجة غير مؤكدة",
        "MANUAL_REVIEW": "مراجعة يدوية",
        "COMPLETED": "مكتملة",
        "FAILED_RELEASED": "فشلت وأعيد الرصيد",
        "EXPIRED_RELEASED": "انتهت وأعيد الرصيد",
        "CANCELLED": "ملغاة",
    }
    status_text = status_labels.get(str(row["status"]), str(row["status"]))
    return (
        "<b>📄 تفاصيل السحب</b>\n\n"
        f"🧾 <code>{html(row['withdrawal_code'])}</code>\n"
        f"👤 <code>{html(row['telegram_user_id'])}</code>\n"
        f"💵 <b>{html(format_display_amount(row['amount_usdt']))} USDT</b>\n"
        f"🌐 {html(row['network_label'])}\n"
        f"🏦 {html(row['provider'])}\n"
        f"📬 <code>{html(row['destination_address'])}</code>\n"
        f"📌 الحالة: <b>{html(status_text)}</b>\n"
        f"🆔 Provider ID: <code>{html(row['provider_withdraw_id'] or '—')}</code>\n"
        f"🔗 TxID: <code>{html(normalize_txid(row['txid']) or '—')}</code>\n"
        f"❗ الخطأ: <code>{html(row['error_message'] or '—')}</code>\n"
        f"📅 {html(row['created_at'])}"
    )[:4000]




_export_globals(globals())

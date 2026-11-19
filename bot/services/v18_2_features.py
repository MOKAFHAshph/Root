"""V18.2 customer pricing, withdrawal floors, tutorials, and safe copy UX.

This layer is intentionally loaded after V18.1.  It extends the existing
SQLite source of truth and wraps the final Telegram handlers without
duplicating balances, withdrawals, or transaction history.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
try:
    from telegram import CopyTextButton
except ImportError:  # pragma: no cover - compatibility for lightweight tests
    CopyTextButton = None
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals

_seed_globals(globals())


_TUTORIAL_ICONS = ("📹", "🎬", "📖", "ℹ️")
_RATE_MIN = Decimal("1")
_RATE_MAX = Decimal("1000000")


def _v18_2_schema() -> None:
    """Add only nullable/defaulted columns and independent configuration rows."""
    with DBX.connect() as con:
        customer_columns = {
            str(row["name"]) for row in con.execute("PRAGMA table_info(customers)").fetchall()
        }
        if "custom_exchange_rate_yer" not in customer_columns:
            con.execute("ALTER TABLE customers ADD COLUMN custom_exchange_rate_yer TEXT")

        network_columns = {
            str(row["name"]) for row in con.execute("PRAGMA table_info(managed_networks)").fetchall()
        }
        if "minimum_floor" not in network_columns:
            con.execute("ALTER TABLE managed_networks ADD COLUMN minimum_floor TEXT")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tutorial_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                button_code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '📹',
                custom_emoji_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_tutorial_buttons_active_order "
            "ON tutorial_buttons(active, sort_order, id)"
        )
        tutorial_columns = {
            str(row["name"]) for row in con.execute("PRAGMA table_info(tutorial_buttons)").fetchall()
        }
        if "custom_emoji_id" not in tutorial_columns:
            con.execute("ALTER TABLE tutorial_buttons ADD COLUMN custom_emoji_id TEXT")
        con.commit()


_v18_2_schema()


def _valid_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("سعر الصرف غير صالح") from exc
    if rate < _RATE_MIN or rate > _RATE_MAX:
        raise ValueError("سعر الصرف يجب أن يكون أكبر من صفر وضمن النطاق الآمن")
    return rate


def customer_custom_rate(customer: Any) -> Optional[Decimal]:
    if customer is None:
        return None
    try:
        raw = customer["custom_exchange_rate_yer"]
    except (KeyError, IndexError, TypeError):
        raw = None
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = _valid_rate(raw)
    except ValueError:
        log.error("Invalid custom customer rate customer=%s", customer.get("id") if isinstance(customer, dict) else "unknown")
        return None
    return value


def effective_customer_rate(telegram_user_id: int | None = None, *, customer: Any = None) -> Decimal:
    row = customer
    if row is None and telegram_user_id is not None:
        row = DBX.get_customer_by_telegram(int(telegram_user_id))
    return customer_custom_rate(row) or _valid_rate(DBX.get_rate())


def set_customer_custom_rate(customer_id: int, value: Any | None) -> Any:
    current = DBX.get_customer(int(customer_id))
    if not current:
        raise ValueError("العميل غير موجود")
    stored = None if value is None or str(value).strip() in {"", "-", "0"} else q2(_valid_rate(value))
    DBX.update_customer(int(customer_id), custom_exchange_rate_yer=stored)
    return DBX.get_customer(int(customer_id))


def managed_network_minimum_floor(network_code: str) -> Decimal:
    row = get_managed_network(str(network_code or "").upper())
    if not row:
        return Decimal("0")
    try:
        raw = row["minimum_floor"]
    except (KeyError, IndexError):
        raw = None
    value = decimal_or_zero(raw)
    return max(Decimal("0"), value)


def set_managed_network_minimum_floor(network_id: int, value: Any | None) -> Any:
    with DBX.connect() as con:
        row = con.execute("SELECT * FROM managed_networks WHERE id=?", (int(network_id),)).fetchone()
        if not row:
            raise ValueError("الشبكة غير موجودة")
        text = str(value or "").strip()
        if text in {"", "-", "0"}:
            stored = None
        else:
            minimum = decimal_or_zero(text)
            if minimum <= 0 or minimum > Decimal("1000000"):
                raise ValueError("الحد الأدنى الإداري يجب أن يكون أكبر من صفر")
            stored = q8(minimum)
        con.execute(
            "UPDATE managed_networks SET minimum_floor=?, updated_at=? WHERE id=?",
            (stored, now_str(), int(network_id)),
        )
        con.commit()
        return con.execute("SELECT * FROM managed_networks WHERE id=?", (int(network_id),)).fetchone()


def effective_network_minimum(network_code: str, provider_minimum: Any) -> Decimal:
    return max(wallet_decimal(provider_minimum or 0), managed_network_minimum_floor(network_code))


_v18_2_live_limits = binance_live_limits


def binance_live_limits(network_code: str, *, force_refresh: bool = True) -> dict[str, Any]:
    """Combine Binance's live minimum with the administrator's business floor."""
    result = dict(_v18_2_live_limits(network_code, force_refresh=force_refresh))
    provider_minimum = wallet_decimal(result.get("minimum") or 0)
    admin_floor = managed_network_minimum_floor(network_code)
    result["provider_minimum"] = provider_minimum
    result["admin_minimum_floor"] = admin_floor
    result["minimum"] = max(provider_minimum, admin_floor)
    return result


_OFF_CHAIN_TXID_RE = re.compile(
    r"^\s*off[\s-]*chain\s+transfer(?:\s+(?:id|txid))?\s*[:#-]?\s*([0-9]+)\s*$",
    re.IGNORECASE,
)


def normalize_txid(txid: Any) -> str:
    """Return the identifier users must copy, without Binance's label.

    Binance represents an internal transfer as e.g. ``Off-chain transfer
    400391218260``.  Merchant forms expect the numeric identifier only.  Real
    on-chain hashes and all unknown formats remain unchanged.
    """
    value = str(txid or "").strip()
    match = _OFF_CHAIN_TXID_RE.fullmatch(value)
    return match.group(1) if match else value


def mask_txid(txid: str) -> str:
    value = normalize_txid(txid)
    if len(value) < 12:
        return value
    return value[:6] + "••••••••••••" + value[-4:]


def txid_copy_keyboard(txid: str, *, include_home: bool = True) -> InlineKeyboardMarkup:
    value = normalize_txid(txid)
    rows: list[list[InlineKeyboardButton]] = []
    if value and CopyTextButton is not None:
        rows.append([InlineKeyboardButton(render_bot_button('bot.services.v18_2_features.py:204:button','📋 نسخ TxID', []), copy_text=CopyTextButton(value))])
    if include_home:
        rows.append([btn(render_bot_button('bot.services.v18_2_features.py:206:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


def _validate_tutorial_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or len(url) > 1000:
        raise ValueError("الرابط يجب أن يكون HTTPS صالحًا")
    return url


def list_tutorial_buttons(*, active_only: bool = False) -> list[Any]:
    where = "WHERE active=1" if active_only else ""
    with DBX.connect() as con:
        return con.execute(
            f"SELECT * FROM tutorial_buttons {where} ORDER BY sort_order, id"
        ).fetchall()


def get_tutorial_button(button_id: int) -> Any:
    with DBX.connect() as con:
        return con.execute("SELECT * FROM tutorial_buttons WHERE id=?", (int(button_id),)).fetchone()


def _validate_custom_emoji_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if text in {"", "-", "0"}:
        return None
    if not re.fullmatch(r"[0-9]{5,40}", text):
        raise ValueError("معرّف الرمز المتحرك يجب أن يكون رقم Custom Emoji صالحًا")
    return text


def _custom_emoji_input(message: Any, fallback: str) -> Any:
    """Accept either a pasted Premium emoji entity or its raw Telegram id."""
    entities = list(getattr(message, "entities", None) or [])
    entities.extend(list(getattr(message, "caption_entities", None) or []))
    for entity in entities:
        custom_id = getattr(entity, "custom_emoji_id", None)
        if custom_id:
            return str(custom_id)
    return fallback


def create_tutorial_button(
    name: str,
    url: str,
    icon: str,
    *,
    admin_id: int,
    custom_emoji_id: Any = None,
) -> Any:
    label = str(name or "").strip()
    if len(label) < 2 or len(label) > 100:
        raise ValueError("اسم الزر يجب أن يكون بين حرفين و100 حرف")
    clean_icon = str(icon or "").strip()
    if clean_icon not in _TUTORIAL_ICONS:
        raise ValueError("أيقونة الزر غير مدعومة")
    clean_url = _validate_tutorial_url(url)
    with DBX.connect() as con:
        maximum = con.execute("SELECT COALESCE(MAX(sort_order),0) FROM tutorial_buttons").fetchone()[0]
        cur = con.execute(
            """
            INSERT INTO tutorial_buttons(
                button_code, name, url, icon, custom_emoji_id, active,
                sort_order, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                f"btn_{uuid.uuid4().hex[:12]}", label, clean_url, clean_icon,
                _validate_custom_emoji_id(custom_emoji_id), int(maximum or 0) + 10,
                int(admin_id), now_str(), now_str(),
            ),
        )
        con.commit()
        return con.execute("SELECT * FROM tutorial_buttons WHERE id=?", (int(cur.lastrowid),)).fetchone()


def update_tutorial_button(button_id: int, **fields: Any) -> Any:
    row = get_tutorial_button(button_id)
    if not row:
        raise ValueError("الزر التعليمي غير موجود")
    updates: dict[str, Any] = {}
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if len(name) < 2 or len(name) > 100:
            raise ValueError("اسم الزر غير صالح")
        updates["name"] = name
    if "url" in fields:
        updates["url"] = _validate_tutorial_url(str(fields["url"] or ""))
    if "icon" in fields:
        icon = str(fields["icon"] or "").strip()
        if icon not in _TUTORIAL_ICONS:
            raise ValueError("أيقونة الزر غير مدعومة")
        updates["icon"] = icon
    if "custom_emoji_id" in fields:
        updates["custom_emoji_id"] = _validate_custom_emoji_id(fields["custom_emoji_id"])
    if "active" in fields:
        updates["active"] = 1 if bool(fields["active"]) else 0
    if "sort_order" in fields:
        order = int(fields["sort_order"])
        if not -100000 <= order <= 100000:
            raise ValueError("ترتيب الزر خارج النطاق")
        updates["sort_order"] = order
    if updates:
        updates["updated_at"] = now_str()
        columns = ", ".join(f"{key}=?" for key in updates)
        with DBX.connect() as con:
            con.execute(
                f"UPDATE tutorial_buttons SET {columns} WHERE id=?",
                (*updates.values(), int(button_id)),
            )
            con.commit()
    return get_tutorial_button(button_id)


def delete_tutorial_button(button_id: int) -> None:
    with DBX.connect() as con:
        changed = con.execute("DELETE FROM tutorial_buttons WHERE id=?", (int(button_id),))
        con.commit()
    if changed.rowcount != 1:
        raise ValueError("الزر التعليمي غير موجود")


def tutorial_custom_emoji_enabled() -> bool:
    """Keep Bot API custom icons off until the owner confirms eligibility."""
    return str(DBX.get_setting("tutorial_custom_emoji_enabled") or "0") == "1"


def set_tutorial_custom_emoji_enabled(value: bool) -> None:
    DBX.set_setting("tutorial_custom_emoji_enabled", "1" if value else "0")


def _tutorial_url_button(row: Any) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {"url": row["url"]}
    custom_id = str(row["custom_emoji_id"] or "").strip()
    animated = bool(custom_id and tutorial_custom_emoji_enabled())
    if animated:
        kwargs["icon_custom_emoji_id"] = custom_id
    try:
        label = str(row["name"]) if animated else f"{row['icon']} {row['name']}"
        return InlineKeyboardButton(render_bot_button('bot.services.v18_2_features.py:348:button','{v0}', [str(label)]), **kwargs)
    except TypeError:
        kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(render_bot_button('bot.services.v18_2_features.py:351:button','{v0} {v1}', [f"{row['icon']}",f"{row['name']}"]), **kwargs)


_v18_2_help_keyboard = help_keyboard


def help_keyboard() -> InlineKeyboardMarkup:
    base = _v18_2_help_keyboard()
    rows: list[list[InlineKeyboardButton]] = []
    for row in list_tutorial_buttons(active_only=True):
        rows.append([_tutorial_url_button(row)])
    legacy_rows = [list(item) for item in base.inline_keyboard]
    seen_urls = {str(button.url or "") for item in rows for button in item}
    for item in legacy_rows:
        if any(button.url and str(button.url) in seen_urls for button in item):
            continue
        rows.append(item)
    return InlineKeyboardMarkup(rows)


_v18_2_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _v18_2_admin_kb(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    back = rows.pop() if rows and any((button.callback_data or "") == "main_menu" for button in rows[-1]) else [btn(render_bot_button('bot.services.v18_2_features.py:377:button','🏠 رجوع', []), "main_menu")]
    if DBX.has_permission(int(admin_id), "wallet_settings"):
        rows.append([btn(render_bot_button('bot.services.v18_2_features.py:379:button','📚 إدارة الأزرار التعليمية', []), "adm_tutorials", "primary")])
    rows.append(back)
    return InlineKeyboardMarkup(rows)


_v18_2_customer_detail_text = customer_detail_text
_v18_2_customer_actions_kb = customer_actions_kb


def customer_detail_text(row: Any) -> str:
    special = customer_custom_rate(row)
    general = DBX.get_rate()
    return (
        _v18_2_customer_detail_text(row)
        + "\n💱 سعر الصرف الخاص: <b>"
        + (f"{html(q2(special))} YER" if special is not None else "غير مخصص — يستخدم السعر العام")
        + "</b>"
        + f"\n💲 السعر الفعلي الآن: <b>{html(q2(special or general))} YER = 1 USDT</b>"
    )


def customer_actions_kb(row: Any, admin_id: int) -> InlineKeyboardMarkup:
    base = _v18_2_customer_actions_kb(row, admin_id)
    rows = [list(item) for item in base.inline_keyboard]
    if DBX.has_permission(int(admin_id), "customers_edit"):
        special = customer_custom_rate(row)
        insert_at = max(0, len(rows) - 1)
        rate_buttons = [btn(render_bot_button('bot.services.v18_2_features.py:406:button','💱 تعيين سعر خاص', []), f"cust_rate_set:{row['id']}", "primary")]
        if special is not None:
            rate_buttons.append(btn(render_bot_button('bot.services.v18_2_features.py:408:button','↩️ إلغاء السعر الخاص', []), f"cust_rate_clear:{row['id']}", "danger"))
        rows.insert(insert_at, rate_buttons)
    return InlineKeyboardMarkup(rows)


def _tutorial_admin_text() -> str:
    rows = list_tutorial_buttons()
    lines = [
        "<b>⚙️ إدارة الأزرار التعليمية</b>",
        "",
        "✨ <b>الرمز المتحرك الاختياري:</b> عند الإضافة أو التعديل أرسل الرمز "
        "المتحرك نفسه ليقرأ البوت معرّفه تلقائيًا، أو أرسل المعرّف يدويًا. "
        "أرسل <code>-</code> لمسحه. سيبقى الرمز العادي ظاهرًا دائمًا كبديل آمن.",
        "حالة إرسال الرموز المتحركة الآن: <b>"
        + ("مفعلة" if tutorial_custom_emoji_enabled() else "متوقفة حتى تأكيد Premium")
        + "</b>",
        "",
    ]
    if not rows:
        lines.append(render_bot_text('fragment.bot.services.v18_2_features.py:_tutorial_admin_text:428','لا توجد أزرار تعليمية مخصصة حتى الآن.',[]))
    for row in rows:
        state = "✅" if int(row["active"] or 0) else "⛔"
        lines.append(f"{state} {html(row['icon'])} <b>{html(row['name'])}</b>")
    return "\n".join(lines)


def _tutorial_admin_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[btn(render_bot_button('bot.services.v18_2_features.py:435:button','➕ إضافة زر جديد', []), "adm_tutorial_add", "success")]]
    for row in list_tutorial_buttons():
        state = "✅" if int(row["active"] or 0) else "⛔"
        rows.append([btn(render_bot_button('bot.services.v18_2_features.py:438:button','{v0} {v1} {v2}', [f'{state}',f"{row['icon']}",f"{row['name']}"]), f"adm_tutorial_view:{row['id']}")])
    rows.append(
        [
            btn(
                render_bot_button('bot.services.v18_2_features.py:441:button','{v0}', [str('⏸ إيقاف الرموز المتحركة' if tutorial_custom_emoji_enabled() else '✨ تفعيل بعد اشتراك Premium')]),
                "adm_tutorial_emoji_toggle",
                "primary",
            )
        ]
    )
    rows.append([btn(render_bot_button('bot.services.v18_2_features.py:448:button','👁️ معاينة', []), "adm_tutorial_preview"), btn(render_bot_button('bot.services.v18_2_features.py:448:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def _tutorial_detail_text(row: Any) -> str:
    return (
        render_bot_text('fragment.bot.services.v18_2_features.py:_tutorial_detail_text:455','<b>📚 الزر التعليمي</b>\n\nالاسم: <b>{v0} {v1}</b>\nالرابط: <code>{v2}</code>\nالحالة: <b>{v3}</b>\nالرمز المتحرك: <code>{v4}</code>\nالترتيب: <b>{v5}</b>',[f"{html(row['icon'])}",f"{html(row['name'])}",f"{html(row['url'])}",f"{('مفعل' if int(row['active'] or 0) else 'متوقف')}",f"{html(row['custom_emoji_id'] or 'غير محدد')}",f"{int(row['sort_order'])}"])
    )


def _tutorial_detail_kb(row: Any) -> InlineKeyboardMarkup:
    button_id = int(row["id"])
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.v18_2_features.py:467:button','✏️ الاسم', []), f"adm_tutorial_name:{button_id}"), btn(render_bot_button('bot.services.v18_2_features.py:467:button','🔗 الرابط', []), f"adm_tutorial_url:{button_id}")],
            [btn(render_bot_button('bot.services.v18_2_features.py:468:button','🎨 الأيقونة', []), f"adm_tutorial_icon:{button_id}"), btn(render_bot_button('bot.services.v18_2_features.py:468:button','✨ الرمز المتحرك', []), f"adm_tutorial_custom_emoji:{button_id}")],
            [btn(render_bot_button('bot.services.v18_2_features.py:469:button','{v0}', [str('إيقاف' if int(row['active'] or 0) else 'تفعيل')]), f"adm_tutorial_toggle:{button_id}")],
            [btn(render_bot_button('bot.services.v18_2_features.py:470:button','⬆️ للأعلى', []), f"adm_tutorial_move:{button_id}:-1"), btn(render_bot_button('bot.services.v18_2_features.py:470:button','⬇️ للأسفل', []), f"adm_tutorial_move:{button_id}:1")],
            [btn(render_bot_button('bot.services.v18_2_features.py:471:button','🗑️ حذف', []), f"adm_tutorial_delete:{button_id}", "danger")],
            [btn(render_bot_button('bot.services.v18_2_features.py:472:button','↩️ الأزرار', []), "adm_tutorials")],
        ]
    )


async def cmd_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:480:message','⛔ لا تملك صلاحية إدارة الأزرار التعليمية.', []))
        return
    set_state(context, None)
    await update.effective_message.reply_text(
        render_bot_text('bot.services.v18_2_features.py:483:message','{v0}', [str(_tutorial_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_admin_kb()
    )


async def cmd_setminwithdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:491:message','⛔ لا تملك صلاحية تعديل حد السحب.', []))
        return
    if not context.args:
        floor = managed_network_minimum_floor("BSC")
        await update.effective_message.reply_text(
            render_bot_text('bot.services.v18_2_features.py:495:message','الاستخدام: <code>/setminwithdraw 10</code>\nالحد الإداري الحالي لـBSC: <b>{v0} USDT</b>', [f'{html(format_display_amount(floor))}']),
            parse_mode=ParseMode.HTML,
        )
        return
    row = get_managed_network("BSC")
    if not row:
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:502:message','❌ شبكة BSC غير موجودة في الإعدادات.', []))
        return
    try:
        updated = set_managed_network_minimum_floor(int(row["id"]), context.args[0].translate(ARABIC_DIGITS))
    except ValueError as exc:
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:507:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
        return
    DBX.audit(user.id, actor_name(user), "SET_WITHDRAW_MINIMUM_FLOOR", "managed_network", row["id"], q8(updated["minimum_floor"] or 0))
    await update.effective_message.reply_text(
        render_bot_text('bot.services.v18_2_features.py:510:message','✅ تم تحديث الحد الأدنى الإداري لـBSC.\nسيستخدم النظام تلقائيًا القيمة الأعلى بينه وبين حد Binance الحي.', [])
    )


_v18_2_previous_on_callback = on_callback
_v18_2_previous_on_message = on_message


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = str(query.data or "")
    user = query.from_user

    if data.startswith("cust_rate_set:") or data.startswith("cust_rate_clear:"):
        if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "customers_edit"):
            await query.answer("لا تملك صلاحية تعديل العملاء", show_alert=True)
            return
        customer_id = int(data.split(":", 1)[1])
        if data.startswith("cust_rate_clear:"):
            updated = set_customer_custom_rate(customer_id, None)
            DBX.audit(user.id, actor_name(user), "CLEAR_CUSTOMER_RATE", "customer", customer_id)
            await query.answer("تم إلغاء السعر الخاص")
            await query.edit_message_text(
                render_bot_text('bot.services.v18_2_features.py:534:message','{v0}', [str(customer_detail_text(updated))]), parse_mode=ParseMode.HTML,
                reply_markup=customer_actions_kb(updated, int(user.id)),
            )
            return
        set_state(context, "await_v18_2_customer_rate", customer_id=customer_id)
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.v18_2_features.py:541:message','<b>💱 تعيين سعر صرف خاص</b>\n\nأرسل سعر 1 USDT بالريال، مثال: <code>545</code>.\nسيطبق على العمليات الجديدة فقط.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:544:button','إلغاء', []), f"cust_view:{customer_id}")]]),
        )
        return

    if data == "adm_v18_networks" or data.startswith("adm_v18_network:"):
        # Only replace the detail page. The list remains owned by V18.1.
        if data.startswith("adm_v18_network:"):
            if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
                await query.answer("لا تملك الصلاحية", show_alert=True)
                return
            network_id = int(data.split(":", 1)[1])
            with DBX.connect() as con:
                row = con.execute("SELECT * FROM managed_networks WHERE id=?", (network_id,)).fetchone()
            if not row:
                await query.answer("الشبكة غير موجودة", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                render_bot_text('bot.services.v18_2_features.py:561:message','<b>🌐 إعداد الشبكة</b>\n\nالاسم: <b>{v0}</b>\nالرمز: <code>{v1}</code>\nالحالة: <b>{v2}</b>\nالحد الأدنى الإداري: <b>{v3} USDT</b>\nالقيمة الفعلية هي الأعلى بين هذا الحد وحد Binance الحي.\nالرسوم البديلة: <b>{v4}</b>', [f"{html(row['display_label'])}",f"{html(row['network_code'])}",f"{('مفعلة' if int(row['enabled'] or 0) else 'متوقفة')}",f"{html(format_display_amount(row['minimum_floor'] or 0))}",f"{html(row['fee_override'] or 'حية من Binance')}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(render_bot_button('bot.services.v18_2_features.py:572:button','✏️ الحد الأدنى الإداري', []), f"adm_v18_2_network_min:{network_id}", "primary")],
                        [btn(render_bot_button('bot.services.v18_2_features.py:573:button','{v0}', [str('إيقاف الشبكة' if int(row['enabled'] or 0) else 'تفعيل الشبكة')]), f"adm_v18_network_toggle:{network_id}")],
                        [btn(render_bot_button('bot.services.v18_2_features.py:574:button','✏️ عنوان المحفظة', []), f"adm_v18_network_wallet:{network_id}"), btn(render_bot_button('bot.services.v18_2_features.py:574:button','💸 رسوم بديلة', []), f"adm_v18_network_fee:{network_id}")],
                        [btn(render_bot_button('bot.services.v18_2_features.py:575:button','↩️ الشبكات', []), "adm_v18_networks")],
                    ]
                ),
            )
            return

    if data.startswith("adm_v18_2_network_min:"):
        if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        network_id = int(data.split(":", 1)[1])
        set_state(context, "await_v18_2_network_minimum", network_id=network_id)
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.v18_2_features.py:588:message','أرسل الحد الأدنى الإداري بـUSDT، أو أرسل <code>-</code> لاستخدام حد Binance الحي فقط.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:591:button','إلغاء', []), f"adm_v18_network:{network_id}")]]),
        )
        return

    if data == "adm_tutorials" or data.startswith("adm_tutorial_"):
        if not DBX.is_admin(int(user.id)) or not DBX.has_permission(int(user.id), "wallet_settings"):
            await query.answer("لا تملك صلاحية إدارة التعليمات", show_alert=True)
            return
        await query.answer()
        if data == "adm_tutorials":
            set_state(context, None)
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:602:message','{v0}', [str(_tutorial_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_admin_kb())
            return
        if data == "adm_tutorial_add":
            set_state(context, "await_tutorial_add_name")
            context.user_data.pop("tutorial_draft", None)
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:607:message','1️⃣ أرسل اسم الزر التعليمي.', []), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:607:button','إلغاء', []), "adm_tutorials")]]))
            return
        if data == "adm_tutorial_emoji_toggle":
            enabled = not tutorial_custom_emoji_enabled()
            set_tutorial_custom_emoji_enabled(enabled)
            DBX.audit(
                user.id,
                actor_name(user),
                "TOGGLE_TUTORIAL_CUSTOM_EMOJI",
                "settings",
                "tutorial_custom_emoji_enabled",
                f"enabled={int(enabled)}",
            )
            await query.edit_message_text(
                render_bot_text('bot.services.v18_2_features.py:620:message','{v0}', [str(_tutorial_admin_text())]),
                parse_mode=ParseMode.HTML,
                reply_markup=_tutorial_admin_kb(),
            )
            return
        if data == "adm_tutorial_preview":
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:627:message','<b>📚 التعليمات — معاينة</b>', []), parse_mode=ParseMode.HTML, reply_markup=help_keyboard())
            return
        if data.startswith("adm_tutorial_view:"):
            row = get_tutorial_button(int(data.split(":", 1)[1]))
            if not row:
                await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:632:message','الزر غير موجود.', []), reply_markup=_tutorial_admin_kb())
                return
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:634:message','{v0}', [str(_tutorial_detail_text(row))]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_detail_kb(row))
            return
        if data.startswith("adm_tutorial_toggle:"):
            button_id = int(data.rsplit(":", 1)[1])
            row = get_tutorial_button(button_id)
            updated = update_tutorial_button(button_id, active=not bool(int(row["active"] or 0)))
            DBX.audit(user.id, actor_name(user), "TOGGLE_TUTORIAL_BUTTON", "tutorial_button", button_id, f"active={updated['active']}")
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:641:message','{v0}', [str(_tutorial_detail_text(updated))]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_detail_kb(updated))
            return
        if data.startswith("adm_tutorial_move:"):
            _, button_id, direction = data.split(":")
            row = get_tutorial_button(int(button_id))
            updated = update_tutorial_button(int(button_id), sort_order=int(row["sort_order"]) + int(direction) * 10)
            DBX.audit(user.id, actor_name(user), "MOVE_TUTORIAL_BUTTON", "tutorial_button", button_id, f"order={updated['sort_order']}")
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:648:message','{v0}', [str(_tutorial_detail_text(updated))]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_detail_kb(updated))
            return
        if data.startswith("adm_tutorial_delete_confirm:"):
            button_id = int(data.rsplit(":", 1)[1])
            delete_tutorial_button(button_id)
            DBX.audit(user.id, actor_name(user), "DELETE_TUTORIAL_BUTTON", "tutorial_button", button_id)
            await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:654:message','{v0}', [str(_tutorial_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_admin_kb())
            return
        if data.startswith("adm_tutorial_delete:"):
            button_id = int(data.rsplit(":", 1)[1])
            await query.edit_message_text(
                render_bot_text('bot.services.v18_2_features.py:658:message','⚠️ هل تريد حذف الزر التعليمي نهائيًا؟', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:660:button','نعم، احذف', []), f"adm_tutorial_delete_confirm:{button_id}", "danger")], [btn(render_bot_button('bot.services.v18_2_features.py:660:button','إلغاء', []), f"adm_tutorial_view:{button_id}")]]),
            )
            return
        for suffix, state_name, prompt in (
            ("name", "await_tutorial_edit_name", "أرسل الاسم الجديد."),
            ("url", "await_tutorial_edit_url", "أرسل رابط HTTPS الجديد."),
            ("icon", "await_tutorial_edit_icon", "أرسل إحدى الأيقونات: 📹 🎬 📖 ℹ️"),
            ("custom_emoji", "await_tutorial_edit_custom_emoji", "أرسل الرمز المتحرك نفسه، أو معرّف Custom Emoji، أو - لمسحه."),
        ):
            prefix = f"adm_tutorial_{suffix}:"
            if data.startswith(prefix):
                button_id = int(data.split(":", 1)[1])
                set_state(context, state_name, button_id=button_id)
                await query.edit_message_text(render_bot_text('bot.services.v18_2_features.py:673:message','{v0}', [str(prompt)]), reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:673:button','إلغاء', []), f"adm_tutorial_view:{button_id}")]]))
                return

    await _v18_2_previous_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state, payload = current_state(context)
    managed_states = {
        "await_v18_2_customer_rate",
        "await_v18_2_network_minimum",
        "await_tutorial_add_name",
        "await_tutorial_add_url",
        "await_tutorial_add_icon",
        "await_tutorial_add_custom_emoji",
        "await_tutorial_edit_name",
        "await_tutorial_edit_url",
        "await_tutorial_edit_icon",
        "await_tutorial_edit_custom_emoji",
    }
    if state not in managed_states:
        await _v18_2_previous_on_message(update, context)
        return
    user = update.effective_user
    if not user or not DBX.is_admin(int(user.id)):
        set_state(context, None)
        return
    text_value = str(update.effective_message.text or "").strip()
    try:
        if state == "await_v18_2_customer_rate":
            if not DBX.has_permission(int(user.id), "customers_edit"):
                raise ValueError("لا تملك صلاحية تعديل العملاء")
            customer_id = int(payload.get("customer_id") or 0)
            updated = set_customer_custom_rate(customer_id, text_value.translate(ARABIC_DIGITS))
            DBX.audit(user.id, actor_name(user), "SET_CUSTOMER_RATE", "customer", customer_id, f"rate={updated['custom_exchange_rate_yer']}")
            set_state(context, None)
            await update.effective_message.reply_text(
                render_bot_text('bot.services.v18_2_features.py:709:message','{v0}', [str(customer_detail_text(updated))]), parse_mode=ParseMode.HTML,
                reply_markup=customer_actions_kb(updated, int(user.id)),
            )
            return
        if state == "await_v18_2_network_minimum":
            if not DBX.has_permission(int(user.id), "wallet_settings"):
                raise ValueError("لا تملك صلاحية إعدادات المحفظة")
            network_id = int(payload.get("network_id") or 0)
            updated = set_managed_network_minimum_floor(network_id, text_value.translate(ARABIC_DIGITS))
            DBX.audit(user.id, actor_name(user), "SET_WITHDRAW_MINIMUM_FLOOR", "managed_network", network_id, f"floor={updated['minimum_floor'] or 0}")
            set_state(context, None)
            await update.effective_message.reply_text(
                render_bot_text('bot.services.v18_2_features.py:721:message','✅ تم حفظ الحد الأدنى الإداري. سيطبق الأعلى بينه وبين حد Binance الحي.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.v18_2_features.py:723:button','↩️ الشبكة', []), f"adm_v18_network:{network_id}")]]),
            )
            return
        if not DBX.has_permission(int(user.id), "wallet_settings"):
            raise ValueError("لا تملك صلاحية إدارة التعليمات")
        if state == "await_tutorial_add_name":
            if len(text_value) < 2 or len(text_value) > 100:
                raise ValueError("اسم الزر غير صالح")
            context.user_data["tutorial_draft"] = {"name": text_value}
            set_state(context, "await_tutorial_add_url")
            await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:733:message','2️⃣ أرسل رابط الفيديو أو الشرح بصيغة HTTPS.', []))
            return
        if state == "await_tutorial_add_url":
            clean_url = _validate_tutorial_url(text_value)
            draft = context.user_data.get("tutorial_draft") or {}
            draft["url"] = clean_url
            context.user_data["tutorial_draft"] = draft
            set_state(context, "await_tutorial_add_icon")
            await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:741:message','3️⃣ اختر الأيقونة بإرسال واحدة: 📹 🎬 📖 ℹ️', []))
            return
        if state == "await_tutorial_add_icon":
            draft = context.user_data.get("tutorial_draft") or {}
            if text_value not in _TUTORIAL_ICONS:
                raise ValueError("أرسل إحدى الأيقونات: 📹 🎬 📖 ℹ️")
            draft["icon"] = text_value
            context.user_data["tutorial_draft"] = draft
            set_state(context, "await_tutorial_add_custom_emoji")
            await update.effective_message.reply_text(
                render_bot_text('bot.services.v18_2_features.py:750:message','4️⃣ أرسل الرمز المتحرك نفسه أو معرّف Custom Emoji، أو أرسل - لتخطيه.\nيتطلب ظهوره أن يكون مالك البوت مشتركًا في Telegram Premium.', [])
            )
            return
        if state == "await_tutorial_add_custom_emoji":
            draft = context.user_data.get("tutorial_draft") or {}
            custom_emoji_value = _custom_emoji_input(update.effective_message, text_value)
            row = create_tutorial_button(
                draft.get("name", ""), draft.get("url", ""), draft.get("icon", "📹"),
                admin_id=int(user.id), custom_emoji_id=custom_emoji_value,
            )
            context.user_data.pop("tutorial_draft", None)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "CREATE_TUTORIAL_BUTTON", "tutorial_button", row["id"], row["name"])
            await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:765:message','{v0}', [str(_tutorial_admin_text())]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_admin_kb())
            return
        button_id = int(payload.get("button_id") or 0)
        field = {
            "await_tutorial_edit_name": "name",
            "await_tutorial_edit_url": "url",
            "await_tutorial_edit_icon": "icon",
            "await_tutorial_edit_custom_emoji": "custom_emoji_id",
        }[state]
        field_value = (
            _custom_emoji_input(update.effective_message, text_value)
            if field == "custom_emoji_id"
            else text_value
        )
        updated = update_tutorial_button(button_id, **{field: field_value})
        set_state(context, None)
        DBX.audit(user.id, actor_name(user), "UPDATE_TUTORIAL_BUTTON", "tutorial_button", button_id, field)
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:782:message','{v0}', [str(_tutorial_detail_text(updated))]), parse_mode=ParseMode.HTML, reply_markup=_tutorial_detail_kb(updated))
    except (KeyError, TypeError, ValueError) as exc:
        await update.effective_message.reply_text(render_bot_text('bot.services.v18_2_features.py:784:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)


_export_globals(globals())

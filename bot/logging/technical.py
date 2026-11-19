"""Technical-channel UI and safe error reporting.

"""
from __future__ import annotations

import logging
import traceback

from bot.config import redact_sensitive_text
from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- قناة الأخطاء والردود التقنية ----------------


def technical_channel_kb() -> InlineKeyboardMarkup:
    rows = [
        [btn(render_bot_button('bot.logging.technical.py:18:button','🚨 تعيين/تعديل قناة الردود', []), "adm_technical_channel_set", "primary")],
        [btn(render_bot_button('bot.logging.technical.py:19:button','🧪 اختبار القناة', []), "adm_technical_channel_test")],
    ]
    if DBX.get_setting("technical_channel_id"):
        rows.append([btn(render_bot_button('bot.logging.technical.py:22:button','🗑️ حذف قناة الردود', []), "adm_technical_channel_clear", "danger")])
    rows.append([btn(render_bot_button('bot.logging.technical.py:23:button','↩️ إعدادات القنوات', []), "adm_channels_settings")])
    return InlineKeyboardMarkup(rows)


def channels_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.logging.technical.py:30:button','🔔 قناة الإشعارات', []), "adm_notification_channel")],
            [btn(render_bot_button('bot.logging.technical.py:31:button','🚨 قناة الأخطاء والردود', []), "adm_technical_channel", "primary")],
            [btn(render_bot_button('bot.logging.technical.py:32:button','🏠 لوحة الأدمن', []), "admin_panel")],
        ]
    )


async def send_technical_log(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reference_id: str,
    event_type: str,
    details: str,
    severity: str = "ERROR",
    telegram_user_id: Optional[int] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    # Technical events are persisted in SQLite and may also be sent to Telegram,
    # so apply the same secret redaction used by the rotating file log.
    safe_details = redact_sensitive_text(details or "")
    level_value = getattr(logging, str(severity or "ERROR").upper(), logging.ERROR)
    log.log(
        level_value,
        "Technical event reference=%s type=%s user=%s details=%s",
        reference_id,
        event_type,
        telegram_user_id,
        safe_details,
    )
    with DBX.connect() as con:
        con.execute(
            "INSERT INTO technical_events(reference_id,severity,event_type,telegram_user_id,details,created_at) VALUES (?,?,?,?,?,?)",
            (reference_id, severity.upper(), event_type, telegram_user_id, safe_details[:12000], now_str()),
        )
        con.commit()
    icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🚨", "CRITICAL": "🔥"}.get(severity.upper(), "🚨")
    text = (
        f"<b>{icon} {html(severity.upper())} — {html(event_type)}</b>\n\n"
        f"المرجع: <code>{html(reference_id)}</code>\n"
        f"المستخدم: <code>{html(telegram_user_id or '—')}</code>\n"
        f"الوقت: <code>{html(now_str())}</code>\n\n"
        f"<pre>{html(safe_details[:3000])}</pre>"
    )
    channel_id = (
        unified_notification_channel_id()
        if callable(globals().get("unified_notification_channel_id"))
        else DBX.get_setting("technical_channel_id")
    )
    enabled = (DBX.get_setting("technical_channel_enabled") or "1") == "1"
    if channel_id and enabled:
        try:
            await context.bot.send_message(
                chat_id=int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id,
                text=render_bot_text('bot.logging.technical.py:81:message','{v0}', [str(text)]),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            log.exception("failed to send technical log to channel")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=render_bot_text('bot.logging.technical.py:91:message','{v0}', [str(text)]),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except Exception:
        log.exception("failed to send technical log to main admin")


async def send_safe_wallet_error(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reference_id: str,
    event_type: str,
    exc: Exception,
    telegram_user_id: Optional[int] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    severity: str = "ERROR",
) -> None:
    await send_or_edit(
        target,
        render_bot_text('bot.logging.technical.py:112:message','{v0}', [str(wallet_user_status_message(reference_id))]),
        reply_markup=reply_markup or InlineKeyboardMarkup([[btn(render_bot_button('bot.logging.technical.py:115:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
    )
    await send_technical_log(
        context,
        reference_id=reference_id,
        event_type=event_type,
        details="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        severity=severity,
        telegram_user_id=telegram_user_id,
    )




_export_globals(globals())

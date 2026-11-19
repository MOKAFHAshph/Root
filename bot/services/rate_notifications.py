"""Automatic exchange-rate announcement after an admin changes the rate."""
from __future__ import annotations

import asyncio
from decimal import InvalidOperation

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

_rate_legacy_on_callback = on_callback
_rate_legacy_on_message = on_message


def rate_notification_text(rate: Decimal) -> str:
    return (
        render_bot_text('fragment.bot.services.rate_notifications.py:rate_notification_text:22','📢 <b>سعر الصرف المعتمد لحسابك</b>\n\nالسعر الفعلي للعمليات الجديدة:\n\n<b>1 USDT = {v0} ريال يمني</b>',[f'{html(format_display_amount(rate))}'])
    )


async def broadcast_current_rate(query: Any, context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int, int]:
    rate = DBX.get_rate()
    targets = list_broadcast_targets()
    total = len(targets)
    sent = 0
    failed = 0
    progress = await query.edit_message_text(
        render_bot_text('bot.services.rate_notifications.py:33:message','⏳ جاري إرسال تحديث السعر إلى <b>{v0}</b> مستخدم...', [f'{total}']),
        parse_mode=ParseMode.HTML,
    )
    for index, target_id in enumerate(targets, start=1):
        # العملاء ذوو السعر الخاص يستلمون سعرهم الفعلي، لا السعر العام.
        target_rate = effective_customer_rate(int(target_id))
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text=render_bot_text('bot.services.rate_notifications.py:41:message','{v0}', [str(rate_notification_text(target_rate))]),
                parse_mode=ParseMode.HTML,
            )
            mark_bot_user_blocked(target_id, False)
            sent += 1
        except RetryAfter as exc:
            retry_after = getattr(exc, "retry_after", 1)
            seconds = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
            await asyncio.sleep(max(1.0, seconds))
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=render_bot_text('bot.services.rate_notifications.py:53:message','{v0}', [str(rate_notification_text(target_rate))]),
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except Exception:
                failed += 1
                log.exception("Rate notification copy failed user=%s", target_id)
        except Forbidden:
            mark_bot_user_blocked(target_id, True)
            failed += 1
            log.info("Rate notification blocked by user=%s", target_id)
        except TelegramError:
            failed += 1
            log.warning("Rate notification failed user=%s", target_id, exc_info=True)
        if index % 25 == 0 or index == total:
            try:
                await progress.edit_text(
                    f"⏳ تحديث سعر الصرف\n\nتمت معالجة {index}/{total}\n✅ نجح: {sent}\n❌ فشل: {failed}"
                )
            except Exception:
                log.debug("Could not update rate broadcast progress", exc_info=True)
        if BROADCAST_DELAY_SECONDS > 0:
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    DBX.audit(
        query.from_user.id,
        actor_name(query.from_user),
        "BROADCAST_RATE",
        "settings",
        "usdt_rate_yer",
        f"rate={q2(rate)} total={total} sent={sent} failed={failed}",
    )
    await progress.edit_text(
        "✅ <b>اكتمل إشعار المستخدمين</b>\n\n"
        f"💲 السعر: <b>{html(format_display_amount(rate))} ريال</b>\n"
        f"👥 الإجمالي: <b>{total}</b>\n"
        f"✅ تم الإرسال: <b>{sent}</b>\n"
        f"❌ تعذر الإرسال: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb(query.from_user.id),
    )
    return total, sent, failed


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    if data != "rate_notify_all":
        await _rate_legacy_on_callback(update, context)
        return
    if not DBX.is_admin(query.from_user.id) or not DBX.has_permission(query.from_user.id, "settings_rate"):
        await query.answer("لا تملك صلاحية تعديل سعر الصرف", show_alert=True)
        return
    await query.answer("بدأ إرسال الإشعار")
    await broadcast_current_rate(query, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state, _ = current_state(context)
    if state != "await_rate":
        await _rate_legacy_on_message(update, context)
        return
    user = update.effective_user
    if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "settings_rate"):
        set_state(context, None)
        await update.effective_message.reply_text(render_bot_text('bot.services.rate_notifications.py:120:message','⛔ لا تملك صلاحية تعديل سعر الصرف.', []))
        return
    text_value = (update.effective_message.text or "").strip()
    try:
        rate = parse_decimal(text_value)
        if rate <= 0:
            raise InvalidOperation
    except Exception:
        await update.effective_message.reply_text(render_bot_text('bot.services.rate_notifications.py:128:message','❌ السعر غير صالح. أرسل رقمًا أكبر من صفر.', []))
        return
    DBX.set_setting("usdt_rate_yer", q2(rate))
    DBX.audit(user.id, actor_name(user), "SET_RATE", "settings", "usdt_rate_yer", q2(rate))
    set_state(context, None)
    await update.effective_message.reply_text(
        render_bot_text('bot.services.rate_notifications.py:133:message','✅ <b>تم تحديث سعر الصرف بنجاح</b>\n\nالسعر الجديد: <b>1 USDT = {v0} ريال يمني</b>\n\nيمكنك إشعار جميع المستخدمين مباشرة دون كتابة إذاعة يدوية.', [f'{html(format_display_amount(rate))}']),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn(render_bot_button('bot.services.rate_notifications.py:140:button','📢 إشعار جميع المستخدمين', []), "rate_notify_all", "primary")],
                [btn(render_bot_button('bot.services.rate_notifications.py:141:button','🏠 لوحة الأدمن', []), "admin_panel")],
            ]
        ),
    )


_export_globals(globals())

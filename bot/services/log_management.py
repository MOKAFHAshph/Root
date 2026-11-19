"""Runtime log management and admin controls.

This layer keeps the existing technical-event channel while adding management of
on-disk application logs: rotating files, level/debug controls, channel delivery,
and safe truncation without deleting the active file.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import export_globals as _export_globals
from bot.runtime import seed_globals as _seed_globals

_seed_globals(globals())

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_SIZE_PRESETS = (
    (5 * 1024 * 1024, "5 MB"),
    (10 * 1024 * 1024, "10 MB"),
    (25 * 1024 * 1024, "25 MB"),
    (50 * 1024 * 1024, "50 MB"),
)
LOG_COPY_PRESETS = (3, 5, 10, 20)

ADMIN_PERMISSION_LABELS.update({"logs_manage": "🧾 إدارة سجل الأخطاء"})
ALL_ADMIN_PERMISSIONS = tuple(ADMIN_PERMISSION_LABELS)


def _log_defaults() -> None:
    defaults = {
        "log_level": LOG_LEVEL_DEFAULT,
        "debug_mode": "1" if DEBUG_MODE_DEFAULT else "0",
        "log_max_bytes": str(LOG_MAX_BYTES_DEFAULT),
        "log_backup_count": str(LOG_BACKUP_COUNT_DEFAULT),
        "log_channel_id": "",
        "log_channel_title": "",
        "log_channel_username": "",
        "log_channel_enabled": "1",
        "log_level_before_debug": "INFO",
    }
    for key, value in defaults.items():
        if DBX.get_setting(key) is None:
            DBX.set_setting(key, value)


_log_defaults()


def current_log_level() -> str:
    value = str(DBX.get_setting("log_level") or LOG_LEVEL_DEFAULT).upper()
    return value if value in LOG_LEVELS else "INFO"


def debug_mode_enabled() -> bool:
    return str(DBX.get_setting("debug_mode") or "0") == "1"


def current_log_max_bytes() -> int:
    try:
        value = int(DBX.get_setting("log_max_bytes") or LOG_MAX_BYTES_DEFAULT)
    except (TypeError, ValueError):
        value = LOG_MAX_BYTES_DEFAULT
    return max(1_048_576, min(value, 500 * 1024 * 1024))


def current_log_backup_count() -> int:
    try:
        value = int(DBX.get_setting("log_backup_count") or LOG_BACKUP_COUNT_DEFAULT)
    except (TypeError, ValueError):
        value = LOG_BACKUP_COUNT_DEFAULT
    return max(1, min(value, 50))


def configured_log_channel_id() -> Optional[int | str]:
    if str(DBX.get_setting("log_channel_enabled") or "0") != "1":
        return None
    unified = unified_notification_channel_id() if callable(globals().get("unified_notification_channel_id")) else None
    value = str(unified or DBX.get_setting("log_channel_id") or "").strip()
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def apply_persisted_logging_settings() -> None:
    configure_logging(
        level=current_log_level(),
        debug=debug_mode_enabled(),
        max_bytes=current_log_max_bytes(),
        backup_count=current_log_backup_count(),
    )


apply_persisted_logging_settings()


def _human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _log_file_size() -> int:
    try:
        return LOG_PATH.stat().st_size
    except OSError:
        return 0


def _log_channel_display() -> str:
    title = str(DBX.get_setting("log_channel_title") or "").strip()
    username = str(DBX.get_setting("log_channel_username") or "").strip().lstrip("@")
    if not title:
        return "غير محددة"
    return f"{title} (@{username})" if username else title


def log_settings_text() -> str:
    return (
        render_bot_text('fragment.bot.services.log_management.py:log_settings_text:131','<b>🧾 إدارة سجل الأخطاء</b>\n\n📄 الملف: <code>{v0}</code>\n📦 الحجم الحالي: <b>{v1}</b>\n📊 مستوى التسجيل: <b>{v2}</b>\n🐞 وضع Debug: <b>{v3}</b>\n🔄 حد التدوير: <b>{v4}</b>\n🗂️ عدد الملفات القديمة: <b>{v5}</b>\n📺 قناة/مجموعة السجلات: <b>{v6}</b>\n\nيتم تدوير ملف السجل تلقائيًا عند بلوغ الحد، مع الاحتفاظ بعدد النسخ المحدد.',[f'{html(LOG_PATH.name)}',f'{html(_human_size(_log_file_size()))}',f'{html(current_log_level())}',f"{('مفعّل' if debug_mode_enabled() else 'متوقف')}",f'{html(_human_size(current_log_max_bytes()))}',f'{current_log_backup_count()}',f'{html(_log_channel_display())}'])
    )


def log_settings_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [btn(render_bot_button('bot.services.log_management.py:145:button','📄 تحميل سجل الأخطاء', []), "adm_logs_download", "primary")],
    ]
    if configured_log_channel_id():
        rows.append([btn(render_bot_button('bot.services.log_management.py:148:button','📤 إرسال السجل للقناة', []), "adm_logs_send_channel")])
    rows.extend(
        [
            [btn(render_bot_button('bot.services.log_management.py:151:button','🗑️ تصفير سجل الأخطاء', []), "adm_logs_clear", "danger")],
            [btn(render_bot_button('bot.services.log_management.py:152:button','📊 مستوى التسجيل', []), "adm_logs_levels"), btn(render_bot_button('bot.services.log_management.py:152:button','🐞 تبديل Debug', []), "adm_logs_toggle_debug")],
            [btn(render_bot_button('bot.services.log_management.py:153:button','🔄 إعدادات Log Rotation', []), "adm_logs_rotation")],
            [btn(render_bot_button('bot.services.log_management.py:154:button','📺 تعيين قناة/مجموعة السجلات', []), "adm_logs_channel_set")],
        ]
    )
    if configured_log_channel_id():
        rows.append(
            [
                btn(render_bot_button('bot.services.log_management.py:160:button','🧪 اختبار القناة', []), "adm_logs_channel_test"),
                btn(render_bot_button('bot.services.log_management.py:161:button','🗑️ حذف القناة', []), "adm_logs_channel_clear", "danger"),
            ]
        )
    rows.append([btn(render_bot_button('bot.services.log_management.py:164:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def log_levels_kb() -> InlineKeyboardMarkup:
    rows = []
    current = current_log_level()
    for level in LOG_LEVELS:
        marker = "✅ " if level == current else ""
        rows.append([btn(render_bot_button('bot.services.log_management.py:173:button','{v0}{v1}', [f'{marker}',f'{level}']), f"adm_logs_level:{level}")])
    rows.append([btn(render_bot_button('bot.services.log_management.py:174:button','↩️ رجوع', []), "adm_logs")])
    return InlineKeyboardMarkup(rows)


def log_rotation_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([btn(render_bot_button('bot.services.log_management.py:180:button','— الحد الأقصى لحجم الملف —', []), "adm_logs_rotation")])
    rows.append([btn(render_bot_button('bot.services.log_management.py:181:button','{v0}', [str(label)]), f"adm_logs_size:{size}") for size, label in LOG_SIZE_PRESETS])
    rows.append([btn(render_bot_button('bot.services.log_management.py:182:button','— عدد النسخ القديمة —', []), "adm_logs_rotation")])
    rows.append([btn(render_bot_button('bot.services.log_management.py:183:button','{v0}', [str(str(count))]), f"adm_logs_copies:{count}") for count in LOG_COPY_PRESETS])
    rows.append([btn(render_bot_button('bot.services.log_management.py:184:button','↩️ رجوع', []), "adm_logs")])
    return InlineKeyboardMarkup(rows)


def _flush_and_snapshot_log() -> Optional[Path]:
    flush_log_handlers()
    if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
        return None
    snapshot_dir = EXPORT_DIR / "log_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / f"{LOG_PATH.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{LOG_PATH.suffix or '.log'}"
    shutil.copy2(LOG_PATH, snapshot)
    return snapshot


async def _send_log_snapshot(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    *,
    caption: str,
) -> bool:
    snapshot = await asyncio.to_thread(_flush_and_snapshot_log)
    if snapshot is None:
        return False
    try:
        with snapshot.open("rb") as file_obj:
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_obj,
                filename=snapshot.name,
                caption=render_bot_text('bot.services.log_management.py:210:message','{v0}', [str(caption)]),
            )
        return True
    finally:
        try:
            snapshot.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete temporary log snapshot path=%s", snapshot)


def _truncate_active_log() -> None:
    """Empty the active file without deleting it or breaking Windows handlers."""
    flush_log_handlers()
    handled = False
    target = str(LOG_PATH.resolve())
    for handler in logging.getLogger().handlers:
        base_filename = getattr(handler, "baseFilename", None)
        if not base_filename or str(Path(base_filename).resolve()) != target:
            continue
        handler.acquire()
        try:
            stream = getattr(handler, "stream", None)
            if stream is not None:
                stream.flush()
                stream.seek(0)
                stream.truncate(0)
                stream.flush()
                handled = True
        finally:
            handler.release()
    if not handled:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("w", encoding="utf-8"):
            pass


def _chat_type_value(chat: Any) -> str:
    value = getattr(chat, "type", "")
    return str(getattr(value, "value", value)).lower()


def _status_value(member: Any) -> str:
    value = getattr(member, "status", "")
    return str(getattr(value, "value", value)).lower()


async def _validate_log_destination(bot: Any, identifier: Any) -> dict[str, Any]:
    chat = await bot.get_chat(identifier)
    member = await bot.get_chat_member(chat.id, bot.id)
    status = _status_value(member)
    chat_type = _chat_type_value(chat)

    admin_statuses = {"administrator", "creator", "owner"}
    if chat_type == "channel" and status not in admin_statuses:
        raise ValueError("يجب أن يكون البوت أدمنًا داخل قناة السجلات.")
    if chat_type in {"group", "supergroup"} and status in {"left", "kicked", "banned"}:
        raise ValueError("يجب إضافة البوت إلى مجموعة السجلات.")

    await bot.send_message(chat.id, render_bot_text('bot.services.log_management.py:272:message','✅ تم ربط هذه المحادثة بسجل أخطاء البوت.', []))
    return {
        "id": int(chat.id),
        "title": getattr(chat, "title", None) or str(chat.id),
        "username": getattr(chat, "username", None) or "",
        "type": chat_type,
    }


def _message_destination_identifier(message: Any) -> Any:
    # PTB 22+ stores forwarded chat metadata under forward_origin.
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    if chat is not None:
        return getattr(chat, "id", None)
    forward_from_chat = getattr(message, "forward_from_chat", None)
    if forward_from_chat is not None:
        return getattr(forward_from_chat, "id", None)
    text = (getattr(message, "text", None) or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    if text.startswith("@") and len(text) >= 6:
        return text
    return None


_previous_admin_kb_logs = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _previous_admin_kb_logs(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    back: Optional[list[InlineKeyboardButton]] = None
    if rows and any(button.callback_data == "main_menu" for button in rows[-1]):
        back = rows.pop()
    if DBX.has_permission(admin_id, "logs_manage"):
        rows.append([btn(render_bot_button('bot.services.log_management.py:308:button','🧾 إدارة سجل الأخطاء', []), "adm_logs", "primary")])
    rows.append(back or [btn(render_bot_button('bot.services.log_management.py:309:button','🏠 رجوع', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


_previous_callback_permission_logs = callback_permission


def callback_permission(data: str) -> Optional[str]:
    if data == "adm_logs" or data.startswith("adm_logs_"):
        return "logs_manage"
    return _previous_callback_permission_logs(data)


_previous_state_permission_logs = state_permission


def state_permission(state: Optional[str]) -> Optional[str]:
    if state == "await_log_channel":
        return "logs_manage"
    return _previous_state_permission_logs(state)


_previous_on_callback_logs = on_callback
_previous_on_message_logs = on_message


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    if not (data == "adm_logs" or data.startswith("adm_logs_")):
        await _previous_on_callback_logs(update, context)
        return

    if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "logs_manage"):
        await query.answer("لا تملك صلاحية إدارة السجلات", show_alert=True)
        return

    if data == "adm_logs":
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:350:message','{v0}', [str(log_settings_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=log_settings_kb(),
        )
        return

    if data == "adm_logs_download":
        await query.answer("جاري تجهيز السجل...")
        sent = await _send_log_snapshot(
            context,
            int(user.id),
            caption=f"سجل أخطاء {APP_NAME} — {now_str()}",
        )
        if not sent:
            await context.bot.send_message(user.id, render_bot_text('bot.services.log_management.py:365:message','ℹ️ ملف السجل فارغ حاليًا.', []))
        else:
            DBX.audit(user.id, actor_name(user), "DOWNLOAD_LOG_FILE", "logs", LOG_PATH.name)
        return

    if data == "adm_logs_send_channel":
        destination = configured_log_channel_id()
        if destination is None:
            await query.answer("حدد قناة أو مجموعة السجلات أولًا", show_alert=True)
            return
        await query.answer("جاري إرسال السجل...")
        sent = await _send_log_snapshot(
            context,
            destination,
            caption=f"سجل أخطاء {APP_NAME} — {now_str()}",
        )
        if not sent:
            await context.bot.send_message(user.id, render_bot_text('bot.services.log_management.py:382:message','ℹ️ ملف السجل فارغ حاليًا.', []))
        else:
            DBX.audit(user.id, actor_name(user), "SEND_LOG_TO_CHANNEL", "logs", destination)
            await context.bot.send_message(user.id, render_bot_text('bot.services.log_management.py:385:message','✅ تم إرسال السجل إلى القناة/المجموعة المحددة.', []))
        return

    if data == "adm_logs_clear":
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:390:message','⚠️ <b>تأكيد تصفير سجل الأخطاء</b>\n\nسيتم تفريغ محتوى الملف الحالي دون حذف الملف. يفضّل تحميل نسخة أولًا.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(render_bot_button('bot.services.log_management.py:396:button','✅ نعم، صفّر السجل', []), "adm_logs_clear_confirm", "danger")],
                    [btn(render_bot_button('bot.services.log_management.py:397:button','↩️ إلغاء', []), "adm_logs")],
                ]
            ),
        )
        return

    if data == "adm_logs_clear_confirm":
        await query.answer("تم التصفير")
        # Record the action in the database before emptying the file, then avoid
        # writing another file-log entry immediately after truncation.
        DBX.audit(user.id, actor_name(user), "TRUNCATE_LOG_FILE", "logs", LOG_PATH.name)
        await asyncio.to_thread(_truncate_active_log)
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:409:message','✅ تم تفريغ محتوى ملف السجل مع الإبقاء على الملف جاهزًا للأحداث الجديدة.', []),
            reply_markup=log_settings_kb(),
        )
        return

    if data == "adm_logs_levels":
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:417:message','اختر مستوى التسجيل. DEBUG يسجل تفاصيل أكثر وقد يزيد حجم الملف.', []),
            reply_markup=log_levels_kb(),
        )
        return

    if data.startswith("adm_logs_level:"):
        level = data.split(":", 1)[1].upper()
        if level not in LOG_LEVELS:
            await query.answer("مستوى غير صالح", show_alert=True)
            return
        DBX.set_setting("log_level", level)
        DBX.set_setting("debug_mode", "1" if level == "DEBUG" else "0")
        if level != "DEBUG":
            DBX.set_setting("log_level_before_debug", level)
        apply_persisted_logging_settings()
        DBX.audit(user.id, actor_name(user), "SET_LOG_LEVEL", "settings", level)
        log.info("Log level changed by admin=%s level=%s", user.id, level)
        await query.answer("تم تحديث المستوى")
        await query.edit_message_text(render_bot_text('bot.services.log_management.py:436:message','{v0}', [str(log_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=log_settings_kb())
        return

    if data == "adm_logs_toggle_debug":
        enabled = not debug_mode_enabled()
        if enabled:
            previous = current_log_level()
            if previous != "DEBUG":
                DBX.set_setting("log_level_before_debug", previous)
            DBX.set_setting("debug_mode", "1")
            DBX.set_setting("log_level", "DEBUG")
        else:
            previous = str(DBX.get_setting("log_level_before_debug") or "INFO").upper()
            if previous not in LOG_LEVELS or previous == "DEBUG":
                previous = "INFO"
            DBX.set_setting("debug_mode", "0")
            DBX.set_setting("log_level", previous)
        apply_persisted_logging_settings()
        DBX.audit(user.id, actor_name(user), "TOGGLE_DEBUG", "settings", "debug_mode", str(enabled))
        log.warning("Debug mode changed by admin=%s enabled=%s", user.id, enabled)
        await query.answer("تم تحديث وضع Debug")
        await query.edit_message_text(render_bot_text('bot.services.log_management.py:457:message','{v0}', [str(log_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=log_settings_kb())
        return

    if data == "adm_logs_rotation":
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:462:message','<b>🔄 إعدادات Log Rotation</b>\n\nاختر حجم الملف وعدد النسخ القديمة.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=log_rotation_kb(),
        )
        return

    if data.startswith("adm_logs_size:"):
        value = int(data.split(":", 1)[1])
        allowed = {size for size, _ in LOG_SIZE_PRESETS}
        if value not in allowed:
            await query.answer("قيمة غير صالحة", show_alert=True)
            return
        DBX.set_setting("log_max_bytes", str(value))
        apply_persisted_logging_settings()
        DBX.audit(user.id, actor_name(user), "SET_LOG_MAX_BYTES", "settings", value)
        log.info("Log rotation size changed admin=%s bytes=%s", user.id, value)
        await query.answer("تم تحديث حجم التدوير")
        await query.edit_message_text(render_bot_text('bot.services.log_management.py:480:message','{v0}', [str(log_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=log_settings_kb())
        return

    if data.startswith("adm_logs_copies:"):
        value = int(data.split(":", 1)[1])
        if value not in LOG_COPY_PRESETS:
            await query.answer("قيمة غير صالحة", show_alert=True)
            return
        DBX.set_setting("log_backup_count", str(value))
        apply_persisted_logging_settings()
        DBX.audit(user.id, actor_name(user), "SET_LOG_BACKUP_COUNT", "settings", value)
        log.info("Log rotation copies changed admin=%s count=%s", user.id, value)
        await query.answer("تم تحديث عدد النسخ")
        await query.edit_message_text(render_bot_text('bot.services.log_management.py:493:message','{v0}', [str(log_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=log_settings_kb())
        return

    if data == "adm_logs_channel_set":
        set_state(context, "await_log_channel")
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.log_management.py:499:message','أرسل معرف القناة/المجموعة مثل <code>-100...</code> أو <code>@username</code>، أو أعد توجيه رسالة منها.\n\nفي القنوات يجب أن يكون البوت أدمنًا.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.log_management.py:504:button','↩️ إلغاء', []), "adm_logs")]]),
        )
        return

    if data == "adm_logs_channel_clear":
        if callable(globals().get("set_admin_channel")):
            set_admin_channel("")
        else:
            for key in ("log_channel_id", "log_channel_title"):
                DBX.set_setting(key, "")
            DBX.set_setting("log_channel_enabled", "0")
        DBX.set_setting("log_channel_username", "")
        DBX.audit(user.id, actor_name(user), "CLEAR_LOG_CHANNEL", "settings", "log_channel_id")
        log.info("Log destination cleared admin=%s", user.id)
        await query.answer("تم حذف القناة")
        await query.edit_message_text(render_bot_text('bot.services.log_management.py:519:message','{v0}', [str(log_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=log_settings_kb())
        return

    if data == "adm_logs_channel_test":
        destination = configured_log_channel_id()
        if destination is None:
            await query.answer("لم يتم تحديد قناة", show_alert=True)
            return
        try:
            await context.bot.send_message(destination, render_bot_text('bot.services.log_management.py:528:message','🧪 اختبار قناة السجلات — {v0}\n{v1}', [f'{APP_NAME}',f'{now_str()}']))
            await query.answer("نجح الاختبار", show_alert=True)
        except TelegramError as exc:
            log.exception("Log channel test failed destination=%s", destination)
            await query.answer(f"فشل الاختبار: {exc}", show_alert=True)
        return

    await _previous_on_callback_logs(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state, _ = current_state(context)
    if state != "await_log_channel":
        await _previous_on_message_logs(update, context)
        return

    user = update.effective_user
    message = update.effective_message
    if not user or not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "logs_manage"):
        set_state(context, None)
        if message:
            await message.reply_text(render_bot_text('bot.services.log_management.py:549:message','⛔ لا تملك صلاحية إدارة السجلات.', []))
        return

    identifier = _message_destination_identifier(message)
    if identifier is None:
        await message.reply_text(render_bot_text('bot.services.log_management.py:554:message','❌ أرسل Channel ID أو @username صالحًا، أو أعد توجيه رسالة من القناة/المجموعة.', []))
        return

    try:
        destination = await _validate_log_destination(context.bot, identifier)
    except Exception as exc:
        log.exception("Failed to validate log destination identifier=%s", identifier)
        await message.reply_text(render_bot_text('bot.services.log_management.py:561:message','❌ تعذر ربط القناة/المجموعة: <code>{v0}</code>', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
        return

    if callable(globals().get("set_admin_channel")):
        set_admin_channel(str(destination["id"]), destination["title"])
    else:
        DBX.set_setting("log_channel_id", str(destination["id"]))
        DBX.set_setting("log_channel_title", destination["title"])
    DBX.set_setting("log_channel_username", destination["username"])
    DBX.set_setting("log_channel_enabled", "1")
    DBX.audit(user.id, actor_name(user), "SET_LOG_CHANNEL", "settings", destination["id"], destination["title"])
    set_state(context, None)
    log.info(
        "Log destination configured admin=%s chat=%s type=%s",
        user.id,
        destination["id"],
        destination["type"],
    )
    await message.reply_text(
        render_bot_text('bot.services.log_management.py:579:message','✅ <b>تم حفظ قناة/مجموعة السجلات</b>\n\n📺 {v0}\n🆔 <code>{v1}</code>', [f"{html(destination['title'])}",f"{destination['id']}"]),
        parse_mode=ParseMode.HTML,
        reply_markup=log_settings_kb(),
    )


_previous_post_init_logs = post_init


async def post_init(application: Application):
    apply_persisted_logging_settings()
    await _previous_post_init_logs(application)
    log.info(
        "Bot started app=%s version=%s admin=%s db=%s log=%s level=%s debug=%s",
        APP_NAME,
        APP_VERSION,
        ADMIN_ID,
        DB_FILE,
        LOG_PATH,
        current_log_level(),
        debug_mode_enabled(),
    )


async def post_shutdown(application: Application):
    log.info("Bot stopped app=%s version=%s", APP_NAME, APP_VERSION)
    flush_log_handlers()


_export_globals(globals())

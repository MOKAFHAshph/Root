"""Scheduled Telegram-channel backups, retention, and restore management.

The established ``create_backup_file`` and ``DB.import_snapshot`` functions
remain authoritative. This module adds editable scheduling, a dedicated backup
channel, tracked retention, and channel/manual restore controls.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import export_globals as _export_globals
from bot.runtime import seed_globals as _seed_globals

_seed_globals(globals())

# ---------------------------------------------------------------------------
# Defaults and persistent settings
# ---------------------------------------------------------------------------

BACKUP_MODE_MANUAL = "MANUAL"
BACKUP_MODE_AUTO = "AUTO"
BACKUP_DEFAULT_INTERVAL_SECONDS = int(env("BACKUP_DEFAULT_INTERVAL_SECONDS", "86400"))
BACKUP_DEFAULT_MAX_COPIES = int(env("BACKUP_DEFAULT_MAX_COPIES", "100"))
BACKUP_SCHEDULER_TICK_SECONDS = max(30, int(env("BACKUP_SCHEDULER_TICK_SECONDS", "60")))
BACKUP_FAILURE_RETRY_SECONDS = max(60, int(env("BACKUP_FAILURE_RETRY_SECONDS", "600")))
BACKUP_MAX_UPLOAD_BYTES = max(1024, int(env("BACKUP_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))))
BACKUP_LIST_PAGE_SIZE = 7
BACKUP_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

BACKUP_INTERVALS: tuple[tuple[int, str], ...] = (
    (3600, "كل ساعة"),
    (6 * 3600, "كل 6 ساعات"),
    (12 * 3600, "كل 12 ساعة"),
    (24 * 3600, "كل 24 ساعة"),
    (48 * 3600, "كل يومين"),
    (7 * 24 * 3600, "كل أسبوع"),
)

_BACKUP_JOB_LOCK = asyncio.Lock()


def _backup_schema_and_defaults(database: Optional[DB] = None) -> None:
    """Create only the additive tracking table and missing setting defaults."""
    database = database or DBX
    with database.connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_channel_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                channel_title TEXT,
                channel_username TEXT,
                message_id INTEGER NOT NULL,
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT,
                filename TEXT NOT NULL,
                file_size INTEGER,
                source TEXT NOT NULL DEFAULT 'AUTO',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                delete_error TEXT,
                UNIQUE(channel_id, message_id)
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_backup_channel_files_active "
            "ON backup_channel_files(channel_id, status, id DESC)"
        )
        con.commit()

    defaults = {
        "backup_mode": BACKUP_MODE_MANUAL,
        "backup_interval_seconds": str(BACKUP_DEFAULT_INTERVAL_SECONDS),
        "backup_max_copies": str(BACKUP_DEFAULT_MAX_COPIES),
        "backup_channel_id": str(globals().get("CHANNEL_BACKUP", "") or ""),
        "backup_channel_title": "",
        "backup_channel_username": "",
        "backup_next_run_at": "",
        "backup_last_attempt_at": "",
        "backup_last_success_at": "",
        "backup_last_status": "NEVER",
        "backup_last_error": "",
    }
    for key, value in defaults.items():
        if database.get_setting(key) is None:
            database.set_setting(key, value)


_backup_schema_and_defaults()


def _setting(key: str, default: str = "") -> str:
    value = DBX.get_setting(key)
    return default if value is None else str(value)


def backup_mode() -> str:
    value = _setting("backup_mode", BACKUP_MODE_MANUAL).upper()
    return value if value in {BACKUP_MODE_MANUAL, BACKUP_MODE_AUTO} else BACKUP_MODE_MANUAL


def backup_interval_seconds() -> int:
    try:
        value = int(_setting("backup_interval_seconds", str(BACKUP_DEFAULT_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        value = BACKUP_DEFAULT_INTERVAL_SECONDS
    return max(300, value)


def backup_max_copies() -> int:
    try:
        value = int(_setting("backup_max_copies", str(BACKUP_DEFAULT_MAX_COPIES)))
    except (TypeError, ValueError):
        value = BACKUP_DEFAULT_MAX_COPIES
    return max(1, min(value, 5000))


def backup_channel_id() -> Optional[int]:
    value = _setting("backup_channel_id", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, BACKUP_DATETIME_FORMAT)
    except ValueError:
        return None


def _format_datetime(value: Optional[datetime]) -> str:
    return value.strftime(BACKUP_DATETIME_FORMAT) if value else ""


def _interval_label(seconds: Optional[int] = None) -> str:
    seconds = backup_interval_seconds() if seconds is None else int(seconds)
    for known, label in BACKUP_INTERVALS:
        if known == seconds:
            return label
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"كل {days} يوم" if days != 1 else "كل يوم"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"كل {hours} ساعة"
    return f"كل {seconds // 60} دقيقة"


def _reset_next_run() -> None:
    if backup_mode() != BACKUP_MODE_AUTO:
        DBX.set_setting("backup_next_run_at", "")
        return
    next_run = datetime.now() + timedelta(seconds=backup_interval_seconds())
    DBX.set_setting("backup_next_run_at", _format_datetime(next_run))


def _tracked_backup_count(channel_id: Optional[int] = None) -> int:
    channel_id = backup_channel_id() if channel_id is None else int(channel_id)
    if channel_id is None:
        return 0
    with DBX.connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM backup_channel_files "
            "WHERE channel_id = ? AND status = 'ACTIVE'",
            (channel_id,),
        ).fetchone()
    return int(row["c"] or 0)


def _backup_settings_text() -> str:
    mode = backup_mode()
    channel = backup_channel_id()
    channel_title = _setting("backup_channel_title", "") or "غير محددة"
    username = _setting("backup_channel_username", "")
    channel_display = f"{channel_title} (@{username})" if username else channel_title
    next_run = _setting("backup_next_run_at", "") or "—"
    last_success = _setting("backup_last_success_at", "") or "—"
    last_status = _setting("backup_last_status", "NEVER")
    last_error = _setting("backup_last_error", "")
    tracked = _tracked_backup_count(channel) if channel else 0
    status_labels = {
        "NEVER": "لم تُنفذ بعد",
        "SUCCESS": "✅ ناجحة",
        "FAILED": "❌ فاشلة",
        "RUNNING": "⏳ قيد التنفيذ",
    }
    lines = [
        "<b>🗄️ إعدادات النسخ الاحتياطي</b>",
        "",
        f"⚙️ الوضع: <b>{'تلقائي' if mode == BACKUP_MODE_AUTO else 'يدوي'}</b>",
        f"⏱️ الفترة: <b>{html(_interval_label())}</b>",
        f"📺 قناة النسخ: <b>{html(channel_display)}</b>",
        f"🆔 معرّف القناة: <code>{html(channel or '—')}</code>",
        f"📚 الحد الأقصى: <b>{backup_max_copies()}</b> نسخة",
        f"🗂️ النسخ المتتبعة حاليًا: <b>{tracked}</b>",
        f"🕘 التشغيل القادم: <code>{html(next_run if mode == BACKUP_MODE_AUTO else 'غير مجدول')}</code>",
        f"✅ آخر نجاح: <code>{html(last_success)}</code>",
        f"📌 آخر حالة: <b>{html(status_labels.get(last_status, last_status))}</b>",
    ]
    if last_error:
        lines.extend(["", f"⚠️ آخر خطأ: <code>{html(last_error[:500])}</code>"])
    lines.extend(
        [
            "",
            "يعتمد النظام على دالة النسخ الحالية، ويرسل ملفات JSON إلى قناة مستقلة. "
            "يجب أن يكون البوت أدمنًا بصلاحيتي نشر الرسائل وحذفها حتى يعمل الاحتفاظ التلقائي.",
        ]
    )
    return "\n".join(lines)


def _backup_settings_kb(admin_id: int) -> InlineKeyboardMarkup:
    mode = backup_mode()
    rows: list[list[InlineKeyboardButton]] = []
    if DBX.has_permission(admin_id, "backup_manage"):
        rows.append(
            [
                btn(render_bot_button('bot.services.backup_automation.py:237:button','{v0}', [str('✅ يدوي' if mode == BACKUP_MODE_MANUAL else '▫️ يدوي')]), "backup_mode:manual"),
                btn(render_bot_button('bot.services.backup_automation.py:238:button','{v0}', [str('✅ تلقائي' if mode == BACKUP_MODE_AUTO else '▫️ تلقائي')]), "backup_mode:auto", "success"),
            ]
        )
        if mode == BACKUP_MODE_AUTO:
            rows.append(
                [
                    btn(render_bot_button('bot.services.backup_automation.py:244:button','⏱️ الفترة الزمنية', []), "backup_interval_menu", "primary"),
                    btn(render_bot_button('bot.services.backup_automation.py:245:button','📺 قناة النسخ', []), "backup_set_channel", "primary"),
                ]
            )
        else:
            rows.append([btn(render_bot_button('bot.services.backup_automation.py:249:button','📺 قناة النسخ', []), "backup_set_channel", "primary")])
        rows.append([btn(render_bot_button('bot.services.backup_automation.py:250:button','📚 الحد الأقصى للنسخ', []), "backup_set_max")])
    action_row: list[InlineKeyboardButton] = []
    if DBX.has_permission(admin_id, "backup_create"):
        action_row.append(btn(render_bot_button('bot.services.backup_automation.py:253:button','💾 إنشاء نسخة الآن', []), "backup_run_now", "success"))
    if DBX.has_permission(admin_id, "backup_import"):
        action_row.append(btn(render_bot_button('bot.services.backup_automation.py:255:button','♻️ الاستعادة', []), "backup_restore_menu", "danger"))
    if action_row:
        rows.append(action_row)
    rows.append([btn(render_bot_button('bot.services.backup_automation.py:258:button','🔄 تحديث', []), "adm_backup_settings"), btn(render_bot_button('bot.services.backup_automation.py:258:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def _backup_interval_kb() -> InlineKeyboardMarkup:
    current = backup_interval_seconds()
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for seconds, label in BACKUP_INTERVALS:
        mark = "✅ " if seconds == current else ""
        pair.append(btn(render_bot_button('bot.services.backup_automation.py:268:button','{v0}{v1}', [f'{mark}',f'{label}']), f"backup_interval:{seconds}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn(render_bot_button('bot.services.backup_automation.py:274:button','✍️ فترة مخصصة بالساعات', []), "backup_interval_custom", "primary")])
    rows.append([btn(render_bot_button('bot.services.backup_automation.py:275:button','↩️ إعدادات النسخ', []), "adm_backup_settings")])
    return InlineKeyboardMarkup(rows)


def _restore_menu_kb(admin_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn(render_bot_button('bot.services.backup_automation.py:281:button','🆕 استعادة أحدث نسخة', []), "backup_restore_latest", "success")],
        [btn(render_bot_button('bot.services.backup_automation.py:282:button','📚 اختيار نسخة من القناة', []), "backup_restore_list:0", "primary")],
        [btn(render_bot_button('bot.services.backup_automation.py:283:button','📤 رفع نسخة من الجهاز', []), "backup_restore_upload", "primary")],
        [btn(render_bot_button('bot.services.backup_automation.py:284:button','📁 نسخة من مجلد backups', []), "backup_restore_local")],
        [btn(render_bot_button('bot.services.backup_automation.py:285:button','↩️ إعدادات النسخ', []), "adm_backup_settings")],
    ]
    return InlineKeyboardMarkup(rows)


def _list_backup_records(page: int = 0, page_size: int = BACKUP_LIST_PAGE_SIZE) -> tuple[list[sqlite3.Row], int]:
    channel = backup_channel_id()
    if channel is None:
        return [], 0
    with DBX.connect() as con:
        total = con.execute(
            "SELECT COUNT(*) AS c FROM backup_channel_files "
            "WHERE channel_id = ? AND status = 'ACTIVE'",
            (channel,),
        ).fetchone()["c"]
        pages = max(1, (int(total) + page_size - 1) // page_size)
        page = max(0, min(int(page), pages - 1))
        rows = con.execute(
            "SELECT * FROM backup_channel_files "
            "WHERE channel_id = ? AND status = 'ACTIVE' "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (channel, page_size, page * page_size),
        ).fetchall()
    return rows, pages


def _backup_records_kb(page: int = 0) -> InlineKeyboardMarkup:
    rows, pages = _list_backup_records(page)
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        source = "آلي" if str(row["source"]).upper() == "AUTO" else "يدوي"
        when = str(row["created_at"] or "—")
        buttons.append(
            [btn(render_bot_button('bot.services.backup_automation.py:318:button','🗄️ {v0} — {v1}', [f'{when}',f'{source}']), f"backup_pick:{int(row['id'])}", "primary")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn(render_bot_button('bot.services.backup_automation.py:322:button','⬅️ السابق', []), f"backup_restore_list:{page - 1}"))
    if page + 1 < pages:
        nav.append(btn(render_bot_button('bot.services.backup_automation.py:324:button','التالي ➡️', []), f"backup_restore_list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn(render_bot_button('bot.services.backup_automation.py:327:button','↩️ الاستعادة', []), "backup_restore_menu")])
    return InlineKeyboardMarkup(buttons)


def _get_backup_record(record_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM backup_channel_files WHERE id = ? AND status = 'ACTIVE'",
            (int(record_id),),
        ).fetchone()


def _latest_backup_record() -> Optional[sqlite3.Row]:
    channel = backup_channel_id()
    if channel is None:
        return None
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM backup_channel_files "
            "WHERE channel_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (channel,),
        ).fetchone()


def _backup_record_text(row: sqlite3.Row) -> str:
    size = int(row["file_size"] or 0)
    size_text = f"{size / 1024:.1f} KB" if size else "—"
    return (
        render_bot_text('fragment.bot.services.backup_automation.py:_backup_record_text:355','<b>🗄️ تفاصيل النسخة الاحتياطية</b>\n\n📄 الملف: <code>{v0}</code>\n📅 التاريخ: <code>{v1}</code>\n⚙️ النوع: <b>{v2}</b>\n📦 الحجم: <b>{v3}</b>\n📺 القناة: <code>{v4}</code>\n✉️ Message ID: <code>{v5}</code>\n\n⚠️ ستُنشأ نسخة أمان محلية قبل الاستعادة، ثم تُستبدل بيانات النظام بمحتويات هذه النسخة.',[f"{html(row['filename'])}",f"{html(row['created_at'])}",f"{('تلقائي' if str(row['source']).upper() == 'AUTO' else 'يدوي')}",f'{html(size_text)}',f"{html(row['channel_id'])}",f"{html(row['message_id'])}"])
    )


def _restore_confirm_kb(record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.backup_automation.py:369:button','✅ تأكيد الاستعادة', []), f"backup_restore:{int(record_id)}", "danger")],
            [btn(render_bot_button('bot.services.backup_automation.py:370:button','❌ إلغاء', []), "backup_restore_menu")],
        ]
    )


def _uploaded_restore_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.backup_automation.py:378:button','✅ تأكيد استعادة الملف المرفوع', []), "backup_restore_uploaded", "danger")],
            [btn(render_bot_button('bot.services.backup_automation.py:379:button','❌ إلغاء', []), "backup_restore_menu")],
        ]
    )


# ---------------------------------------------------------------------------
# Channel validation, send, retention, and restore
# ---------------------------------------------------------------------------


def _forwarded_channel_identifier(message: Any) -> Optional[int]:
    legacy = getattr(message, "forward_from_chat", None)
    if legacy and getattr(legacy, "id", None):
        return int(legacy.id)
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    if origin_chat and getattr(origin_chat, "id", None):
        return int(origin_chat.id)
    return None


async def _validate_backup_channel(bot: Any, identifier: Any) -> dict[str, Any]:
    try:
        chat = await bot.get_chat(identifier)
    except TelegramError as exc:
        raise ValueError(f"تعذر الوصول إلى القناة: {exc}") from exc
    if chat.type != ChatType.CHANNEL and str(chat.type) != "channel":
        raise ValueError("المعرّف لا يعود إلى قناة تيليجرام. استخدم قناة خاصة بالنسخ الاحتياطية.")
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError as exc:
        raise ValueError(f"تعذر التحقق من صلاحيات البوت داخل القناة: {exc}") from exc

    status = member.status
    is_owner = status == ChatMemberStatus.OWNER or str(status) in {"creator", "owner"}
    is_admin = status == ChatMemberStatus.ADMINISTRATOR or str(status) == "administrator"
    if not (is_owner or is_admin):
        raise ValueError("يجب إضافة البوت أدمنًا داخل قناة النسخ الاحتياطية.")
    can_post = is_owner or bool(getattr(member, "can_post_messages", False))
    can_delete = is_owner or bool(getattr(member, "can_delete_messages", False))
    if not can_post:
        raise ValueError("امنح البوت صلاحية نشر الرسائل داخل القناة.")
    if not can_delete:
        raise ValueError("امنح البوت صلاحية حذف الرسائل حتى يستطيع تطبيق الحد الأقصى للنسخ.")

    return {
        "id": int(chat.id),
        "title": str(getattr(chat, "title", "") or "قناة النسخ الاحتياطية"),
        "username": str(getattr(chat, "username", "") or ""),
    }


def _record_channel_backup(message: Any, path: Path, source: str) -> int:
    document = getattr(message, "document", None)
    if document is None:
        raise RuntimeError("لم تُرجع Telegram بيانات ملف النسخة بعد الإرسال.")
    chat = getattr(message, "chat", None)
    channel_id = int(getattr(chat, "id", backup_channel_id() or 0))
    with DBX.connect() as con:
        cur = con.execute(
            """
            INSERT INTO backup_channel_files(
                channel_id, channel_title, channel_username, message_id,
                telegram_file_id, telegram_file_unique_id, filename, file_size,
                source, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            ON CONFLICT(channel_id, message_id) DO UPDATE SET
                telegram_file_id = excluded.telegram_file_id,
                telegram_file_unique_id = excluded.telegram_file_unique_id,
                filename = excluded.filename,
                file_size = excluded.file_size,
                source = excluded.source,
                status = 'ACTIVE',
                deleted_at = NULL,
                delete_error = NULL
            """,
            (
                channel_id,
                _setting("backup_channel_title", ""),
                _setting("backup_channel_username", ""),
                int(message.message_id),
                str(document.file_id),
                str(getattr(document, "file_unique_id", "") or ""),
                path.name,
                int(getattr(document, "file_size", 0) or path.stat().st_size),
                str(source).upper(),
                now_str(),
            ),
        )
        con.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = con.execute(
            "SELECT id FROM backup_channel_files WHERE channel_id = ? AND message_id = ?",
            (channel_id, int(message.message_id)),
        ).fetchone()
        return int(row["id"])


async def enforce_backup_retention(bot: Any, channel_id: Optional[int] = None) -> tuple[int, int]:
    """Delete oldest tracked bot-created backups beyond the configured maximum."""
    channel_id = backup_channel_id() if channel_id is None else int(channel_id)
    if channel_id is None:
        return 0, 0
    maximum = backup_max_copies()
    with DBX.connect() as con:
        rows = con.execute(
            "SELECT * FROM backup_channel_files "
            "WHERE channel_id = ? AND status = 'ACTIVE' ORDER BY id DESC",
            (channel_id,),
        ).fetchall()
    overflow = rows[maximum:]
    deleted = 0
    failed = 0
    for row in overflow:
        removed = False
        error_text = ""
        try:
            await bot.delete_message(chat_id=channel_id, message_id=int(row["message_id"]))
            removed = True
        except BadRequest as exc:
            error_text = str(exc)
            lowered = error_text.lower()
            if "message to delete not found" in lowered or "message_id_invalid" in lowered:
                # It is already absent from the channel, so it should no longer
                # count against retention even though Telegram could not delete it.
                removed = True
        except (Forbidden, TelegramError) as exc:
            error_text = str(exc)

        with DBX.connect() as con:
            if removed:
                deleted += 1
                con.execute(
                    "UPDATE backup_channel_files "
                    "SET status = 'DELETED', deleted_at = ?, delete_error = NULL WHERE id = ?",
                    (now_str(), int(row["id"])),
                )
            else:
                failed += 1
                con.execute(
                    "UPDATE backup_channel_files SET delete_error = ? WHERE id = ?",
                    ((error_text or "تعذر حذف الرسالة")[:1000], int(row["id"])),
                )
            con.commit()
    return deleted, failed


async def create_and_send_channel_backup(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    actor_id: Optional[int] = None,
) -> dict[str, Any]:
    channel_id = backup_channel_id()
    if channel_id is None:
        raise RuntimeError("لم يتم تحديد قناة النسخ الاحتياطية.")

    DBX.set_setting("backup_last_attempt_at", now_str())
    DBX.set_setting("backup_last_status", "RUNNING")
    DBX.set_setting("backup_last_error", "")

    path = await asyncio.to_thread(create_backup_file)
    caption = (
        "🗄️ <b>نسخة احتياطية للنظام</b>\n\n"
        f"📅 التاريخ: <code>{html(now_str())}</code>\n"
        f"⚙️ النوع: <b>{'تلقائية' if str(source).upper() == 'AUTO' else 'يدوية'}</b>\n"
        f"📄 الملف: <code>{html(path.name)}</code>"
    )
    try:
        with path.open("rb") as handle:
            message = await context.bot.send_document(
                chat_id=channel_id,
                document=handle,
                filename=path.name,
                caption=render_bot_text('bot.services.backup_automation.py:550:message','{v0}', [str(caption)]),
                parse_mode=ParseMode.HTML,
                disable_notification=str(source).upper() == "AUTO",
            )
        record_id = _record_channel_backup(message, path, source)
        deleted, cleanup_failed = await enforce_backup_retention(context.bot, channel_id)
        DBX.set_setting("backup_last_success_at", now_str())
        DBX.set_setting("backup_last_status", "SUCCESS")
        DBX.set_setting("backup_last_error", "")
        DBX.audit(
            actor_id,
            "AUTO_BACKUP" if actor_id is None else str(actor_id),
            "AUTO_BACKUP_CREATE" if str(source).upper() == "AUTO" else "MANUAL_CHANNEL_BACKUP",
            "backup",
            path.name,
            f"channel={channel_id} record_id={record_id} deleted={deleted} cleanup_failed={cleanup_failed}",
        )
        return {
            "path": path,
            "record_id": record_id,
            "message_id": int(message.message_id),
            "deleted": deleted,
            "cleanup_failed": cleanup_failed,
        }
    except Exception as exc:
        DBX.set_setting("backup_last_status", "FAILED")
        DBX.set_setting("backup_last_error", f"{type(exc).__name__}: {exc}"[:1000])
        log.exception("Channel backup failed source=%s actor=%s", source, actor_id)
        raise


def _validate_backup_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("ملف النسخة يجب أن يحتوي على كائن JSON رئيسي.")
    required_any = {"settings_v2", "customers", "orders_v2", "user_wallets"}
    if not required_any.intersection(payload):
        raise ValueError("الملف لا يبدو كنسخة احتياطية صادرة من هذا النظام.")
    for key in ("settings_v2", "customers", "orders_v2"):
        if key in payload and not isinstance(payload[key], list):
            raise ValueError(f"القسم {key} داخل النسخة غير صالح.")
    return payload


def _restore_from_path(path: Path) -> Path:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("ملف النسخة غير موجود.")
    if path.stat().st_size > BACKUP_MAX_UPLOAD_BYTES:
        raise ValueError("حجم ملف النسخة يتجاوز الحد المسموح.")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    payload = _validate_backup_payload(payload)
    # Keep a local safety point using the already-existing backup implementation.
    safety_path = create_backup_file()
    DBX.import_snapshot(payload)
    _backup_schema_and_defaults()
    return safety_path


async def _download_channel_backup(context: ContextTypes.DEFAULT_TYPE, row: sqlite3.Row) -> Path:
    telegram_file = await context.bot.get_file(str(row["telegram_file_id"]))
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row["filename"] or "channel_backup.json"))
    target = BACKUP_DIR / f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    await telegram_file.download_to_drive(custom_path=target)
    return target


async def _restore_channel_record(
    context: ContextTypes.DEFAULT_TYPE,
    row: sqlite3.Row,
    *,
    actor_id: int,
) -> Path:
    downloaded = await _download_channel_backup(context, row)
    safety_path = await asyncio.to_thread(_restore_from_path, downloaded)
    DBX.audit(
        actor_id,
        str(actor_id),
        "RESTORE_CHANNEL_BACKUP",
        "backup",
        int(row["id"]),
        f"filename={row['filename']} safety={safety_path.name}",
    )
    return safety_path


# Extend the existing full snapshot with the new channel-tracking table. The
# previous export/import functions remain responsible for all original tables.
_backup_previous_export_snapshot = DB.export_snapshot
_backup_previous_import_snapshot = DB.import_snapshot


def _backup_automation_export_snapshot(self: DB) -> dict[str, Any]:
    payload = _backup_previous_export_snapshot(self)
    with self.connect() as con:
        payload["backup_channel_files"] = [
            dict(row)
            for row in con.execute("SELECT * FROM backup_channel_files ORDER BY id").fetchall()
        ]
    return payload


def _backup_automation_import_snapshot(self: DB, payload: dict[str, Any]):
    _backup_previous_import_snapshot(self, payload)
    _backup_schema_and_defaults(self)
    rows = payload.get("backup_channel_files", [])
    if not isinstance(rows, list):
        return
    with self.connect() as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(backup_channel_files)").fetchall()]
        for row in rows:
            if not isinstance(row, dict):
                continue
            usable = {key: row.get(key) for key in columns if key in row}
            if not usable:
                continue
            names = list(usable)
            placeholders = ",".join("?" for _ in names)
            # Merge instead of deleting current tracking history. A backup file is
            # created before its Telegram message exists, so replacing the table
            # during restore would otherwise make the restored file disappear from
            # the channel-copy list.
            con.execute(
                f"INSERT OR IGNORE INTO backup_channel_files({','.join(names)}) VALUES ({placeholders})",
                [usable[name] for name in names],
            )
        con.commit()


DB.export_snapshot = _backup_automation_export_snapshot
DB.import_snapshot = _backup_automation_import_snapshot


# ---------------------------------------------------------------------------
# Permissions and administration UI
# ---------------------------------------------------------------------------

ADMIN_PERMISSION_LABELS["backup_manage"] = "⚙️ إدارة إعدادات النسخ التلقائي"
ALL_ADMIN_PERMISSIONS = tuple(ADMIN_PERMISSION_LABELS)

_backup_previous_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _backup_previous_admin_kb(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    if DBX.has_permission(admin_id, "backup_manage"):
        exists = any(
            getattr(button, "callback_data", None) == "adm_backup_settings"
            for row in rows
            for button in row
        )
        if not exists:
            insert_at = max(0, len(rows) - 1)
            rows.insert(insert_at, [btn(render_bot_button('bot.services.backup_automation.py:706:button','🗄️ إعدادات النسخ الاحتياطي', []), "adm_backup_settings", "primary")])
    return InlineKeyboardMarkup(rows)


_backup_previous_callback_permission = callback_permission


def callback_permission(data: str) -> Optional[str]:
    if data in {
        "adm_backup_settings",
        "backup_interval_menu",
        "backup_interval_custom",
        "backup_set_channel",
        "backup_set_max",
    } or data.startswith("backup_mode:") or data.startswith("backup_interval:"):
        return "backup_manage"
    if data == "backup_run_now":
        return "backup_create"
    if data in {
        "backup_restore_menu",
        "backup_restore_latest",
        "backup_restore_upload",
        "backup_restore_uploaded",
        "backup_restore_local",
    } or data.startswith("backup_restore_list:") or data.startswith("backup_pick:") or data.startswith("backup_restore:"):
        return "backup_import"
    return _backup_previous_callback_permission(data)


_backup_previous_state_permission = state_permission


def state_permission(state: Optional[str]) -> Optional[str]:
    if state in {"await_backup_channel", "await_backup_custom_hours", "await_backup_max_copies"}:
        return "backup_manage"
    if state == "await_backup_upload":
        return "backup_import"
    return _backup_previous_state_permission(state)


# ---------------------------------------------------------------------------
# Callback and message handlers
# ---------------------------------------------------------------------------

_backup_previous_on_callback = on_callback
_backup_previous_on_message = on_message


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    if data == "adm_backup_settings":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك صلاحية إدارة النسخ الاحتياطية", show_alert=True)
            return
        await query.answer()
        set_state(context, None)
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:765:message','{v0}', [str(_backup_settings_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if data.startswith("backup_mode:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        requested = data.split(":", 1)[1].upper()
        if requested not in {BACKUP_MODE_MANUAL, BACKUP_MODE_AUTO}:
            await query.answer("وضع غير صالح", show_alert=True)
            return
        DBX.set_setting("backup_mode", requested)
        _reset_next_run()
        DBX.audit(user.id, actor_name(user), "SET_BACKUP_MODE", "settings", "backup_mode", requested)
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:784:message','{v0}', [str(_backup_settings_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if data == "backup_interval_menu":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:796:message','<b>⏱️ فترة النسخ التلقائي</b>\n\nاختر الفترة المطلوبة. يمكن تعديلها في أي وقت.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_interval_kb(),
        )
        return

    if data.startswith("backup_interval:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        try:
            seconds = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("فترة غير صالحة", show_alert=True)
            return
        if seconds not in {item[0] for item in BACKUP_INTERVALS}:
            await query.answer("الفترة غير مدعومة", show_alert=True)
            return
        await query.answer("تم حفظ الفترة")
        DBX.set_setting("backup_interval_seconds", str(seconds))
        _reset_next_run()
        DBX.audit(user.id, actor_name(user), "SET_BACKUP_INTERVAL", "settings", seconds, _interval_label(seconds))
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:819:message','{v0}', [str(_backup_settings_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if data == "backup_interval_custom":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        set_state(context, "await_backup_custom_hours")
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:832:message','<b>✍️ فترة مخصصة</b>\n\nأرسل عدد الساعات من 1 إلى 8760. مثال: <code>36</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.backup_automation.py:835:button','↩️ إلغاء', []), "adm_backup_settings")]]),
        )
        return

    if data == "backup_set_channel":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        set_state(context, "await_backup_channel")
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:845:message','<b>📺 تحديد قناة النسخ الاحتياطية</b>\n\nأرسل أحد الخيارات التالية:\n• معرّف القناة الذي يبدأ غالبًا بـ <code>-100</code>.\n• اسم القناة العام مثل <code>@backup_channel</code>.\n• أو أعد توجيه رسالة من القناة إلى البوت.\n\nيجب أن يكون البوت أدمنًا بصلاحية نشر الرسائل وحذفها.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.backup_automation.py:853:button','↩️ إلغاء', []), "adm_backup_settings")]]),
        )
        return

    if data == "backup_set_max":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_manage"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        set_state(context, "await_backup_max_copies")
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:863:message','<b>📚 الحد الأقصى للنسخ</b>\n\nأرسل عددًا من 1 إلى 5000، مثل: <code>100</code>.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.backup_automation.py:866:button','↩️ إلغاء', []), "adm_backup_settings")]]),
        )
        return

    if data == "backup_run_now":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_create"):
            await query.answer("لا تملك صلاحية إنشاء النسخ", show_alert=True)
            return
        await query.answer("جاري إنشاء النسخة")
        progress = await query.edit_message_text(render_bot_text('bot.services.backup_automation.py:875:message','⏳ جاري إنشاء النسخة وإرسالها إلى قناة النسخ الاحتياطية...', []))
        try:
            result = await create_and_send_channel_backup(context, source="MANUAL", actor_id=user.id)
            cleanup_note = (
                f"\n🧹 حُذفت {result['deleted']} نسخة قديمة."
                if result["deleted"]
                else ""
            )
            cleanup_warning = (
                f"\n⚠️ تعذر حذف {result['cleanup_failed']} نسخة قديمة؛ تحقق من صلاحية الحذف."
                if result["cleanup_failed"]
                else ""
            )
            await progress.edit_text(
                "✅ <b>تم إنشاء النسخة الاحتياطية وإرسالها</b>\n\n"
                f"📄 <code>{html(result['path'].name)}</code>{cleanup_note}{cleanup_warning}",
                parse_mode=ParseMode.HTML,
                reply_markup=_backup_settings_kb(user.id),
            )
        except Exception as exc:
            log.exception("Manual channel backup failed admin=%s", user.id)
            await progress.edit_text(
                f"❌ <b>فشل إنشاء النسخة</b>\n\n<code>{html(exc)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_backup_settings_kb(user.id),
            )
        return

    if data == "backup_restore_menu":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك صلاحية الاستعادة", show_alert=True)
            return
        await query.answer()
        set_state(context, None)
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:909:message','<b>♻️ استعادة نسخة احتياطية</b>\n\nاختر مصدر النسخة. لن تبدأ الاستعادة قبل عرض التفاصيل وطلب التأكيد.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=_restore_menu_kb(user.id),
        )
        return

    if data == "backup_restore_latest":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        row = _latest_backup_record()
        if not row:
            await query.answer("لا توجد نسخة متتبعة داخل القناة", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:926:message','{v0}', [str(_backup_record_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=_restore_confirm_kb(int(row["id"])),
        )
        return

    if data.startswith("backup_restore_list:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        rows, _ = _list_backup_records(page)
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:943:message','<b>📚 النسخ الموجودة في قناة النسخ</b>\n\n{v0}', [str('اختر النسخة المراد استعادتها.' if rows else 'لا توجد نسخ متتبعة حتى الآن.')]),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_records_kb(page),
        )
        return

    if data.startswith("backup_pick:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        try:
            record_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("نسخة غير صالحة", show_alert=True)
            return
        row = _get_backup_record(record_id)
        if not row:
            await query.answer("النسخة غير موجودة أو حُذفت", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:965:message','{v0}', [str(_backup_record_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=_restore_confirm_kb(record_id),
        )
        return

    if data.startswith("backup_restore:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        try:
            record_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("نسخة غير صالحة", show_alert=True)
            return
        row = _get_backup_record(record_id)
        if not row:
            await query.answer("النسخة غير موجودة أو حُذفت", show_alert=True)
            return
        await query.answer("بدأت الاستعادة")
        progress = await query.edit_message_text(render_bot_text('bot.services.backup_automation.py:986:message','⏳ جاري تنزيل النسخة والتحقق منها ثم استعادتها...', []))
        try:
            safety_path = await _restore_channel_record(context, row, actor_id=user.id)
            set_state(context, None)
            await progress.edit_text(
                "✅ <b>تمت استعادة النسخة بنجاح</b>\n\n"
                f"🛡️ نسخة الأمان السابقة: <code>{html(safety_path.name)}</code>\n"
                "أعد تشغيل البوت إذا كانت النسخة غيّرت إعدادات تشغيل رئيسية.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb(user.id),
            )
        except Exception as exc:
            log.exception("Channel backup restore failed admin=%s record_id=%s", user.id, record_id)
            await progress.edit_text(
                f"❌ <b>فشلت الاستعادة</b>\n\n<code>{html(exc)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_restore_menu_kb(user.id),
            )
        return

    if data == "backup_restore_upload":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        set_state(context, "await_backup_upload")
        context.user_data.pop("uploaded_backup_path", None)
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:1013:message','<b>📤 رفع نسخة من الجهاز</b>\n\nأرسل ملف النسخة بصيغة <code>.json</code> كمستند. سيتم التحقق منه ثم طلب التأكيد قبل الاستعادة.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.backup_automation.py:1017:button','↩️ إلغاء', []), "backup_restore_menu")]]),
        )
        return

    if data == "backup_restore_uploaded":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        path_value = str(context.user_data.get("uploaded_backup_path") or "")
        path = Path(path_value) if path_value else None
        if not path or not path.exists():
            await query.answer("انتهت جلسة الملف؛ ارفعه مرة أخرى", show_alert=True)
            return
        await query.answer("بدأت الاستعادة")
        progress = await query.edit_message_text(render_bot_text('bot.services.backup_automation.py:1031:message','⏳ جاري التحقق من الملف واستعادة البيانات...', []))
        try:
            safety_path = await asyncio.to_thread(_restore_from_path, path)
            DBX.audit(
                user.id,
                actor_name(user),
                "RESTORE_UPLOADED_BACKUP",
                "backup",
                path.name,
                f"safety={safety_path.name}",
            )
            context.user_data.pop("uploaded_backup_path", None)
            set_state(context, None)
            await progress.edit_text(
                "✅ <b>تمت استعادة الملف المرفوع بنجاح</b>\n\n"
                f"🛡️ نسخة الأمان السابقة: <code>{html(safety_path.name)}</code>\n"
                "أعد تشغيل البوت إذا تغيرت إعدادات تشغيل رئيسية.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb(user.id),
            )
        except Exception as exc:
            log.exception("Uploaded backup restore failed admin=%s path=%s", user.id, path)
            await progress.edit_text(
                f"❌ <b>فشلت الاستعادة</b>\n\n<code>{html(exc)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_restore_menu_kb(user.id),
            )
        return

    if data == "backup_restore_local":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "backup_import"):
            await query.answer("لا تملك الصلاحية", show_alert=True)
            return
        await query.answer()
        # Preserve the original local-file restore state and handler.
        set_state(context, "await_import_backup")
        await query.edit_message_text(
            render_bot_text('bot.services.backup_automation.py:1067:message','أرسل اسم ملف النسخة الموجود داخل مجلد <code>backups</code>.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.backup_automation.py:1070:button','↩️ إلغاء', []), "backup_restore_menu")]]),
        )
        return

    await _backup_previous_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state, _ = current_state(context)
    if state not in {
        "await_backup_channel",
        "await_backup_custom_hours",
        "await_backup_max_copies",
        "await_backup_upload",
    }:
        await _backup_previous_on_message(update, context)
        return

    user = update.effective_user
    if not user or not DBX.is_admin(user.id):
        set_state(context, None)
        if update.effective_message:
            await update.effective_message.reply_text(render_bot_text('bot.services.backup_automation.py:1092:message','⛔ للأدمن فقط.', []))
        return

    message = update.effective_message

    if state == "await_backup_channel":
        if not DBX.has_permission(user.id, "backup_manage"):
            set_state(context, None)
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1100:message','⛔ لا تملك صلاحية إدارة النسخ الاحتياطية.', []))
            return
        identifier: Any = _forwarded_channel_identifier(message)
        if identifier is None:
            text = (message.text or "").strip()
            if re.fullmatch(r"-?\d+", text):
                identifier = int(text)
            elif re.fullmatch(r"@[A-Za-z0-9_]{5,}", text):
                identifier = text
            else:
                await message.reply_text(
                    render_bot_text('bot.services.backup_automation.py:1110:message','❌ أرسل Channel ID صالحًا، أو @username، أو أعد توجيه رسالة من القناة.', [])
                )
                return
        try:
            channel = await _validate_backup_channel(context.bot, identifier)
        except ValueError as exc:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1117:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
            return
        if callable(globals().get("set_project_backup_channel")):
            set_project_backup_channel(str(channel["id"]), channel["title"])
        else:
            DBX.set_setting("backup_channel_id", str(channel["id"]))
            DBX.set_setting("backup_channel_title", channel["title"])
        DBX.set_setting("backup_channel_username", channel["username"])
        _reset_next_run()
        DBX.audit(
            user.id,
            actor_name(user),
            "SET_BACKUP_CHANNEL",
            "settings",
            channel["id"],
            channel["title"],
        )
        set_state(context, None)
        await message.reply_text(
            render_bot_text('bot.services.backup_automation.py:1135:message','✅ <b>تم حفظ قناة النسخ الاحتياطية</b>\n\n📺 {v0}\n🆔 <code>{v1}</code>', [f"{html(channel['title'])}",f"{html(channel['id'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if state == "await_backup_custom_hours":
        if not DBX.has_permission(user.id, "backup_manage"):
            set_state(context, None)
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1147:message','⛔ لا تملك الصلاحية.', []))
            return
        text = (message.text or "").strip().translate(ARABIC_DIGITS)
        try:
            hours = int(text)
        except ValueError:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1153:message','❌ أرسل عدد ساعات صحيحًا من 1 إلى 8760.', []))
            return
        if not 1 <= hours <= 8760:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1156:message','❌ يجب أن تكون الفترة بين ساعة واحدة و8760 ساعة.', []))
            return
        seconds = hours * 3600
        DBX.set_setting("backup_interval_seconds", str(seconds))
        _reset_next_run()
        DBX.audit(user.id, actor_name(user), "SET_BACKUP_INTERVAL", "settings", seconds, f"{hours} hours")
        set_state(context, None)
        await message.reply_text(
            render_bot_text('bot.services.backup_automation.py:1163:message','✅ تم ضبط النسخ التلقائي <b>كل {v0} ساعة</b>.', [f'{hours}']),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if state == "await_backup_max_copies":
        if not DBX.has_permission(user.id, "backup_manage"):
            set_state(context, None)
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1173:message','⛔ لا تملك الصلاحية.', []))
            return
        text = (message.text or "").strip().translate(ARABIC_DIGITS)
        try:
            maximum = int(text)
        except ValueError:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1179:message','❌ أرسل عددًا صحيحًا من 1 إلى 5000.', []))
            return
        if not 1 <= maximum <= 5000:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1182:message','❌ يجب أن يكون الحد من 1 إلى 5000.', []))
            return
        DBX.set_setting("backup_max_copies", str(maximum))
        deleted = failed = 0
        if backup_channel_id() is not None:
            deleted, failed = await enforce_backup_retention(context.bot)
        DBX.audit(user.id, actor_name(user), "SET_BACKUP_MAX", "settings", maximum, f"deleted={deleted} failed={failed}")
        set_state(context, None)
        warning = f"\n⚠️ تعذر حذف {failed} رسالة قديمة." if failed else ""
        await message.reply_text(
            render_bot_text('bot.services.backup_automation.py:1191:message','✅ تم ضبط الحد الأقصى على <b>{v0}</b> نسخة.\n🧹 حُذفت الآن <b>{v1}</b> نسخة زائدة.{v2}', [f'{maximum}',f'{deleted}',f'{warning}']),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_settings_kb(user.id),
        )
        return

    if state == "await_backup_upload":
        if not DBX.has_permission(user.id, "backup_import"):
            set_state(context, None)
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1202:message','⛔ لا تملك صلاحية الاستعادة.', []))
            return
        document = message.document
        if document is None:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1206:message','❌ أرسل ملف النسخة كمستند بصيغة JSON.', []))
            return
        filename = str(document.file_name or "uploaded_backup.json")
        if not filename.lower().endswith(".json"):
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1210:message','❌ الملف يجب أن يكون بصيغة .json.', []))
            return
        if int(document.file_size or 0) > BACKUP_MAX_UPLOAD_BYTES:
            await message.reply_text(render_bot_text('bot.services.backup_automation.py:1213:message','❌ حجم الملف يتجاوز الحد المسموح.', []))
            return
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        target = BACKUP_DIR / f"uploaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
        try:
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=target)
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            _validate_backup_payload(payload)
        except Exception as exc:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                log.warning("Could not delete rejected uploaded backup path=%s", target, exc_info=True)
            await message.reply_text(
                render_bot_text('bot.services.backup_automation.py:1227:message','❌ تعذر قبول الملف: <code>{v0}</code>', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
            )
            return
        context.user_data["uploaded_backup_path"] = str(target)
        set_state(context, None)
        await message.reply_text(
            render_bot_text('bot.services.backup_automation.py:1234:message','✅ <b>تم التحقق من ملف النسخة</b>\n\n📄 <code>{v0}</code>\n📦 الحجم: <b>{v1} KB</b>\n\nاضغط تأكيد لبدء الاستعادة.', [f'{html(filename)}',f'{target.stat().st_size / 1024:.1f}']),
            parse_mode=ParseMode.HTML,
            reply_markup=_uploaded_restore_confirm_kb(),
        )
        return


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


async def backup_scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if backup_mode() != BACKUP_MODE_AUTO:
        return
    channel_id = backup_channel_id()
    if channel_id is None:
        DBX.set_setting("backup_last_status", "FAILED")
        DBX.set_setting("backup_last_error", "الوضع تلقائي ولكن قناة النسخ غير محددة.")
        return

    async with _BACKUP_JOB_LOCK:
        if backup_mode() != BACKUP_MODE_AUTO:
            return
        now = datetime.now()
        next_run = _parse_datetime(_setting("backup_next_run_at", ""))
        if next_run is None:
            _reset_next_run()
            return
        if now < next_run:
            return

        # Claim the next slot before any network or disk work. A single bot
        # instance is required elsewhere in the project as well, and this also
        # prevents overlapping JobQueue ticks inside that instance.
        DBX.set_setting(
            "backup_next_run_at",
            _format_datetime(now + timedelta(seconds=backup_interval_seconds())),
        )
        try:
            await create_and_send_channel_backup(context, source="AUTO", actor_id=None)
        except Exception as exc:
            log.exception("automatic backup failed")
            DBX.set_setting(
                "backup_next_run_at",
                _format_datetime(datetime.now() + timedelta(seconds=BACKUP_FAILURE_RETRY_SECONDS)),
            )
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        render_bot_text('bot.services.backup_automation.py:1286:message','❌ <b>فشل النسخ الاحتياطي التلقائي</b>\n\n<code>{v0}</code>\n\nستتم إعادة المحاولة بعد نحو {v1} دقيقة.', [f"{html(type(exc).__name__ + ': ' + str(exc))}",f'{BACKUP_FAILURE_RETRY_SECONDS // 60}'])
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.exception("failed to notify admin about automatic backup failure")


_backup_previous_post_init = post_init


async def post_init(application: Application):
    await _backup_previous_post_init(application)
    if application.job_queue is None:
        raise RuntimeError("JobQueue غير متاح. ثبّت python-telegram-bot[job-queue].")
    existing = application.job_queue.get_jobs_by_name("automatic-backup-scheduler")
    if not existing:
        application.job_queue.run_repeating(
            backup_scheduler_tick,
            interval=BACKUP_SCHEDULER_TICK_SECONDS,
            first=20,
            name="automatic-backup-scheduler",
        )
    log.info(
        "Backup scheduler enabled mode=%s interval=%ss channel=%s max=%s",
        backup_mode(),
        backup_interval_seconds(),
        backup_channel_id(),
        backup_max_copies(),
    )


_export_globals(globals())

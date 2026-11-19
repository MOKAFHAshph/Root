"""Open the isolated, Telegram-signed administrator credential editor."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from bot.config import ADMIN_ID


def _url() -> str | None:
    # The existing Mini App already has routing for /api/admin/*.
    from bot import runtime
    raw = str(runtime.get("MINI_APP_URL") or "").strip()
    u = urlsplit(raw)
    if u.scheme != "https" or not u.netloc or u.username or u.password:
        return None
    return urlunsplit(("https", u.netloc, "/api/admin/jaib-config/ui", "", ""))


async def on_jaib_admin_launcher(update, context):
    query = update.callback_query
    if not query:
        return
    uid = int(query.from_user.id)
    chat = query.message.chat if query.message else None
    if uid != int(ADMIN_ID) or not chat or chat.type != "private" or int(chat.id) != uid:
        await query.answer("هذه العملية للأدمن الرئيسي في المحادثة الخاصة فقط", show_alert=True)
        return
    link = _url()
    if not link:
        await query.answer("رابط Mini App HTTPS غير مضبوط", show_alert=True)
        return
    await query.answer()
    await query.message.reply_text(
        "🔐 إدارة بيانات محفظة جيب\n\n"
        "افتح الصفحة الآمنة، والصق تقرير Reqable/JSON هناك فقط. "
        "يمكنك أيضاً تعديل اسم المستلم وأرقام التحويل.\n\n"
        "⚠️ لا ترسل كلمات المرور أو الرموز أو تقرير Reqable في رسائل تيليجرام.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔒 فتح محرر محفظة جيب", web_app=WebAppInfo(url=link)
        )]]),
    )


async def cmd_jaib_admin_config(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or int(user.id) != int(ADMIN_ID) or chat.type != "private":
        await update.effective_message.reply_text("غير مصرح لهذه العملية.")
        return
    link = _url()
    if not link:
        await update.effective_message.reply_text("رابط Mini App HTTPS غير مضبوط.")
        return
    await update.effective_message.reply_text(
        "🔐 لتحديث بيانات جيب، استخدم الصفحة الآمنة فقط، وليس رسائل تيليجرام.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🔒 إدارة بيانات جيب", web_app=WebAppInfo(url=link)
        )]]),
    )

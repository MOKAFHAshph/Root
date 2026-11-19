"""User/admin views and broadcast delivery.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- واجهات المستخدم والإدارة ----------------


def delivery_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.services.admin_communication.py:15:button','🌐 السحب عبر عنوان المحفظة', []), f"delivery_chain:{order_id}", "primary")],
            [btn(render_bot_button('bot.services.admin_communication.py:16:button','🏠 القائمة الرئيسية', []), "main_menu")],
        ]
    )


_legacy_admin_kb = admin_kb


def admin_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    base = _legacy_admin_kb(admin_id)
    rows = [list(row) for row in base.inline_keyboard]
    for row in rows:
        for index, button in enumerate(row):
            if button.callback_data == "adm_recent_orders":
                row[index] = btn(render_bot_button('bot.services.admin_communication.py:30:button','📜 سجل العمليات', []), "adm_recent_orders")
    if rows and any(button.callback_data == "main_menu" for button in rows[-1]):
        back = rows.pop()
    else:
        back = [btn(render_bot_button('bot.services.admin_communication.py:34:button','🏠 رجوع', []), "main_menu")]
    permissions = DBX.get_admin_permissions(admin_id)
    if "broadcast_send" in permissions:
        rows.append([btn(render_bot_button('bot.services.admin_communication.py:37:button','📢 إذاعة للكل', []), "adm_broadcast", "primary")])
    channel_row: list[InlineKeyboardButton] = []
    if "notification_channel_manage" in permissions:
        channel_row.append(btn(render_bot_button('bot.services.admin_communication.py:40:button','📺 قناة الإشعارات', []), "adm_notification_channel"))
    if "subscriptions_manage" in permissions:
        channel_row.append(btn(render_bot_button('bot.services.admin_communication.py:42:button','✅ الاشتراك الإجباري', []), "adm_required_channels"))
    if channel_row:
        rows.append(channel_row)
    if "settings_view" in permissions:
        rows.append([btn(render_bot_button('bot.services.admin_communication.py:46:button','⚙️ الإعدادات', []), "adm_settings_overview")])
    if int(admin_id) == int(ADMIN_ID):  # JAIB_SECURE_CONFIG_V1
        rows.append([btn("🔐 إدارة بيانات محفظة جيب", "adm_jaib_secure", "primary")])
    rows.append(back)
    return InlineKeyboardMarkup(rows)


_legacy_callback_permission = callback_permission


def callback_permission(data: str) -> Optional[str]:
    exact = {
        "adm_broadcast": "broadcast_send",
        "adm_required_channels": "subscriptions_manage",
        "adm_required_channel_add": "subscriptions_manage",
        "adm_notification_channel": "notification_channel_manage",
        "adm_notification_channel_set": "notification_channel_manage",
        "adm_notification_channel_clear": "notification_channel_manage",
        "adm_settings_overview": "settings_view",
    }
    if data in exact:
        return exact[data]
    if data.startswith("adm_required_channel_"):
        return "subscriptions_manage"
    return _legacy_callback_permission(data)


_legacy_state_permission = state_permission


def state_permission(state: Optional[str]) -> Optional[str]:
    enhanced = {
        "await_broadcast_message": "broadcast_send",
        "await_required_channel": "subscriptions_manage",
        "await_notification_channel": "notification_channel_manage",
    }
    return enhanced.get(state or "") or _legacy_state_permission(state)


def required_channels_admin_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in list_required_channels(False):
        title = channel["title"] or channel["username"] or channel["chat_id"]
        rows.append(
            [
                btn(render_bot_button('bot.services.admin_communication.py:89:button','📢 {v0}', [f'{title}']), f"adm_required_channel_view:{channel['id']}", "primary"),
                btn(render_bot_button('bot.services.admin_communication.py:90:button','🗑️', []), f"adm_required_channel_delete:{channel['id']}", "danger"),
            ]
        )
    rows.append([btn(render_bot_button('bot.services.admin_communication.py:93:button','➕ إضافة قناة', []), "adm_required_channel_add", "success")])
    rows.append([btn(render_bot_button('bot.services.admin_communication.py:94:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


def notification_channel_kb() -> InlineKeyboardMarkup:
    rows = [[btn(render_bot_button('bot.services.admin_communication.py:99:button','📺 تعيين/تعديل القناة', []), "adm_notification_channel_set", "primary")]]
    if DBX.get_setting("notification_channel_id"):
        rows.append([btn(render_bot_button('bot.services.admin_communication.py:101:button','🗑️ حذف قناة الإشعارات', []), "adm_notification_channel_clear", "danger")])
    rows.append([btn(render_bot_button('bot.services.admin_communication.py:102:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(rows)


async def show_main(target: Any, context: ContextTypes.DEFAULT_TYPE):
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    register_bot_user(user)
    if not await ensure_required_subscription(target, context, user.id):
        return
    customer = DBX.get_customer_by_telegram(user.id)
    is_admin = DBX.is_admin(user.id)
    if not is_admin and (not customer or customer["status"] != "ACTIVE"):
        text = (
            render_bot_text('fragment.bot.services.admin_communication.py:show_main:116','⛔ <b>الوصول غير متاح</b>\n\nهذا البوت مخصص للعملاء المضافين مسبقًا فقط.\nتواصل مع الأدمن {v0} لربط Telegram ID الخاص بك.',[f'{html(ADMIN_CONTACT)}'])
        )
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(render_bot_text('bot.services.admin_communication.py:120:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML)
        elif hasattr(target, "effective_message") and target.effective_message:
            await target.effective_message.reply_text(render_bot_text('bot.services.admin_communication.py:122:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML)
        else:
            await target.reply_text(render_bot_text('bot.services.admin_communication.py:124:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML)
        return
    text = render_bot_text('fragment.bot.services.admin_communication.py:show_main:127','<b>{v0}</b>\n\nأهلاً <b>{v1}</b>',[f'{html(APP_NAME)}',f'{html(user.first_name)}'])
    if customer:
        text += render_bot_text('fragment.bot.services.admin_communication.py:show_main:129','\n👤 العميل: <b>{v0}</b>',[f"{html(customer['full_name'])}"])
    text += render_bot_text('fragment.bot.services.admin_communication.py:show_main:130','\n💵 الحد الأدنى: <b>{v0} USDT</b>',[f'{html(format_display_amount(MIN_USDT_ORDER))}'])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(render_bot_text('bot.services.admin_communication.py:131:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=user_main_kb(is_admin))
    elif hasattr(target, "effective_message") and target.effective_message:
        await target.effective_message.reply_text(render_bot_text('bot.services.admin_communication.py:133:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=user_main_kb(is_admin))
    else:
        await target.reply_text(render_bot_text('bot.services.admin_communication.py:135:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=user_main_kb(is_admin))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, None)
    user = update.effective_user
    register_bot_user(user)
    if not bot_user_welcome_sent(user.id):
        await update.effective_message.reply_text(render_bot_text('bot.services.admin_communication.py:143:message','{v0}', [str(welcome_text(user))]), parse_mode=ParseMode.HTML)
        mark_bot_user_welcome_sent(user.id)
    await show_main(update, context)


# ---------------- الإذاعة ----------------


def _retry_after_seconds(exc: RetryAfter) -> float:
    value = getattr(exc, "retry_after", 1)
    if hasattr(value, "total_seconds"):
        return max(1.0, float(value.total_seconds()))
    try:
        return max(1.0, float(value))
    except Exception:
        return 1.0


async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    admin = update.effective_user
    targets = list_broadcast_targets()
    total = len(targets)
    sent = 0
    failed = 0
    progress = await message.reply_text(render_bot_text('bot.services.admin_communication.py:168:message','⏳ بدأت الإذاعة إلى {v0} مستخدم...', [f'{total}']))
    for index, target_id in enumerate(targets, start=1):
        try:
            await context.bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            sent += 1
            mark_bot_user_blocked(target_id, False)
        except RetryAfter as exc:
            await asyncio.sleep(_retry_after_seconds(exc))
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1
            except TelegramError:
                failed += 1
        except Forbidden:
            failed += 1
            mark_bot_user_blocked(target_id, True)
        except TelegramError as exc:
            failed += 1
            log.warning("broadcast failed user=%s: %s", target_id, exc)
        if BROADCAST_DELAY_SECONDS > 0:
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)
        if index % 50 == 0:
            try:
                await progress.edit_text(
                    f"⏳ جاري الإرسال...\n\n✅ نجح: {sent}\n❌ فشل: {failed}\n👥 تم فحص: {index}/{total}"
                )
            except TelegramError:
                log.debug("Could not update broadcast progress message", exc_info=True)
    with DBX.connect() as con:
        con.execute(
            """
            INSERT INTO broadcasts(
                admin_id, source_chat_id, source_message_id, total_users,
                sent_count, failed_count, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (admin.id, message.chat_id, message.message_id, total, sent, failed, now_str(), now_str()),
        )
        con.commit()
    DBX.audit(admin.id, actor_name(admin), "BROADCAST", "broadcast", message.message_id, f"total={total}; sent={sent}; failed={failed}")
    set_state(context, None)
    await progress.edit_text(
        "<b>📢 انتهت الإذاعة</b>\n\n"
        f"✅ تم الإرسال بنجاح: <b>{sent}</b>\n"
        f"❌ فشل الإرسال: <b>{failed}</b>\n"
        f"👥 إجمالي المستخدمين: <b>{total}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb(admin.id),
    )




_export_globals(globals())

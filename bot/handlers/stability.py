"""Stability-layer callbacks and text routing.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- معالجات الأزرار والرسائل الجديدة ----------------

_legacy_on_callback = on_callback
_legacy_on_text = on_text


async def _handle_admin_payment_review(query: Any, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user = query.from_user
    if not DBX.has_permission(user.id, "orders_manage"):
        await query.answer("لا تملك صلاحية إدارة العمليات", show_alert=True)
        return
    order_id = int(data.split(":", 1)[1])
    order = DBX.get_order(order_id)
    if not order or order["status"] != "PAYMENT_REVIEW":
        await query.answer("العملية ليست بانتظار المراجعة", show_alert=True)
        return
    if data.startswith("adm_payment_reject:"):
        DBX.update_order(
            order_id,
            status="REJECTED",
            rejection_reason="ADMIN_REJECTED",
            admin_reviewed_by=user.id,
            admin_reviewed_at=now_str(),
            error_message="Rejected by administrator after payment-data review",
        )
        DBX.audit(user.id, actor_name(user), "REJECT_PAYMENT_REVIEW", "order", order["order_code"])
        try:
            await context.bot.send_message(
                order["telegram_user_id"],
                render_bot_text('bot.handlers.stability.py:36:message','<b>❌ تم رفض العملية بعد مراجعة الإدارة</b>\n\n🧾 الطلب: <code>{v0}</code>\n🔢 رقم العملية: <code>{v1}</code>\n\nللاستفسار تواصل مع {v2}.', [f"{html(order['order_code'])}",f"{html(order['payment_reference'] or '—')}",f'{html(ADMIN_CONTACT)}']),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            log.exception("failed to notify rejected review order=%s", order["order_code"])
        await query.edit_message_text(render_bot_text('bot.handlers.stability.py:46:message','❌ تم رفض العملية نهائيًا وإشعار المستخدم.', []), reply_markup=admin_kb(user.id))
        return
    DBX.update_order(
        order_id,
        status="PAYMENT_VERIFIED",
        paid_at=order["paid_at"] or now_str(),
        payer_match_source="ADMIN_MANUAL_REVIEW",
        admin_reviewed_by=user.id,
        admin_reviewed_at=now_str(),
        review_reason=None,
        rejection_reason=None,
        error_message=None,
    )
    DBX.audit(user.id, actor_name(user), "APPROVE_PAYMENT_REVIEW", "order", order["order_code"])
    try:
        await context.bot.send_message(
            order["telegram_user_id"],
            render_bot_text('bot.handlers.stability.py:61:message','<b>✅ وافقت الإدارة على عملية الدفع</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 الكمية: <b>{v1} USDT</b>\n\nاختر السحب عبر عنوان المحفظة للمتابعة.', [f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=delivery_kb(order_id),
        )
    except TelegramError:
        log.exception("failed to notify approved review order=%s", order["order_code"])
    await query.edit_message_text(render_bot_text('bot.handlers.stability.py:72:message','✅ تم قبول الدفعة وإشعار المستخدم بالمتابعة.', []), reply_markup=admin_kb(user.id))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    register_bot_user(user)
    data = query.data or ""
    is_admin = DBX.is_admin(user.id)

    if data == "admin_panel":
        set_state(context, None)

    if data == "check_subscription":
        await query.answer("جاري التحقق...")
        missing = await missing_required_channels(context, user.id)
        if missing:
            await show_subscription_gate(query, context, missing)
        else:
            await show_main(query, context)
        return

    if not is_admin and data not in {"main_menu"}:
        missing = await missing_required_channels(context, user.id)
        if missing:
            await query.answer("اشترك في القنوات أولاً", show_alert=True)
            await show_subscription_gate(query, context, missing)
            return

    # منع الوصول اليدوي لمسار Binance ID حتى لو أرسل المستخدم callback مزوراً.
    if data.startswith("delivery_binance:"):
        await query.answer("التحويل عبر Binance ID غير متاح حاليًا", show_alert=True)
        return

    if data.startswith("delivery_chain:") or data.startswith("net:"):
        try:
            order_id = int(data.split(":", 2)[1])
        except Exception:
            await query.answer("طلب غير صالح", show_alert=True)
            return
        order = DBX.get_order(order_id)
        if not order or int(order["telegram_user_id"]) != int(user.id):
            await query.answer("الطلب غير متاح", show_alert=True)
            return
        if order["status"] not in {"PAYMENT_VERIFIED", "WAITING_DESTINATION", "FAILED"}:
            await query.answer("يجب التحقق من الدفع أولاً", show_alert=True)
            return

    if data.startswith("adm_payment_approve:") or data.startswith("adm_payment_reject:"):
        await query.answer()
        await _handle_admin_payment_review(query, context, data)
        return

    new_admin_callbacks = {
        "adm_broadcast", "adm_required_channels", "adm_required_channel_add",
        "adm_notification_channel", "adm_notification_channel_set",
        "adm_notification_channel_clear", "adm_settings_overview",
    }
    is_new_admin = data in new_admin_callbacks or data.startswith("adm_required_channel_")
    if is_new_admin:
        if not is_admin:
            await query.answer("للأدمن فقط", show_alert=True)
            return
        permission = callback_permission(data)
        if permission and not DBX.has_permission(user.id, permission):
            await query.answer("لا تملك هذه الصلاحية", show_alert=True)
            return
        await query.answer()

        if data == "adm_broadcast":
            set_state(context, "await_broadcast_message")
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:143:message','<b>📢 إذاعة للكل</b>\n\nأرسل الآن الرسالة التي تريد إذاعتها. يمكن أن تكون نصًا أو صورة أو فيديو أو ملفًا أو صوتًا أو ملصقًا.\n\nسيقوم البوت بنسخ الرسالة إلى جميع المستخدمين المسجلين.', []),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.stability.py:148:button','❌ إلغاء', []), "admin_panel", "danger")]]),
            )
            return

        if data == "adm_required_channels":
            set_state(context, None)
            channels = list_required_channels(False)
            body = (
                "<b>✅ إدارة الاشتراك الإجباري</b>\n\nلا توجد قنوات مضافة."
                if not channels
                else "<b>✅ إدارة الاشتراك الإجباري</b>\n\nاضغط على قناة لحذفها أو أضف قناة جديدة."
            )
            await query.edit_message_text(render_bot_text('bot.handlers.stability.py:160:message','{v0}', [str(body)]), parse_mode=ParseMode.HTML, reply_markup=required_channels_admin_kb())
            return

        if data == "adm_required_channel_add":
            set_state(context, "await_required_channel")
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:165:message','<b>➕ إضافة قناة اشتراك إجباري</b>\n\nأرسل معرف القناة أو ID القناة.\n\nللقناة الخاصة استخدم الصيغة:\n<code>-1001234567890 | https://t.me/+InviteLink</code>\n\nيجب أن يكون البوت مشرفًا في القناة حتى يستطيع التحقق من العضوية.', []),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.stability.py:172:button','↩️ رجوع', []), "adm_required_channels")]]),
            )
            return

        if data.startswith("adm_required_channel_view:"):
            channel_id = int(data.split(":", 1)[1])
            row = get_required_channel(channel_id)
            if not row:
                await query.edit_message_text(render_bot_text('bot.handlers.stability.py:180:message','❌ القناة غير موجودة.', []), reply_markup=required_channels_admin_kb())
                return
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:182:message','<b>بيانات قناة الاشتراك</b>\n\n📺 الاسم: {v0}\n🆔 المعرف: <code>{v1}</code>\n🔗 المستخدم: @{v2}\n📌 الحالة: {v3}', [f"{html(row['title'] or '—')}",f"{html(row['chat_id'])}",f"{html(row['username'] or '—')}",f"{('مفعلة' if row['active'] else 'متوقفة')}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(render_bot_button('bot.handlers.stability.py:191:button','🗑️ حذف القناة', []), f"adm_required_channel_delete:{channel_id}", "danger")],
                        [btn(render_bot_button('bot.handlers.stability.py:192:button','↩️ رجوع', []), "adm_required_channels")],
                    ]
                ),
            )
            return

        if data.startswith("adm_required_channel_delete:"):
            channel_id = int(data.split(":", 1)[1])
            row = get_required_channel(channel_id)
            if not row:
                await query.edit_message_text(render_bot_text('bot.handlers.stability.py:202:message','❌ القناة غير موجودة.', []), reply_markup=required_channels_admin_kb())
                return
            delete_required_channel(channel_id)
            DBX.audit(user.id, actor_name(user), "DELETE_REQUIRED_CHANNEL", "channel", row["chat_id"])
            await query.edit_message_text(render_bot_text('bot.handlers.stability.py:206:message','🗑️ تم حذف قناة الاشتراك.', []), reply_markup=required_channels_admin_kb())
            return

        if data == "adm_notification_channel":
            set_state(context, None)
            current = DBX.get_setting("notification_channel_id")
            title = DBX.get_setting("notification_channel_title") or "—"
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:213:message','<b>📺 قناة إشعارات العمليات</b>\n\nالقناة الحالية: <b>{v0}</b>\nالمعرف: <code>{v1}</code>\n\nلن يتم نشر أرقام الهواتف أو Telegram ID أو العناوين كاملة.', [f'{html(title)}',f"{html(current or 'غير محدد')}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=notification_channel_kb(),
            )
            return

        if data == "adm_notification_channel_set":
            set_state(context, "await_notification_channel")
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:225:message','أرسل Channel ID أو @username لقناة الإشعارات بعد إضافة البوت مشرفًا فيها.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.stability.py:227:button','↩️ رجوع', []), "adm_notification_channel")]]),
            )
            return

        if data == "adm_notification_channel_clear":
            if callable(globals().get("set_users_channel")):
                set_users_channel("")
            else:
                DBX.set_setting("notification_channel_id", "")
                DBX.set_setting("notification_channel_title", "")
            DBX.set_setting("notification_channel_username", "")
            DBX.audit(user.id, actor_name(user), "CLEAR_NOTIFICATION_CHANNEL", "settings", "notification_channel")
            await query.edit_message_text(render_bot_text('bot.handlers.stability.py:239:message','🗑️ تم حذف قناة الإشعارات.', []), reply_markup=notification_channel_kb())
            return

        if data == "adm_settings_overview":
            set_state(context, None)
            await query.edit_message_text(
                render_bot_text('bot.handlers.stability.py:244:message','<b>⚙️ الإعدادات</b>\n\n💲 سعر 1 USDT: <b>{v0} ريال</b>\n💵 الحد الأدنى: <b>{v1} USDT</b>\n💸 رسوم النظام: <b>{v2} USDT</b>\n🛒 البيع: <b>{v3}</b>\n🚀 الصرف التلقائي: <b>{v4}</b>\n✅ قنوات الاشتراك: <b>{v5}</b>\n📺 قناة الإشعارات: <b>{v6}</b>', [f'{html(format_display_amount(DBX.get_rate()))}',f'{html(format_display_amount(MIN_USDT_ORDER))}',f'{html(format_display_amount(SYSTEM_NETWORK_FEE_USDT))}',f"{('مفعل' if DBX.sales_enabled() else 'متوقف')}",f"{('مفعل' if DBX.auto_payout_enabled() else 'متوقف')}",f'{len(list_required_channels(True))}',f"{html(DBX.get_setting('notification_channel_title') or 'غير محددة')}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(render_bot_button('bot.handlers.stability.py:256:button','💲 تعديل السعر', []), "adm_set_rate", "primary")],
                        [btn(render_bot_button('bot.handlers.stability.py:257:button','🟢/🔴 حالة البيع', []), "adm_toggle_sales"), btn(render_bot_button('bot.handlers.stability.py:257:button','🚀 الصرف', []), "adm_toggle_payout")],
                        [btn(render_bot_button('bot.handlers.stability.py:258:button','🏠 لوحة الأدمن', []), "admin_panel")],
                    ]
                ),
            )
            return

    await _legacy_on_callback(update, context)
    if data.startswith("adm_complete:"):
        try:
            order_id = int(data.split(":", 1)[1])
            order = DBX.get_order(order_id)
            if order and order["status"] == "COMPLETED":
                await publish_success_to_channel(context, order)
        except Exception:
            log.exception("manual completed channel notification failed")


async def _handle_new_admin_text_states(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str) -> bool:
    user = update.effective_user
    if not DBX.is_admin(user.id):
        return False
    permission = state_permission(state)
    if permission and not DBX.has_permission(user.id, permission):
        set_state(context, None)
        await update.effective_message.reply_text(render_bot_text('bot.handlers.stability.py:282:message','⛔ لا تملك هذه الصلاحية.', []), reply_markup=admin_kb(user.id))
        return True

    if state == "await_required_channel":
        text_value = (update.effective_message.text or "").strip()
        if not text_value:
            await update.effective_message.reply_text(render_bot_text('bot.handlers.stability.py:288:message','أرسل معرف القناة كنص.', []))
            return True
        try:
            identifier, provided_link = _parse_channel_input(text_value)
            chat = await context.bot.get_chat(identifier)
            chat_id = str(chat.id)
            username = getattr(chat, "username", None) or ""
            title = getattr(chat, "title", None) or username or chat_id
            invite_link = provided_link or getattr(chat, "invite_link", None) or ""
            if not invite_link and username:
                invite_link = f"https://t.me/{username}"
            if not invite_link:
                raise RuntimeError(
                    "القناة خاصة ولم يتم العثور على رابط انضمام. أرسل ID القناة ثم | ثم رابط الدعوة."
                )
            bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            bot_status = _member_status_value(bot_member)
            if bot_status not in {"administrator", "creator", "owner"}:
                raise RuntimeError("يجب إضافة البوت مشرفًا في القناة قبل حفظها")
            save_required_channel(chat_id, username, title, invite_link, user.id)
            DBX.audit(user.id, actor_name(user), "ADD_REQUIRED_CHANNEL", "channel", chat_id, title)
            set_state(context, None)
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.stability.py:310:message','✅ تم حفظ قناة الاشتراك: <b>{v0}</b>', [f'{html(title)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=required_channels_admin_kb(),
            )
        except Exception as exc:
            log.exception("Failed to add required channel admin=%s input=%s", user.id, text_value)
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.stability.py:317:message','❌ تعذر إضافة القناة:\n<code>{v0}</code>\n\nأعد المحاولة؛ محاولات الأدمن غير محدودة.', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
            )
        return True

    if state == "await_notification_channel":
        text_value = (update.effective_message.text or "").strip()
        if not text_value:
            await update.effective_message.reply_text(render_bot_text('bot.handlers.stability.py:326:message','أرسل Channel ID أو @username كنص.', []))
            return True
        try:
            identifier, _ = _parse_channel_input(text_value)
            chat = await context.bot.get_chat(identifier)
            bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            bot_status = _member_status_value(bot_member)
            if bot_status not in {"administrator", "creator", "owner"}:
                raise RuntimeError("يجب أن يكون البوت مشرفًا في قناة الإشعارات")
            await context.bot.send_message(
                chat_id=chat.id,
                text=render_bot_text('bot.handlers.stability.py:335:message','✅ تم ربط هذه القناة بإشعارات العمليات الناجحة.', []),
            )
            title = getattr(chat, "title", None) or str(chat.id)
            if callable(globals().get("set_users_channel")):
                set_users_channel(str(chat.id), title)
            else:
                DBX.set_setting("notification_channel_id", str(chat.id))
                DBX.set_setting("notification_channel_title", title)
            DBX.set_setting("notification_channel_username", getattr(chat, "username", None) or "")
            DBX.audit(user.id, actor_name(user), "SET_NOTIFICATION_CHANNEL", "settings", chat.id)
            set_state(context, None)
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.stability.py:348:message','✅ تم تعيين قناة الإشعارات: <b>{v0}</b>', [f"{html(getattr(chat, 'title', None) or chat.id)}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=notification_channel_kb(),
            )
        except Exception as exc:
            log.exception("Failed to set notification channel admin=%s input=%s", user.id, text_value)
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.stability.py:355:message','❌ تعذر تعيين القناة:\n<code>{v0}</code>\n\nأعد المحاولة؛ محاولات الأدمن غير محدودة.', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
            )
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_bot_user(user)
    state, _ = current_state(context)
    if state in {"await_required_channel", "await_notification_channel"}:
        if await _handle_new_admin_text_states(update, context, state):
            return
    if not DBX.is_admin(user.id):
        if not await ensure_required_subscription(update, context, user.id):
            return
    await _legacy_on_text(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_bot_user(user)
    state, _ = current_state(context)
    if state == "await_broadcast_message":
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "broadcast_send"):
            set_state(context, None)
            await update.effective_message.reply_text(render_bot_text('bot.handlers.stability.py:384:message','⛔ لا تملك صلاحية الإذاعة.', []))
            return
        await perform_broadcast(update, context)
        return
    if update.effective_message.text is not None:
        await on_text(update, context)
        return
    if not DBX.is_admin(user.id):
        if not await ensure_required_subscription(update, context, user.id):
            return
    await update.effective_message.reply_text(render_bot_text('bot.handlers.stability.py:394:message','استخدم الأزرار أو أرسل البيانات المطلوبة كنص.', []))





_export_globals(globals())

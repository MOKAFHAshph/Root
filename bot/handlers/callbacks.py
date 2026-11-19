"""Base Telegram commands and callback-query handlers.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# Telegram
# ============================================================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, None)
    await show_main(update, context)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_query_answer(query)
    if not throttle_allowed(context, query.from_user.id):
        return
    data = query.data or ""
    user = query.from_user
    is_admin = DBX.is_admin(user.id)

    if data == "main_menu":
        set_state(context, None)
        await show_main(query, context)
        return

    if data == "help":
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:34:message','{v0}', [str(help_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=help_keyboard(),
        )
        return

    if data == "help_network_address":
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:42:message','<b>🎥 كيف أستخرج عنوان الشبكة؟</b>\n\nسيتم إضافة رابط الفيديو التعليمي من إعداد NETWORK_ADDRESS_VIDEO_URL. حتى ذلك الوقت افتح محفظتك، اختر USDT ثم استقبال، وحدد الشبكة المطابقة تمامًا قبل نسخ العنوان.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=help_keyboard(),
        )
        return

    if data == "help_usage":
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:52:message','{v0}', [str(help_text())]),
            parse_mode=ParseMode.HTML,
            reply_markup=help_keyboard(),
        )
        return

    if data == "my_orders":
        rows = DBX.list_user_orders(user.id, 10)
        body = (
            "<b>لا توجد طلبات حتى الآن.</b>"
            if not rows
            else "\n\n".join(
                ["<b>آخر طلباتك</b>"]
                + [
                    f"• <code>{html(row['order_code'])}</code> | {status_label(row['status'])}\n"
                    f"  {html(row['crypto_amount'])} USDT | {html(row['expected_yer_amount'])} ريال"
                    for row in rows
                ]
            )
        )
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:73:message','{v0}', [str(body)]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:76:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data == "buy_usdt":
        customer = DBX.get_customer_by_telegram(user.id)
        if not customer or customer["status"] != "ACTIVE":
            await query.answer("غير مسموح", show_alert=True)
            return
        if not DBX.sales_enabled():
            await query.answer("البيع متوقف حاليًا", show_alert=True)
            return
        set_state(context, "await_buy_amount")
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:89:message','<b>أرسل كمية USDT المطلوبة</b>\n\nمثال: <code>10</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:92:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data.startswith("cancel_order:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order or (int(order["telegram_user_id"]) != int(user.id) and not is_admin):
            await query.answer("غير مسموح", show_alert=True)
            return
        DBX.update_order(order_id, status="CANCELLED")
        DBX.audit(user.id, actor_name(user), "CANCEL_ORDER", "order", order["order_code"])
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:104:message','🚫 تم إلغاء الطلب.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:106:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data.startswith("pay_ref:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order or int(order["telegram_user_id"]) != int(user.id):
            await query.answer("الطلب غير متاح", show_alert=True)
            return
        set_state(context, "await_payment_ref", order_id=order_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:117:message','<b>أرسل رقم العملية الآن</b>\n\n🧾 الطلب: <code>{v0}</code>\n💰 المطلوب: <b>{v1} ريال</b>', [f"{html(order['order_code'])}",f"{html(order['expected_yer_amount'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:122:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data.startswith("delivery_binance:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order or int(order["telegram_user_id"]) != int(user.id):
            await query.answer("الطلب غير متاح", show_alert=True)
            return
        set_state(context, "await_binance_id", order_id=order_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:133:message','<b>أرسل Binance ID</b>\n\nأرسل الـ ID الرقمي فقط.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:136:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data.startswith("delivery_chain:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order or int(order["telegram_user_id"]) != int(user.id):
            await query.answer("الطلب غير متاح", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:146:message','<b>اختر شبكة الاستلام</b>\n\n⚠️ يجب أن تطابق الشبكة في المحفظة المستقبلة.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=network_kb(order_id),
        )
        return

    if data.startswith("net:"):
        _, order_text, network = data.split(":", 2)
        order_id = int(order_text)
        order = DBX.get_order(order_id)
        if not order or int(order["telegram_user_id"]) != int(user.id):
            await query.answer("الطلب غير متاح", show_alert=True)
            return
        set_state(context, "await_chain_address", order_id=order_id, network_code=network)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:161:message','<b>أرسل عنوان الاستلام</b>\n\n🌐 الشبكة: <b>{v0}</b>\n\n⚠️ راجع العنوان جيدًا قبل الإرسال.', [f'{html(NETWORK_LOOKUP.get(network, network))}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:166:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    # ---------------- Admin ----------------
    if data == "admin_panel":
        if not is_admin:
            await query.answer("للأدمن فقط", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:175:message','<b>لوحة الأدمن</b>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb(user.id),
        )
        return

    if not is_admin:
        await query.answer("غير مسموح", show_alert=True)
        return

    required = callback_permission(data)
    if required and not DBX.has_permission(user.id, required):
        await query.answer("لا تملك هذه الصلاحية", show_alert=True)
        DBX.security(
            user.id,
            user.username,
            "admin_permission_denied",
            "MEDIUM",
            f"callback={data} permission={required}",
        )
        return

    if data == "adm_set_rate":
        set_state(context, "await_rate")
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:200:message','أرسل السعر الجديد للـ 1 USDT.\n\nالسعر الحالي: <b>{v0}</b>', [f'{html(format_display_amount(DBX.get_rate()))}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:203:button','🏠 رجوع', []), "admin_panel")]]),
        )
        return

    if data == "adm_toggle_sales":
        DBX.set_sales_enabled(not DBX.sales_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_SALES", "settings", "sales_enabled", str(DBX.sales_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:210:message','✅ تم تحديث حالة البيع.', []), reply_markup=admin_kb(user.id))
        return

    if data == "adm_toggle_payout":
        DBX.set_auto_payout_enabled(not DBX.auto_payout_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_AUTO_PAYOUT", "settings", "auto_payout_enabled", str(DBX.auto_payout_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:216:message','✅ تم تحديث حالة الصرف التلقائي.', []), reply_markup=admin_kb(user.id))
        return

    if data == "adm_list_customers" or data.startswith("cust_page:"):
        page = int(data.split(":", 1)[1]) if data.startswith("cust_page:") else 0
        rows = DBX.list_customers(500)
        if not rows:
            await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:223:message','<b>لا يوجد عملاء.</b>', []), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:225:message','<b>العملاء</b>\n\nاضغط على العميل لعرض بياناته والتحكم به.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=customers_list_kb(rows, page),
        )
        return

    if data == "adm_search_customer":
        set_state(context, "await_search_customer")
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:234:message','أرسل الاسم أو الرقم أو ID العميل.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:236:button','🏠 رجوع', []), "admin_panel")]]),
        )
        return

    if data.startswith("cust_view:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:246:message','{v0}', [str(customer_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_actions_kb(row, user.id),
        )
        return

    if data == "adm_add_customer":
        context.user_data["customer_draft"] = {
            "full_name": "",
            "phone_primary": "",
            "phone_alt": "",
            "telegram_id": "",
            "notes": "",
        }
        set_state(context, None)
        await show_customer_draft(query, context)
        return

    if data.startswith("cust_add_field:"):
        field = data.split(":", 1)[1]
        if field not in CUSTOMER_FIELD_LABELS:
            await query.answer("حقل غير صالح", show_alert=True)
            return
        prompts = {
            "full_name": "أرسل اسم العميل الكامل.",
            "phone_primary": "أرسل رقم الهاتف الأساسي.",
            "phone_alt": "أرسل الرقم البديل، أو 0 إذا لا يوجد.",
            "telegram_id": "أرسل Telegram ID الخاص بالعميل.",
            "notes": "أرسل الملاحظات، أو 0 إذا لا توجد.",
        }
        set_state(context, "await_customer_field", field=field)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:278:message','<b>{v0}</b>\n\n{v1}\n\nالأدمن غير مقيد بعدد محاولات.', [f'{html(CUSTOMER_FIELD_LABELS[field])}',f'{prompts[field]}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn(render_bot_button('bot.handlers.callbacks.py:283:button','↩️ رجوع للحقول', []), "cust_add_back", "primary")],
                [btn(render_bot_button('bot.handlers.callbacks.py:284:button','❌ إلغاء', []), "cust_add_cancel", "danger")],
            ]),
        )
        return

    if data == "cust_add_back":
        set_state(context, None)
        await show_customer_draft(query, context)
        return

    if data == "cust_add_cancel":
        set_state(context, None)
        context.user_data.pop("customer_draft", None)
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:297:message','❌ تم إلغاء إضافة العميل.', []), reply_markup=admin_kb(user.id))
        return

    if data == "cust_add_review":
        draft = customer_draft(context)
        errors = validate_customer_draft(draft)
        if errors:
            await show_customer_draft(
                query,
                context,
                "<b>❌ صحح الأخطاء:</b>\n• " + "\n• ".join(html(item) for item in errors),
            )
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:310:message','{v0}\n\n⚠️ تأكد من الرقم الأساسي والبديل وTelegram ID.', [str(customer_draft_text(draft, 'مراجعة نهائية'))]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_confirm_kb(),
        )
        return

    if data == "cust_add_save":
        draft = customer_draft(context)
        errors = validate_customer_draft(draft)
        if errors:
            await show_customer_draft(
                query,
                context,
                "<b>❌ تعذر الحفظ:</b>\n• " + "\n• ".join(html(item) for item in errors),
            )
            return
        try:
            customer_id = DBX.create_customer(
                str(draft["full_name"]),
                str(draft["phone_primary"]),
                str(draft.get("phone_alt") or ""),
                int(str(draft["telegram_id"])),
                "",
                str(draft.get("notes") or ""),
                user.id,
            )
        except Exception as exc:
            log.exception("Failed to create customer admin=%s telegram_id=%s", user.id, draft.get("telegram_id"))
            await query.edit_message_text(
                render_bot_text('bot.handlers.callbacks.py:340:message','❌ تعذر إضافة العميل: <code>{v0}</code>', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=customer_draft_kb(draft),
            )
            return
        row = DBX.get_customer(customer_id)
        DBX.audit(user.id, actor_name(user), "ADD_CUSTOMER", "customer", customer_id, row["full_name"])
        sent, _ = await send_customer_welcome(context, row)
        context.user_data.pop("customer_draft", None)
        set_state(context, None)
        notice = "✅ تم إرسال رسالة الترحيب." if sent else "⚠️ تعذر إرسال الترحيب؛ يجب أن يبدأ العميل البوت أولًا."
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:352:message','✅ <b>تمت إضافة العميل</b>\n\n{v0}\n\n{v1}', [f'{customer_detail_text(row)}',f'{notice}']),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_actions_kb(row, user.id),
        )
        return

    if data.startswith("cust_edit_menu:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:365:message','{v0}\n\n<b>اختر الحقل:</b>', [str(customer_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_edit_kb(customer_id),
        )
        return

    if data.startswith("cust_edit_field:"):
        _, customer_text, field = data.split(":", 2)
        customer_id = int(customer_text)
        if field not in CUSTOMER_FIELD_LABELS or not DBX.get_customer(customer_id):
            await query.answer("طلب غير صالح", show_alert=True)
            return
        prompts = {
            "full_name": "أرسل الاسم الجديد.",
            "phone_primary": "أرسل الرقم الأساسي الجديد.",
            "phone_alt": "أرسل الرقم البديل الجديد، أو 0 لإزالته.",
            "telegram_id": "أرسل Telegram ID الجديد.",
            "notes": "أرسل الملاحظات الجديدة، أو 0 لمسحها.",
        }
        set_state(context, "await_edit_customer_field", customer_id=customer_id, field=field)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:386:message','<b>{v0}</b>\n\n{v1}\n\nالأدمن غير مقيد بعدد محاولات.', [f'{html(CUSTOMER_FIELD_LABELS[field])}',f'{prompts[field]}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:390:button','↩️ رجوع', []), f"cust_edit_menu:{customer_id}")]]),
        )
        return

    if data.startswith("cust_suspend:") or data.startswith("cust_activate:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        status = "SUSPENDED" if data.startswith("cust_suspend:") else "ACTIVE"
        DBX.update_customer(customer_id, status=status)
        DBX.audit(user.id, actor_name(user), "SET_CUSTOMER_STATUS", "customer", customer_id, status)
        row = DBX.get_customer(customer_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:404:message','{v0}', [str(customer_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_actions_kb(row, user.id),
        )
        return

    if data.startswith("cust_delete_confirm:"):
        customer_id = int(data.split(":", 1)[1])
        summary = DBX.customer_deletion_summary(customer_id)
        if not summary:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        row = summary["customer"]
        total_balance = summary["available_balance"] + summary["locked_balance"]
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:419:message','<b>⚠️ تحذير — بيانات المستخدم</b>\n\n👤 الاسم: <b>{v0}</b>\n🆔 المعرف: <code>{v1}</code>\n💰 الرصيد الحالي: <b>{v2} USDT</b>\n├ المتاح: {v3} USDT\n└ المحجوز: {v4} USDT\n📊 عدد العمليات: <b>{v5}</b>\n📅 تاريخ التسجيل: <code>{v6}</code>\n\nاختر تصفية الرصيد أولًا، أو انتقل إلى تأكيد الحذف النهائي بكل البيانات.', [f"{html(row['full_name'])}",f"{html(row['telegram_id'] or 'غير مربوط')}",f'{html(format_display_amount(total_balance))}',f"{html(format_display_amount(summary['available_balance']))}",f"{html(format_display_amount(summary['locked_balance']))}",f"{int(summary['operation_count'])}",f"{html(row['created_at'] or '—')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn(render_bot_button('bot.handlers.callbacks.py:431:button','💰 تصفية الرصيد أولاً', []), "adm_wallet_adjust", "primary")],
                [btn(render_bot_button('bot.handlers.callbacks.py:432:button','🗑 حذف نهائي مع كل البيانات', []), f"cust_delete_force_confirm:{customer_id}", "danger")],
                [btn(render_bot_button('bot.handlers.callbacks.py:433:button','↩️ إلغاء', []), f"cust_view:{customer_id}")],
            ]),
        )
        return

    if data.startswith("cust_delete_force_confirm:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:444:message','<b>⛔ هل أنت متأكد؟</b>\n\nسيتم حذف هذا المستخدم وجميع بياناته ورصيده نهائيًا.\nلا يمكن التراجع عن هذا الإجراء.\n\n👤 <b>{v0}</b>\n🆔 <code>{v1}</code>', [f"{html(row['full_name'])}",f"{html(row['telegram_id'] or 'غير مربوط')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn(render_bot_button('bot.handlers.callbacks.py:452:button','⛔ نعم، حذف نهائي لا رجعة فيه', []), f"cust_delete_do:{customer_id}", "danger")],
                [btn(render_bot_button('bot.handlers.callbacks.py:453:button','↩️ تراجع', []), f"cust_delete_confirm:{customer_id}")],
            ]),
        )
        return

    if data.startswith("cust_delete_do:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        deleted = DBX.delete_customer(customer_id)
        DBX.audit(
            user.id,
            actor_name(user),
            "DELETE_CUSTOMER_PERMANENT",
            "system",
            None,
            f"purged_records={sum(deleted.values())}",
        )
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:473:message','🗑️ تم حذف المستخدم وجميع بياناته المرتبطة نهائيًا.\nإذا عاد إلى البوت فسيُعامل كمستخدم جديد.', []),
            reply_markup=admin_kb(user.id),
        )
        return

    if data.startswith("cust_link:"):
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        set_state(context, "await_link_customer_id", customer_id=customer_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:487:message','<b>ربط Telegram ID</b>\n\nالعميل: <b>{v0}</b>\nأرسل Telegram ID الجديد.', [f"{html(row['full_name'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:491:button','↩️ رجوع', []), f"cust_view:{customer_id}")]]),
        )
        return

    # إدارة الأدمن: للأدمن الرئيسي فقط
    if data == "adm_admins":
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        context.user_data.pop("admin_draft", None)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:501:message','<b>إدارة الأدمن</b>\n\nاختر أدمنًا أو أضف أدمنًا جديدًا.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_list_kb(),
        )
        return

    if data == "adm_admin_add":
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        context.user_data["admin_draft"] = {
            "telegram_id": None,
            "display_name": "",
            "username": "",
            "permissions": set(),
        }
        set_state(context, "await_admin_id")
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:519:message','<b>إضافة أدمن</b>\n\nأرسل Telegram ID الخاص به أرقامًا فقط.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:522:button','❌ إلغاء', []), "adm_admins", "danger")]]),
        )
        return

    if data.startswith("adm_admin_view:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        row = DBX.get_admin(target_id)
        if not row:
            await query.answer("الأدمن غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:535:message','{v0}', [str(admin_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_detail_kb(row),
        )
        return

    if data.startswith("adm_admin_edit:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        row = DBX.get_admin(target_id)
        if not row:
            await query.answer("الأدمن غير موجود", show_alert=True)
            return
        permissions = set(DBX.get_admin_permissions(target_id))
        context.user_data["admin_draft"] = {
            "telegram_id": target_id,
            "display_name": row["display_name"] or "",
            "username": row["username"] or "",
            "permissions": permissions,
        }
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:558:message','<b>تعديل الصلاحيات</b>\n\n🆔 <code>{v0}</code>', [f'{target_id}']),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_permissions_kb(permissions, "edit", target_id),
        )
        return

    if data.startswith("adm_admin_perm:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        _, mode, permission, target_text = data.split(":", 3)
        if permission not in ADMIN_PERMISSION_LABELS:
            await query.answer("صلاحية غير صحيحة", show_alert=True)
            return
        draft = context.user_data.get("admin_draft")
        if not draft:
            await query.answer("انتهت الجلسة", show_alert=True)
            return
        permissions = set(draft.get("permissions") or set())
        if permission in permissions:
            permissions.remove(permission)
        else:
            permissions.add(permission)
        draft["permissions"] = permissions
        target_id = int(target_text) or None
        await query.edit_message_reply_markup(
            reply_markup=admin_permissions_kb(permissions, mode, target_id)
        )
        return

    if data.startswith("adm_admin_all:") or data.startswith("adm_admin_none:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        _, mode, target_text = data.split(":", 2)
        draft = context.user_data.get("admin_draft")
        if not draft:
            await query.answer("انتهت الجلسة", show_alert=True)
            return
        permissions = set(ALL_ADMIN_PERMISSIONS) if data.startswith("adm_admin_all:") else set()
        draft["permissions"] = permissions
        target_id = int(target_text) or None
        await query.edit_message_reply_markup(
            reply_markup=admin_permissions_kb(permissions, mode, target_id)
        )
        return

    if data.startswith("adm_admin_save:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        _, mode, target_text = data.split(":", 2)
        draft = context.user_data.get("admin_draft")
        if not draft:
            await query.answer("انتهت الجلسة", show_alert=True)
            return
        target_id = int(draft.get("telegram_id") or target_text or 0)
        permissions = set(draft.get("permissions") or set())
        if not target_id or target_id == int(ADMIN_ID):
            await query.answer("Telegram ID غير صالح", show_alert=True)
            return
        try:
            if mode == "add":
                DBX.create_admin(
                    target_id,
                    str(draft.get("display_name") or target_id),
                    str(draft.get("username") or ""),
                    permissions,
                    user.id,
                )
                action = "ADD_ADMIN"
            else:
                DBX.update_admin_permissions(target_id, permissions)
                action = "EDIT_ADMIN_PERMISSIONS"
        except sqlite3.IntegrityError:
            await query.answer("هذا الأدمن مضاف مسبقًا", show_alert=True)
            return
        DBX.audit(user.id, actor_name(user), action, "admin", target_id, ",".join(sorted(permissions)))
        row = DBX.get_admin(target_id)
        try:
            permission_text = "\n".join(
                f"• {ADMIN_PERMISSION_LABELS[key]}"
                for key in ALL_ADMIN_PERMISSIONS
                if key in permissions
            ) or "• لا توجد صلاحيات"
            await context.bot.send_message(
                target_id,
                render_bot_text('bot.handlers.callbacks.py:644:message','👋 أهلاً <b>{v0}</b>\n\nتمت إضافتك كأدمن في <b>{v1}</b>.\n\n<b>صلاحياتك:</b>\n{v2}', [f"{html(row['display_name'] or str(target_id))}",f'{html(APP_NAME)}',f'{permission_text}']),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.exception("failed to notify admin %s", target_id)
        context.user_data.pop("admin_draft", None)
        set_state(context, None)
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:655:message','✅ تم حفظ الأدمن والصلاحيات.', []), reply_markup=admin_list_kb())
        return

    if data.startswith("adm_admin_suspend:") or data.startswith("adm_admin_activate:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        row = DBX.get_admin(target_id)
        if not row:
            await query.answer("الأدمن غير موجود", show_alert=True)
            return
        status = "SUSPENDED" if data.startswith("adm_admin_suspend:") else "ACTIVE"
        DBX.set_admin_status(target_id, status)
        DBX.audit(user.id, actor_name(user), "SET_ADMIN_STATUS", "admin", target_id, status)
        row = DBX.get_admin(target_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:671:message','{v0}', [str(admin_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_detail_kb(row),
        )
        return

    if data.startswith("adm_admin_delete_confirm:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        row = DBX.get_admin(target_id)
        if not row:
            await query.answer("الأدمن غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:687:message','{v0}\n\n⚠️ هل تريد حذف هذا الأدمن؟', [str(admin_detail_text(row))]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn(render_bot_button('bot.handlers.callbacks.py:691:button','🗑️ نعم، حذف', []), f"adm_admin_delete_do:{target_id}", "danger")],
                [btn(render_bot_button('bot.handlers.callbacks.py:692:button','↩️ إلغاء', []), f"adm_admin_view:{target_id}")],
            ]),
        )
        return

    if data.startswith("adm_admin_delete_do:"):
        if int(user.id) != int(ADMIN_ID):
            await query.answer("للأدمن الرئيسي فقط", show_alert=True)
            return
        target_id = int(data.split(":", 1)[1])
        DBX.delete_admin(target_id)
        DBX.audit(user.id, actor_name(user), "DELETE_ADMIN", "admin", target_id)
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:704:message','🗑️ تم حذف الأدمن.', []), reply_markup=admin_list_kb())
        return

    if data == "adm_stats":
        stats = DBX.stats()
        body = (
            "<b>الإحصائيات</b>\n\n"
            f"👥 عدد العملاء: <b>{stats['total_customers']}</b>\n"
            f"🟢 النشطون: <b>{stats['active_customers']}</b>\n"
            f"⛔ الموقوفون: <b>{stats['stopped_customers']}</b>\n"
            f"✅ العمليات الناجحة: <b>{stats['successful_orders']}</b>\n"
            f"🚫 العمليات المرفوضة: <b>{stats['rejected_orders']}</b>\n"
            f"🪙 حجم USDT المباع: <b>{stats['total_sold']}</b>\n"
            f"💰 اليوم: <b>{stats['daily_sales']}</b> ريال\n"
            f"💰 الأسبوع: <b>{stats['weekly_sales']}</b> ريال\n"
            f"💰 الشهر: <b>{stats['monthly_sales']}</b> ريال"
        )
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:721:message','{v0}', [str(body)]), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
        return

    if data == "adm_recent_orders":
        rows = DBX.list_recent_orders(20)
        body = (
            "<b>لا توجد عمليات.</b>"
            if not rows
            else "\n\n".join(
                ["<b>آخر العمليات</b>"]
                + [
                    f"• <code>{html(row['order_code'])}</code> | {status_label(row['status'])}\n"
                    f"  👤 {html(row['full_name'] or '—')} | 💵 {html(row['crypto_amount'])} USDT | 📱 {html(row['payer_phone'] or '—')}"
                    for row in rows
                ]
            )
        )
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:738:message','{v0}', [str(body[:4000])]), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
        return

    if data == "adm_pending_orders":
        rows = DBX.list_orders_by_status((
            "WAITING_ADMIN_PAYOUT", "WAITING_PAYMENT", "PAYMENT_VERIFIED",
            "WAITING_DESTINATION", "PAYMENT_REVIEW", "FAILED",
            "READY_FOR_PAYOUT", "PROCESSING_PAYOUT", "WITHDRAW_SUBMITTED",
            "WITHDRAW_STATUS_UNKNOWN",
        ))
        body = (
            "<b>لا توجد طلبات معلقة.</b>"
            if not rows
            else "\n\n".join(
                ["<b>الطلبات المعلقة</b>"]
                + [
                    f"• <code>{html(row['order_code'])}</code> | {status_label(row['status'])}\n"
                    f"  👤 {html(row['full_name'] or '—')} | 💵 {html(row['crypto_amount'])} USDT"
                    for row in rows[:25]
                ]
            )
        )
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:760:message','{v0}', [str(body[:4000])]), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
        return

    if data == "adm_security_log":
        rows = DBX.list_security_logs(20)
        body = (
            "<b>لا يوجد سجل أمني.</b>"
            if not rows
            else "\n\n".join(
                ["<b>السجل الأمني</b>"]
                + [
                    f"• {html(row['event_type'])} | {html(row['severity'])}\n"
                    f"  👤 <code>{html(row['telegram_user_id'] or '—')}</code> | "
                    f"🧾 <code>{html(row['order_code'] or '—')}</code>\n"
                    f"  {html(row['details'])}"
                    for row in rows
                ]
            )
        )
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:779:message','{v0}', [str(body[:4000])]), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
        return

    if data == "adm_backup":
        path = create_backup_file()
        with path.open("rb") as file:
            await context.bot.send_document(user.id, document=file, filename=path.name, caption=render_bot_text('bot.handlers.callbacks.py:785:message','نسخة احتياطية JSON', []))
        DBX.audit(user.id, actor_name(user), "BACKUP_CREATE", "backup", path.name)
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:787:message','✅ تم إنشاء النسخة: <code>{v0}</code>', [f'{html(path.name)}']), parse_mode=ParseMode.HTML, reply_markup=admin_kb(user.id))
        return

    if data == "adm_export_csv":
        customers_path, orders_path = create_csv_exports()
        with customers_path.open("rb") as file:
            await context.bot.send_document(user.id, document=file, filename=customers_path.name, caption=render_bot_text('bot.handlers.callbacks.py:793:message','العملاء CSV', []))
        with orders_path.open("rb") as file:
            await context.bot.send_document(user.id, document=file, filename=orders_path.name, caption=render_bot_text('bot.handlers.callbacks.py:795:message','العمليات CSV', []))
        DBX.audit(user.id, actor_name(user), "EXPORT_CSV", "export", f"{customers_path.name},{orders_path.name}")
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:797:message','✅ تم تصدير الملفات.', []), reply_markup=admin_kb(user.id))
        return

    if data == "adm_import_backup":
        set_state(context, "await_import_backup")
        await query.edit_message_text(
            render_bot_text('bot.handlers.callbacks.py:802:message','أرسل اسم ملف النسخة الموجود داخل مجلد backups.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.callbacks.py:804:button','🏠 رجوع', []), "admin_panel")]]),
        )
        return

    if data.startswith("adm_payment_approve:") or data.startswith("adm_payment_reject:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order or order["status"] != "PAYMENT_REVIEW":
            await query.answer("العملية ليست بانتظار المراجعة", show_alert=True)
            return
        if data.startswith("adm_payment_reject:"):
            DBX.update_order(
                order_id,
                status="REJECTED",
                rejection_reason="ADMIN_REJECTED_MISSING_SENDER",
                error_message="Rejected by admin",
            )
            DBX.audit(user.id, actor_name(user), "REJECT_PAYMENT_REVIEW", "order", order["order_code"])
            try:
                await context.bot.send_message(
                    order["telegram_user_id"],
                    render_bot_text('bot.handlers.callbacks.py:823:message','❌ تم رفض العملية بعد المراجعة.\n\n🧾 <code>{v0}</code>', [f"{html(order['order_code'])}"]),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.exception(
                    "Failed to notify user after payment review rejection order=%s user=%s",
                    order["order_code"],
                    order["telegram_user_id"],
                )
            await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:834:message','❌ تم رفض العملية.', []), reply_markup=admin_kb(user.id))
            return
        DBX.update_order(
            order_id,
            status="PAYMENT_VERIFIED",
            paid_at=now_str(),
            payer_match_source="ADMIN_MANUAL_REVIEW",
            rejection_reason=None,
            error_message=None,
        )
        DBX.audit(user.id, actor_name(user), "APPROVE_PAYMENT_REVIEW", "order", order["order_code"])
        try:
            await context.bot.send_message(
                order["telegram_user_id"],
                render_bot_text('bot.handlers.callbacks.py:846:message','✅ تم قبول الدفعة بعد مراجعة الأدمن.\n\n🧾 <code>{v0}</code>\nاختر طريقة الاستلام:', [f"{html(order['order_code'])}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=delivery_kb(order_id),
            )
        except Exception:
            log.exception(
                "Failed to notify user after payment review approval order=%s user=%s",
                order["order_code"],
                order["telegram_user_id"],
            )
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:859:message','✅ تم قبول الدفعة وإشعار العميل.', []), reply_markup=admin_kb(user.id))
        return

    if data.startswith("adm_complete:") or data.startswith("adm_fail:"):
        order_id = int(data.split(":", 1)[1])
        order = DBX.get_order(order_id)
        if not order:
            await query.answer("الطلب غير موجود", show_alert=True)
            return
        if data.startswith("adm_complete:"):
            DBX.update_order(order_id, status="COMPLETED", completed_at=now_str())
            action = "MANUAL_COMPLETE"
            message = "✅ تم تعليم الطلب كمكتمل."
        else:
            DBX.update_order(order_id, status="FAILED", error_message="MANUAL_FAIL")
            action = "MANUAL_FAIL"
            message = "❌ تم تعليم الطلب كفاشل."
        DBX.audit(user.id, actor_name(user), action, "order", order["order_code"])
        await query.edit_message_text(render_bot_text('bot.handlers.callbacks.py:877:message','{v0}', [str(message)]), reply_markup=admin_kb(user.id))
        return




_export_globals(globals())

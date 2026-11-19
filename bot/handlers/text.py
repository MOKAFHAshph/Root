"""Base text-state handlers.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not throttle_allowed(context, update.effective_user.id):
        return
    state, payload = current_state(context)
    text_value = (update.message.text or "").strip()
    user = update.effective_user

    # ---------------- Customer states: three attempts ----------------
    if state == "await_buy_amount":
        try:
            amount = parse_decimal(text_value)
            if amount < MIN_USDT_ORDER or amount > MAX_USDT_ORDER:
                raise InvalidOperation
        except Exception:
            attempts, remaining = failed_attempt(context)
            if attempts >= MAX_INPUT_ATTEMPTS:
                set_state(context, None)
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:26:message','❌ تم إلغاء الإدخال بعد {v0} محاولات.', [f'{MAX_INPUT_ATTEMPTS}']),
                    reply_markup=user_main_kb(False),
                )
            else:
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:31:message','❌ أدخل كمية بين {v0} و{v1} USDT.\nالمحاولة {v2}/{v3} — المتبقي {v4}.', [f'{format_display_amount(MIN_USDT_ORDER)}',f'{format_display_amount(MAX_USDT_ORDER)}',f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}'])
                )
            return
        await create_buy_order(update, context, text_value)
        return

    if state == "await_payment_ref":
        if not text_value.isdigit() or not (8 <= len(text_value) <= 20):
            attempts, remaining = failed_attempt(context)
            if attempts >= MAX_INPUT_ATTEMPTS:
                set_state(context, None)
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:44:message','❌ تم إلغاء رقم العملية بعد {v0} محاولات.', [f'{MAX_INPUT_ATTEMPTS}']),
                    reply_markup=user_main_kb(False),
                )
            else:
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:49:message','❌ رقم العملية يجب أن يكون من 8 إلى 20 رقمًا.\nالمحاولة {v0}/{v1} — المتبقي {v2}.', [f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}'])
                )
            return
        await verify_payment_reference(update, context, int(payload["order_id"]), text_value)
        return

    if state == "await_binance_id":
        if not text_value.isdigit() or not (5 <= len(text_value) <= 20):
            attempts, remaining = failed_attempt(context)
            if attempts >= MAX_INPUT_ATTEMPTS:
                set_state(context, None)
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:62:message','❌ تم إلغاء Binance ID بعد {v0} محاولات.', [f'{MAX_INPUT_ATTEMPTS}']),
                    reply_markup=user_main_kb(False),
                )
            else:
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:67:message','❌ أرسل Binance ID الرقمي.\nالمحاولة {v0}/{v1} — المتبقي {v2}.', [f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}'])
                )
            return
        set_state(context, None)
        await save_binance_id(update.message, context, int(payload["order_id"]), text_value)
        return

    if state == "await_chain_address":
        network = str(payload["network_code"])
        if not validate_address_for_network(network, text_value):
            attempts, remaining = failed_attempt(context)
            if attempts >= MAX_INPUT_ATTEMPTS:
                set_state(context, None)
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:82:message','❌ تم إلغاء العنوان بعد {v0} محاولات.', [f'{MAX_INPUT_ATTEMPTS}']),
                    reply_markup=user_main_kb(False),
                )
            else:
                await update.message.reply_text(
                    render_bot_text('bot.handlers.text.py:87:message','❌ العنوان لا يطابق شبكة {v0}.\nالمحاولة {v1}/{v2} — المتبقي {v3}.', [f'{html(NETWORK_LOOKUP.get(network, network))}',f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}']),
                    parse_mode=ParseMode.HTML,
                )
            return
        set_state(context, None)
        await submit_onchain_withdraw(
            update.message,
            context,
            int(payload["order_id"]),
            network,
            text_value,
        )
        return

    # ---------------- Admin states: unlimited attempts ----------------
    if not DBX.is_admin(user.id):
        customer = await require_customer(update, context)
        if customer is not None:
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:107:message','استخدم الأزرار من القائمة الرئيسية.', []))
        return

    required = state_permission(state)
    if required and not DBX.has_permission(user.id, required):
        set_state(context, None)
        await update.message.reply_text(render_bot_text('bot.handlers.text.py:113:message','⛔ لا تملك هذه الصلاحية.', []), reply_markup=admin_kb(user.id))
        return

    if state == "await_rate":
        try:
            rate = parse_decimal(text_value)
            if rate <= 0:
                raise InvalidOperation
        except Exception:
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:122:message','❌ السعر غير صالح. أعد الإرسال؛ لا يوجد حد للمحاولات للأدمن.', []))
            return
        DBX.set_setting("usdt_rate_yer", q2(rate))
        DBX.audit(user.id, actor_name(user), "SET_RATE", "settings", "usdt_rate_yer", q2(rate))
        set_state(context, None)
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:127:message','✅ تم تحديث السعر إلى <b>{v0}</b> ريال.', [f'{html(format_display_amount(rate))}']),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb(user.id),
        )
        return

    if state == "await_customer_field":
        field = str(payload.get("field") or "")
        draft = customer_draft(context)
        value = text_value
        error = ""

        if field == "full_name":
            if not (2 <= len(value) <= 80) or value.isdigit():
                error = "الاسم غير صالح."
            else:
                draft[field] = value
        elif field == "phone_primary":
            normalized = normalize_phone(value)
            if not (7 <= len(normalized) <= 15):
                error = "الرقم الأساسي غير صالح."
            elif DBX.get_customer_by_any_phone(normalized):
                error = "الرقم مستخدم لعميل آخر."
            elif phones_match(normalized, str(draft.get("phone_alt") or "")):
                error = "الرقم الأساسي يطابق البديل."
            else:
                draft[field] = normalized
        elif field == "phone_alt":
            if value in {"0", "-", "بدون", "لا يوجد"}:
                draft[field] = ""
            else:
                normalized = normalize_phone(value)
                if not (3 <= len(normalized) <= 20):
                    error = "الرقم البديل غير صالح."
                elif DBX.get_customer_by_any_phone(normalized):
                    error = "الرقم مستخدم لعميل آخر."
                elif phones_match(normalized, str(draft.get("phone_primary") or "")):
                    error = "الرقم البديل يطابق الأساسي."
                else:
                    draft[field] = normalized
        elif field == "telegram_id":
            if not value.isdigit() or not (5 <= len(value) <= 20):
                error = "Telegram ID غير صالح."
            elif DBX.get_customer_by_telegram(int(value)):
                error = "Telegram ID مربوط بعميل آخر."
            else:
                draft[field] = value
        elif field == "notes":
            draft[field] = "" if value in {"0", "-", "بدون", "لا توجد"} else value
            if len(str(draft[field])) > 500:
                error = "الملاحظات طويلة جدًا."
        else:
            error = "الحقل غير معروف."

        if error:
            await update.message.reply_text(
                render_bot_text('bot.handlers.text.py:183:message','❌ {v0}\nأعد الإرسال بعد التصحيح؛ الأدمن غير مقيد بعدد محاولات.', [f'{html(error)}']),
                parse_mode=ParseMode.HTML,
            )
            return
        set_state(context, None)
        await show_customer_draft(
            update.message,
            context,
            f"✅ تم حفظ {html(CUSTOMER_FIELD_LABELS.get(field, field))}.",
        )
        return

    if state == "await_search_customer":
        rows = DBX.search_customers(text_value, 50)
        set_state(context, None)
        if not rows:
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:200:message','لا توجد نتائج.', []), reply_markup=admin_kb(user.id))
            return
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:202:message','<b>نتائج البحث</b>\n\nاضغط على العميل:', []),
            parse_mode=ParseMode.HTML,
            reply_markup=customers_list_kb(rows, 0),
        )
        return

    if state == "await_edit_customer_field":
        customer_id = int(payload.get("customer_id"))
        field = str(payload.get("field") or "")
        row = DBX.get_customer(customer_id)
        if not row or field not in CUSTOMER_FIELD_LABELS:
            set_state(context, None)
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:215:message','❌ العميل أو الحقل غير موجود.', []), reply_markup=admin_kb(user.id))
            return
        value: Any = text_value
        error = ""
        if field == "full_name":
            if not (2 <= len(text_value) <= 80) or text_value.isdigit():
                error = "الاسم غير صالح."
        elif field in {"phone_primary", "phone_alt"}:
            if field == "phone_alt" and text_value in {"0", "-", "بدون", "لا يوجد"}:
                value = ""
            else:
                value = normalize_phone(text_value)
                minimum = 7 if field == "phone_primary" else 3
                maximum = 15 if field == "phone_primary" else 20
                if not (minimum <= len(value) <= maximum):
                    error = "الرقم غير صالح."
                elif DBX.get_customer_by_any_phone(value, exclude_customer_id=customer_id):
                    error = "الرقم مستخدم لعميل آخر."
                elif field == "phone_primary" and phones_match(value, row["phone_alt"]):
                    error = "الرقم يطابق الرقم البديل."
                elif field == "phone_alt" and phones_match(value, row["phone_primary"]):
                    error = "الرقم يطابق الرقم الأساسي."
        elif field == "telegram_id":
            if not text_value.isdigit() or not (5 <= len(text_value) <= 20):
                error = "Telegram ID غير صالح."
            else:
                duplicate = DBX.get_customer_by_telegram(int(text_value))
                if duplicate and int(duplicate["id"]) != customer_id:
                    error = "Telegram ID مربوط بعميل آخر."
                value = int(text_value)
        elif field == "notes":
            value = "" if text_value in {"0", "-", "بدون", "لا توجد"} else text_value
            if len(str(value)) > 500:
                error = "الملاحظات طويلة جدًا."

        if error:
            await update.message.reply_text(
                render_bot_text('bot.handlers.text.py:251:message','❌ {v0}\nأعد الإرسال؛ الأدمن غير مقيد بعدد محاولات.', [f'{html(error)}']),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            DBX.update_customer(customer_id, **{field: value})
        except Exception as exc:
            await update.message.reply_text(
                render_bot_text('bot.handlers.text.py:259:message','❌ فشل التعديل: <code>{v0}</code>', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
            )
            return
        DBX.audit(user.id, actor_name(user), "EDIT_CUSTOMER", "customer", customer_id, f"{field}={value}")
        set_state(context, None)
        row = DBX.get_customer(customer_id)
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:267:message','✅ تم تعديل بيانات العميل.', []),
            reply_markup=customer_actions_kb(row, user.id),
        )
        await update.message.reply_text(render_bot_text('bot.handlers.text.py:271:message','{v0}', [str(customer_detail_text(row))]), parse_mode=ParseMode.HTML)
        return

    if state == "await_link_customer_id":
        customer_id = int(payload.get("customer_id"))
        if not text_value.isdigit() or not (5 <= len(text_value) <= 20):
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:277:message','❌ Telegram ID غير صالح. أعد الإرسال؛ لا يوجد حد للمحاولات.', []))
            return
        row = DBX.get_customer(customer_id)
        if not row:
            set_state(context, None)
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:282:message','❌ العميل غير موجود.', []), reply_markup=admin_kb(user.id))
            return
        duplicate = DBX.get_customer_by_telegram(int(text_value))
        if duplicate and int(duplicate["id"]) != customer_id:
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:286:message','❌ Telegram ID مربوط بعميل آخر. أعد الإرسال.', []))
            return
        DBX.update_customer(customer_id, telegram_id=int(text_value))
        DBX.audit(user.id, actor_name(user), "LINK_TELEGRAM", "customer", customer_id, f"telegram_id={text_value}")
        linked = DBX.get_customer(customer_id)
        sent, _ = await send_customer_welcome(context, linked)
        set_state(context, None)
        note = "وتم إرسال رسالة الترحيب." if sent else "لكن تعذر إرسال الترحيب؛ يجب أن يبدأ العميل البوت أولًا."
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:294:message','✅ تم الربط {v0}', [f'{note}']),
            reply_markup=customer_actions_kb(linked, user.id),
        )
        return

    if state == "await_admin_id":
        if int(user.id) != int(ADMIN_ID):
            set_state(context, None)
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:303:message','⛔ للأدمن الرئيسي فقط.', []))
            return
        if not text_value.isdigit() or not (5 <= len(text_value) <= 20):
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:306:message','❌ Telegram ID غير صالح. أعد الإرسال؛ لا يوجد حد للمحاولات.', []))
            return
        target_id = int(text_value)
        if target_id == int(ADMIN_ID):
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:310:message','❌ هذا Telegram ID للأدمن الرئيسي.', []))
            return
        if DBX.get_admin(target_id):
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:313:message','❌ هذا الأدمن مضاف مسبقًا.', []))
            return
        display_name = str(target_id)
        username = ""
        try:
            chat = await context.bot.get_chat(target_id)
            display_name = getattr(chat, "full_name", None) or getattr(chat, "title", None) or str(target_id)
            username = getattr(chat, "username", None) or ""
        except Exception:
            log.debug("Could not resolve prospective admin chat target=%s", target_id, exc_info=True)
        draft = context.user_data.setdefault("admin_draft", {})
        draft.update({
            "telegram_id": target_id,
            "display_name": display_name,
            "username": username,
            "permissions": set(),
        })
        set_state(context, None)
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:331:message','<b>اختر صلاحيات الأدمن</b>\n\n👤 {v0}\n🆔 <code>{v1}</code>\n\nحدد الصلاحيات بالأزرار أو اضغط منح جميع الصلاحيات.', [f'{html(display_name)}',f'{target_id}']),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_permissions_kb(set(), "add", None),
        )
        return

    if state == "await_import_backup":
        backup_root = BACKUP_DIR.resolve()
        path = (BACKUP_DIR / text_value).resolve()
        if not path.is_relative_to(backup_root) or path.suffix.lower() != ".json":
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:344:message','❌ اختر اسم ملف JSON موجودًا داخل مجلد backups فقط.', []))
            return
        if not path.exists():
            await update.message.reply_text(render_bot_text('bot.handlers.text.py:347:message','❌ الملف غير موجود. أعد الإرسال؛ لا يوجد حد للمحاولات.', []))
            return
        try:
            payload_data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload_data, dict):
                raise ValueError("صيغة النسخة الاحتياطية غير صالحة")
            safety_path = create_backup_file()
            DBX.import_snapshot(payload_data)
        except Exception as exc:
            log.exception("Legacy local backup import failed admin=%s path=%s", user.id, path)
            await update.message.reply_text(
                render_bot_text('bot.handlers.text.py:357:message','❌ فشل الاستيراد: <code>{v0}</code>', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
            )
            return
        DBX.audit(
            user.id,
            actor_name(user),
            "IMPORT_BACKUP",
            "backup",
            path.name,
            f"safety={safety_path.name}",
        )
        set_state(context, None)
        await update.message.reply_text(
            render_bot_text('bot.handlers.text.py:371:message','✅ تم الاستيراد بنجاح.\n🛡️ نسخة الأمان السابقة: <code>{v0}</code>', [f'{html(safety_path.name)}']),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb(user.id),
        )
        return

    await update.message.reply_text(render_bot_text('bot.handlers.text.py:379:message','استخدم الأزرار من لوحة الأدمن.', []), reply_markup=admin_kb(user.id))


async def post_init(application: Application):
    if application.job_queue is None:
        raise RuntimeError("JobQueue غير متاح. ثبّت python-telegram-bot[job-queue].")
    if not application.job_queue.get_jobs_by_name("order-withdraw-monitor"):
        application.job_queue.run_repeating(
            monitor_withdrawals,
            interval=90,
            first=90,
            name="order-withdraw-monitor",
        )
    log.info("Base services initialized app=%s", APP_NAME)





_export_globals(globals())

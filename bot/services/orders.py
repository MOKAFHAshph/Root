"""Order helpers, purchase flow, payment verification, and payout business logic.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# إجراءات مساعدة
# ============================================================




async def send_or_edit(target: Any, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    if hasattr(target, "edit_message_text"):
        return await target.edit_message_text(render_bot_text('bot.services.orders.py:18:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if hasattr(target, "reply_text"):
        return await target.reply_text(render_bot_text('bot.services.orders.py:20:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if hasattr(target, "message") and hasattr(target.message, "reply_text"):
        return await target.message.reply_text(render_bot_text('bot.services.orders.py:22:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    raise RuntimeError("Unsupported target for send_or_edit")


async def notify_admin(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    recipients = {int(ADMIN_ID)}
    for row in DBX.list_admins():
        if row["status"] == "ACTIVE":
            recipients.add(int(row["telegram_id"]))
    for recipient in recipients:
        try:
            await context.bot.send_message(
                recipient,
                render_bot_text('bot.services.orders.py:37:message','{v0}', [str(text)]),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            log.exception("failed to notify admin %s", recipient)


async def show_main(target: Any, context: ContextTypes.DEFAULT_TYPE):
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    customer = DBX.get_customer_by_telegram(user.id)
    is_admin = DBX.is_admin(user.id)
    if not is_admin and (not customer or customer["status"] != "ACTIVE"):
        text = (
            render_bot_text('fragment.bot.services.orders.py:show_main:54','⛔ <b>الوصول غير متاح</b>\n\nهذا البوت مخصص للعملاء المضافين مسبقًا فقط.\nإذا كان لديك حساب، تواصل مع الأدمن لربط Telegram ID الخاص بك.',[])
        )
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(render_bot_text('bot.services.orders.py:58:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML)
        else:
            await target.message.reply_text(render_bot_text('bot.services.orders.py:60:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML)
        return

    text = render_bot_text('fragment.bot.services.orders.py:show_main:64','<b>{v0}</b>\n\nأهلاً <b>{v1}</b>',[f'{html(APP_NAME)}',f'{html(user.first_name)}'])
    if customer:
        text += render_bot_text('fragment.bot.services.orders.py:show_main:66','\n👤 العميل: <b>{v0}</b>',[f"{html(customer['full_name'])}"])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(render_bot_text('bot.services.orders.py:67:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=user_main_kb(is_admin))
    else:
        await target.message.reply_text(render_bot_text('bot.services.orders.py:69:message','{v0}', [str(text)]), parse_mode=ParseMode.HTML, reply_markup=user_main_kb(is_admin))


async def require_customer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[sqlite3.Row]:
    user = update.effective_user
    if DBX.is_admin(user.id):
        return None
    customer = DBX.get_customer_by_telegram(user.id)
    if not customer:
        DBX.security(user.id, user.username, "unknown_user_attempt", "HIGH", "محاولة استخدام من Telegram ID غير مربوط")
        await update.effective_message.reply_text(render_bot_text('bot.services.orders.py:79:message','⛔ غير مسموح لك باستخدام البوت. تواصل مع الأدمن.', []), parse_mode=ParseMode.HTML)
        return None
    if customer["status"] != "ACTIVE":
        DBX.security(user.id, user.username, "suspended_account_attempt", "HIGH", f"محاولة استخدام على حساب موقوف: customer_id={customer['id']}")
        await update.effective_message.reply_text(render_bot_text('bot.services.orders.py:83:message','⛔ حسابك موقوف حاليًا. تواصل مع الأدمن.', []), parse_mode=ParseMode.HTML)
        return None
    return customer


def actor_name(user) -> str:
    if not user:
        return "—"
    return f"{user.full_name} @{user.username or '—'}"


# ============================================================
# منطق الأعمال
# ============================================================


async def create_buy_order(update: Update, context: ContextTypes.DEFAULT_TYPE, amount_text: str):
    customer = await require_customer(update, context)
    if customer is None:
        return
    try:
        amount = parse_decimal(amount_text)
    except InvalidOperation:
        await update.message.reply_text(render_bot_text('bot.services.orders.py:106:message','❌ أدخل رقمًا صحيحًا مثل <code>10</code>', []), parse_mode=ParseMode.HTML)
        return
    if amount < MIN_USDT_ORDER or amount > MAX_USDT_ORDER:
        await update.message.reply_text(
            render_bot_text('bot.services.orders.py:109:message','❌ الكمية يجب أن تكون بين <b>{v0}</b> و <b>{v1}</b> USDT.', [f'{html(format_display_amount(MIN_USDT_ORDER))}',f'{html(format_display_amount(MAX_USDT_ORDER))}']),
            parse_mode=ParseMode.HTML,
        )
        return
    if not DBX.sales_enabled():
        await update.message.reply_text(render_bot_text('bot.services.orders.py:115:message','⛔ البيع متوقف حاليًا.', []), parse_mode=ParseMode.HTML)
        return
    rate = effective_customer_rate(update.effective_user.id, customer=customer)
    expected_yer = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    order_id = DBX.create_order(customer, amount, rate, expected_yer)
    order = DBX.get_order(order_id)
    DBX.audit(update.effective_user.id, actor_name(update.effective_user), "CREATE_ORDER", "order", order["order_code"], f"amount={q8(amount)} rate={q2(rate)}")
    set_state(context, None)
    await update.message.reply_text(render_bot_text('bot.services.orders.py:123:message','{v0}', [str(payment_message(order))]), parse_mode=ParseMode.HTML, reply_markup=order_payment_kb(order_id))
    await notify_admin(
        context,
        (
            render_bot_text('notice.bot.services.orders.py:126','<b>🆕 طلب جديد</b>\n\n🧾 <code>{v0}</code>\n👤 {v1} | <code>{v2}</code>\n📱 {v3}\n💵 {v4} USDT\n💰 {v5} ريال',[f"{html(order['order_code'])}",f"{html(customer['full_name'])}",f"{customer['id']}",f"{html(customer['phone_primary'])}",f"{html(order['crypto_amount'])}",f"{html(order['expected_yer_amount'])}"])
        ),
    )


async def verify_payment_reference(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: int,
    ref_id: str,
):
    order = DBX.get_order(order_id)
    if not order:
        await update.message.reply_text(render_bot_text('bot.services.orders.py:145:message','❌ الطلب غير موجود.', []))
        set_state(context, None)
        return
    customer = DBX.get_customer(order["customer_id"])
    if not customer or customer["status"] != "ACTIVE":
        await update.message.reply_text(render_bot_text('bot.services.orders.py:150:message','⛔ هذا العميل غير نشط.', []))
        set_state(context, None)
        return
    if order["status"] != "WAITING_PAYMENT":
        await update.message.reply_text(render_bot_text('bot.services.orders.py:154:message','⚠️ هذا الطلب ليس في حالة انتظار دفع.', []))
        set_state(context, None)
        return
    if not ref_id.isdigit() or not (8 <= len(ref_id) <= 20):
        await update.message.reply_text(render_bot_text('bot.services.orders.py:158:message','❌ رقم العملية غير صالح. أرسل أرقامًا فقط.', []))
        return

    existing = DBX.get_order_by_payment_reference(ref_id)
    if existing and int(existing["id"]) != int(order_id):
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "duplicate_payment_reference",
            "HIGH",
            f"reference={ref_id} used by order {existing['order_code']}",
            order["order_code"],
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.orders.py:172','<b>🚨 محاولة إعادة استخدام رقم عملية</b>\n\nالطلب: <code>{v0}</code>\nالمرجع: <code>{v1}</code>\nمستخدم في: <code>{v2}</code>',[f"{html(order['order_code'])}",f'{html(ref_id)}',f"{html(existing['order_code'])}"]),
        )
        attempts, remaining = failed_attempt(context)
        if attempts >= MAX_INPUT_ATTEMPTS:
            set_state(context, None)
            await update.message.reply_text(
                render_bot_text('bot.services.orders.py:181:message','❌ رقم العملية مستخدم مسبقًا. انتهت المحاولات ({v0}).', [f'{MAX_INPUT_ATTEMPTS}']),
                reply_markup=user_main_kb(False),
            )
        else:
            await update.message.reply_text(
                render_bot_text('bot.services.orders.py:186:message','❌ رقم العملية مستخدم مسبقًا. المحاولة {v0}/{v1} — المتبقي {v2}.', [f'{attempts}',f'{MAX_INPUT_ATTEMPTS}',f'{remaining}'])
            )
        return

    if not JaibClient.enabled():
        await update.message.reply_text(render_bot_text('bot.services.orders.py:192:message','❌ إعدادات Jaib غير مكتملة في ملف .env', []))
        set_state(context, None)
        return

    wait = await update.message.reply_text(render_bot_text('bot.services.orders.py:196:message','⏳ جاري التحقق من العملية من Jaib...', []))
    client = JaibClient()
    if not client.init():
        await wait.edit_text("❌ فشل الاتصال بخوادم Jaib.")
        set_state(context, None)
        return
    ok, login_message = client.login()
    if not ok:
        await wait.edit_text(
            f"❌ تعذر تسجيل الدخول إلى Jaib:\n{html(login_message)}",
            parse_mode=ParseMode.HTML,
        )
        set_state(context, None)
        return
    ok, result = client.query_ref(ref_id)
    if not ok:
        attempts, remaining = failed_attempt(context)
        if attempts >= MAX_INPUT_ATTEMPTS:
            set_state(context, None)
            await wait.edit_text(
                f"❌ لم يتم التحقق بعد {MAX_INPUT_ATTEMPTS} محاولات.\n\nالسبب: {html(result)}",
                parse_mode=ParseMode.HTML,
                reply_markup=user_main_kb(False),
            )
        else:
            await wait.edit_text(
                f"❌ لم يتم العثور على العملية أو تعذر التحقق منها:\n{html(result)}\n\n"
                f"المحاولة {attempts}/{MAX_INPUT_ATTEMPTS} — المتبقي {remaining}.\n"
                "أرسل رقم العملية الصحيح مرة أخرى.",
                parse_mode=ParseMode.HTML,
            )
        return

    tx: JaibTx = result
    expected = Decimal(order["expected_yer_amount"])
    amount_matches = tx.amount == expected if JAIB_STRICT_EXACT_AMOUNT else tx.amount >= expected
    if not amount_matches:
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "amount_mismatch",
            "MEDIUM",
            f"expected={expected} actual={tx.amount}",
            order["order_code"],
        )
        DBX.update_order(
            order_id,
            payment_reference=tx.reference_id,
            payer_name=tx.sender_name,
            payer_phone=normalize_phone(tx.sender_phone),
            paid_yer_amount=q2(tx.amount),
            status="REJECTED",
            rejection_reason="AMOUNT_MISMATCH",
            error_message=f"expected={expected} actual={tx.amount}",
        )
        set_state(context, None)
        await wait.edit_text(
            f"❌ المبلغ لا يطابق الطلب.\n\n"
            f"المتوقع: <b>{html(order['expected_yer_amount'])}</b> ريال\n"
            f"الواصل: <b>{html(format_display_amount(tx.amount))}</b> {html(tx.currency)}",
            parse_mode=ParseMode.HTML,
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.orders.py:259','<b>🚫 اختلاف مبلغ</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(ref_id)}']),
        )
        return

    matched_registered, actual_candidate, match_source, candidates = match_customer_sender(customer, tx)
    candidates_json = json.dumps(candidates, ensure_ascii=False)
    candidate_text = ", ".join(item["value"] for item in candidates) or "لا يوجد"
    raw_summary = json.dumps(
        {
            "sender_name": tx.sender_name,
            "sender_phone": tx.sender_phone,
            "tx_date": tx.tx_date,
            "tx_type": tx.tx_type,
        },
        ensure_ascii=False,
    )

    if not candidates:
        DBX.update_order(
            order_id,
            payment_reference=tx.reference_id,
            payer_name=tx.sender_name,
            payer_phone="",
            payer_phone_candidates=candidates_json,
            payment_raw_summary=raw_summary,
            paid_yer_amount=q2(tx.amount),
            status="PAYMENT_REVIEW",
            rejection_reason=None,
            error_message="SENDER_PHONE_NOT_EXTRACTED",
        )
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "sender_phone_missing",
            "HIGH",
            f"primary={customer['phone_primary']} alt={customer['phone_alt'] or ''}",
            order["order_code"],
        )
        set_state(context, None)
        await wait.edit_text(
            f"🛡️ <b>وصلت الدفعة، لكن رقم المرسل غير ظاهر</b>\n\n"
            f"🔢 رقم العملية: <code>{html(tx.reference_id)}</code>\n"
            f"💰 المبلغ: <b>{html(format_display_amount(tx.amount))} {html(tx.currency)}</b>\n"
            f"👤 بيانات المرسل: <code>{html(tx.sender_name or '—')}</code>\n\n"
            "تم إيقاف السحب وإرسال العملية للأدمن للمراجعة.",
            parse_mode=ParseMode.HTML,
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.orders.py:309','<b>🛡️ عملية تحتاج مراجعة</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>\n👤 العميل: {v2}\n💰 {v3} {v4}\n👤 بيانات المرسل: <code>{v5}</code>',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f"{html(customer['full_name'])}",f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}',f"{html(tx.sender_name or '—')}"]),
            reply_markup=InlineKeyboardMarkup([[
                btn(render_bot_button('bot.services.orders.py:317:button','✅ قبول بعد المراجعة', []), f"adm_payment_approve:{order_id}", "success"),
                btn(render_bot_button('bot.services.orders.py:318:button','❌ رفض', []), f"adm_payment_reject:{order_id}", "danger"),
            ]]),
        )
        return

    if not matched_registered:
        DBX.update_order(
            order_id,
            payment_reference=tx.reference_id,
            payer_name=tx.sender_name,
            payer_phone=normalize_phone(tx.sender_phone) or candidates[0]["value"],
            payer_phone_candidates=candidates_json,
            payment_raw_summary=raw_summary,
            paid_yer_amount=q2(tx.amount),
            status="REJECTED",
            rejection_reason="SENDER_PHONE_MISMATCH",
            error_message=f"candidates={candidate_text}",
        )
        DBX.security(
            update.effective_user.id,
            update.effective_user.username,
            "sender_phone_mismatch",
            "HIGH",
            f"candidates={candidate_text} primary={customer['phone_primary']} alt={customer['phone_alt'] or ''}",
            order["order_code"],
        )
        set_state(context, None)
        await wait.edit_text(
            f"❌ <b>تم رفض العملية</b>\n\n"
            f"🔢 رقم العملية: <code>{html(tx.reference_id)}</code>\n"
            f"💰 المبلغ الواصل: <b>{html(format_display_amount(tx.amount))} {html(tx.currency)}</b>\n"
            f"👤 بيانات المرسل: <code>{html(tx.sender_name or '—')}</code>\n"
            f"📱 الرقم الظاهر: <code>{html(tx.sender_phone or candidate_text)}</code>\n\n"
            "رقم المرسل لا يطابق الرقم الأساسي أو البديل المسجل.",
            parse_mode=ParseMode.HTML,
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.orders.py:355','<b>🚨 دفع غير مطابق</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>\n👤 العميل: {v2}\n👤 بيانات المرسل: <code>{v3}</code>\n📱 المستخرج: <code>{v4}</code>\n📱 الأساسي: <code>{v5}</code>\n📱 البديل: <code>{v6}</code>',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f"{html(customer['full_name'])}",f"{html(tx.sender_name or '—')}",f'{html(candidate_text)}',f"{html(customer['phone_primary'])}",f"{html(customer['phone_alt'] or '—')}"]),
        )
        return

    try:
        DBX.update_order(
            order_id,
            payment_reference=tx.reference_id,
            payer_name=tx.sender_name,
            payer_phone=actual_candidate,
            payer_phone_matched=matched_registered,
            payer_match_source=match_source,
            payer_phone_candidates=candidates_json,
            payment_raw_summary=raw_summary,
            paid_yer_amount=q2(tx.amount),
            status="PAYMENT_VERIFIED",
            paid_at=now_str(),
            rejection_reason=None,
            error_message=None,
        )
    except sqlite3.IntegrityError:
        await wait.edit_text("❌ رقم العملية مستخدم مسبقًا.")
        set_state(context, None)
        return

    DBX.audit(
        update.effective_user.id,
        actor_name(update.effective_user),
        "VERIFY_PAYMENT",
        "order",
        order["order_code"],
        f"reference={tx.reference_id} matched={matched_registered} source={match_source}",
    )
    order = DBX.get_order(order_id)
    set_state(context, None)
    await wait.edit_text(
        verify_success_message(order, tx),
        parse_mode=ParseMode.HTML,
        reply_markup=delivery_kb(order_id),
    )
    await notify_admin(
        context,
        render_bot_text('notice.bot.services.orders.py:404','<b>✅ تم التحقق من الدفع</b>\n\n🧾 <code>{v0}</code>\n🔢 <code>{v1}</code>\n👤 العميل: {v2}\n💰 {v3} {v4}\n👤 بيانات المرسل: <code>{v5}</code>\n📱 الرقم المستخرج: <code>{v6}</code>\n📱 الرقم المطابق: <code>{v7}</code>\n🔎 المصدر: <code>{v8}</code>',[f"{html(order['order_code'])}",f'{html(tx.reference_id)}',f"{html(customer['full_name'])}",f'{html(format_display_amount(tx.amount))}',f'{html(tx.currency)}',f"{html(tx.sender_name or '—')}",f"{html(actual_candidate or '—')}",f'{html(matched_registered)}',f"{html(match_source or '—')}"]),
    )


async def save_binance_id(query, context: ContextTypes.DEFAULT_TYPE, order_id: int, binance_id: str):
    order = DBX.get_order(order_id)
    if not order:
        await query.edit_message_text(render_bot_text('bot.services.orders.py:420:message','❌ الطلب غير موجود.', []))
        return
    DBX.update_order(order_id, delivery_type="BINANCE_ID", binance_id=binance_id, status="WAITING_ADMIN_PAYOUT")
    DBX.audit(query.from_user.id, actor_name(query.from_user), "SAVE_BINANCE_ID", "order", order["order_code"], f"binance_id={binance_id}")
    order = DBX.get_order(order_id)
    await send_or_edit(
        query,
        (
            render_bot_text('bot.services.orders.py:425:message','<b>✅ تم حفظ بيانات الاستلام</b>\n\n🧾 الطلب: <code>{v0}</code>\n🟡 Binance ID: <code>{v1}</code>\n💵 الكمية: <b>{v2} USDT</b>\n\n🛡️ هذا المسار مضبوط حاليًا على تنفيذ يدوي من الأدمن حتى تتوفر لك خدمة Binance Payout الرسمية.', [f"{html(order['order_code'])}",f'{html(binance_id)}',f"{html(order['crypto_amount'])}"])
        ),
        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:434:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
    )
    await notify_admin(
        context,
        (
            render_bot_text('notice.bot.services.orders.py:434','<b>🟡 طلب Binance ID بانتظار الأدمن</b>\n\n🧾 <code>{v0}</code>\n💵 {v1} USDT\n🟡 Binance ID: <code>{v2}</code>',[f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f'{html(binance_id)}'])
        ),
        reply_markup=admin_manual_order_kb(order_id),
    )


async def submit_onchain_withdraw(query, context: ContextTypes.DEFAULT_TYPE, order_id: int, network_code: str, address: str):
    order = DBX.get_order(order_id)
    if not order:
        await query.edit_message_text(render_bot_text('bot.services.orders.py:451:message','❌ الطلب غير موجود.', []))
        return
    network_label = NETWORK_LOOKUP.get(network_code, network_code)
    amount = Decimal(order["crypto_amount"])
    DBX.update_order(order_id, delivery_type="ONCHAIN", network_code=network_code, network_label=network_label, destination_address=address)
    if not validate_address_for_network(network_code, address):
        await send_or_edit(
            query,
            render_bot_text('bot.services.orders.py:457:message','<b>❌ العنوان غير مطابق للشبكة المختارة</b>\n\n🌐 الشبكة: <b>{v0}</b>\n📬 العنوان: <code>{v1}</code>', [f'{html(network_label)}',f'{html(address)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:460:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if not AUTO_ONCHAIN_WITHDRAW or not BINANCE_WITHDRAW_ENABLED or not BINANCE.enabled() or not DBX.auto_payout_enabled():
        DBX.update_order(order_id, status="WAITING_ADMIN_PAYOUT")
        order = DBX.get_order(order_id)
        await send_or_edit(
            query,
            (
                render_bot_text('bot.services.orders.py:467:message','<b>✅ تم حفظ عنوان الاستلام</b>\n\n🧾 الطلب: <code>{v0}</code>\n🌐 الشبكة: <b>{v1}</b>\n📬 العنوان: <code>{v2}</code>\n\n🛡️ التنفيذ التلقائي غير مفعل حاليًا، لذلك تم تحويل الطلب إلى الأدمن للمراجعة.', [f"{html(order['order_code'])}",f"{html(order['network_label'])}",f'{html(address)}'])
            ),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:476:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(
            context,
            (
                render_bot_text('notice.bot.services.orders.py:472','<b>🌐 طلب عنوان شبكة بانتظار الأدمن</b>\n\n🧾 <code>{v0}</code>\n💵 {v1} USDT\n🌐 {v2}\n📬 <code>{v3}</code>',[f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f"{html(order['network_label'])}",f'{html(address)}'])
            ),
            reply_markup=admin_manual_order_kb(order_id),
        )
        return

    try:
        quote = BINANCE.validate_withdraw_request(coin=BINANCE_COIN, network=network_code, address=address, amount=amount)
    except Exception as exc:
        log.exception(
            "Failed to prepare legacy Binance withdrawal order=%s user=%s",
            order["order_code"],
            query.from_user.id,
        )
        DBX.update_order(order_id, status="FAILED", error_message=str(exc))
        await query.edit_message_text(
            render_bot_text('bot.services.orders.py:500:message','<b>❌ تعذر تجهيز طلب السحب</b>\n\nالسبب: <code>{v0}</code>', [f'{html(exc)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:502:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(context, render_bot_text('notice.bot.services.orders.py:495','<b>❌ فشل تجهيز السحب</b>\n\n🧾 <code>{v0}</code>\nالخطأ: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(exc)}']))
        return

    payout_request_id = f"WD-{order['order_code']}"
    DBX.update_order(order_id, status="READY_FOR_PAYOUT", payout_request_id=payout_request_id)
    if not DBX.mark_processing_payout(order_id):
        await query.edit_message_text(render_bot_text('bot.services.orders.py:510:message','⚠️ الطلب ليس جاهزًا للسحب الآن.', []))
        return

    try:
        response = BINANCE.withdraw(coin=BINANCE_COIN, network=network_code, address=address, amount=amount, withdraw_order_id=payout_request_id, wallet_type=BINANCE_WALLET_TYPE)
        DBX.update_order(order_id, status="WITHDRAW_SUBMITTED", binance_withdraw_id=response.get("id"), payout_started_at=now_str())
        DBX.audit(query.from_user.id, actor_name(query.from_user), "SUBMIT_WITHDRAW", "order", order["order_code"], f"network={network_code} withdraw_id={response.get('id')}")
        order = DBX.get_order(order_id)
        await send_or_edit(
            query,
            (
                render_bot_text('bot.services.orders.py:518:message','<b>✅ تم إرسال طلب السحب إلى Binance</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 الكمية: <b>{v1} USDT</b>\n🌐 الشبكة: <b>{v2}</b>\n📬 العنوان: <code>{v3}</code>\n💸 الرسوم المتوقعة: <b>{v4} {v5}</b>\n🆔 Binance Withdraw ID: <code>{v6}</code>\n\n🔄 سيتم متابعة الحالة وإشعارك عند الاكتمال.', [f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f"{html(order['network_label'])}",f'{html(address)}',f"{html(format_display_amount(quote['fee']))}",f'{html(BINANCE_COIN)}',f"{html(response.get('id', '—'))}"])
            ),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:530:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(context, render_bot_text('notice.bot.services.orders.py:516','<b>🚀 تم إرسال طلب سحب</b>\n\n🧾 <code>{v0}</code>\n🆔 <code>{v1}</code>',[f"{html(order['order_code'])}",f"{html(response.get('id', '—'))}"]))
    except Exception as exc:
        log.exception(
            "Failed to submit legacy Binance withdrawal order=%s user=%s",
            order["order_code"],
            query.from_user.id,
        )
        DBX.update_order(order_id, status="FAILED", error_message=str(exc))
        await query.edit_message_text(
            render_bot_text('bot.services.orders.py:540:message','<b>❌ فشل إرسال طلب سحب إلى Binance</b>\n\n🧾 {v0}\nالخطأ: <code>{v1}</code>', [f"{html(order['order_code'])}",f'{html(exc)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.orders.py:542:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(context, render_bot_text('notice.bot.services.orders.py:528','<b>❌ فشل سحب Binance</b>\n\n🧾 <code>{v0}</code>\nالخطأ: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(exc)}']))


async def monitor_withdrawals(context: ContextTypes.DEFAULT_TYPE):
    if not BINANCE.enabled() or not BINANCE_WITHDRAW_ENABLED:
        return
    pending = DBX.list_orders_by_status(("WITHDRAW_SUBMITTED", "PROCESSING_PAYOUT"))
    for order in pending:
        request_id = order["payout_request_id"]
        if not request_id:
            continue
        try:
            items = BINANCE.withdraw_history(withdraw_order_id=request_id, coin=BINANCE_COIN)
        except Exception as exc:
            log.warning("withdraw monitor failed for %s: %s", request_id, exc)
            continue
        if not items:
            continue
        record = items[0]
        txid = normalize_txid(record.get("txId"))
        complete_time = record.get("completeTime") or ""
        info = record.get("info") or ""
        if txid and complete_time:
            DBX.update_order(order["id"], status="COMPLETED", txid=txid, completed_at=now_str())
            try:
                await context.bot.send_message(
                    order["telegram_user_id"],
                    render_bot_text('bot.services.orders.py:569:message','<b>✅ تم تنفيذ السحب بنجاح</b>\n\n🧾 <code>{v0}</code>\n\n━━━━━━━━━━━━━━━━━━━━\n🔖 رقم العملية (TxID):\n<code>{v1}</code>\n━━━━━━━━━━━━━━━━━━━━\n⚠️ احتفظ بهذا الرقم لتأكيد أي عملية دفع', [f"{html(order['order_code'])}",f'{html(txid)}']),
                    parse_mode=ParseMode.HTML,
                    reply_markup=txid_copy_keyboard(txid),
                )
            except Exception:
                log.exception("notify user completion failed")
            await notify_admin(context, render_bot_text('notice.bot.services.orders.py:561','<b>✅ اكتمل سحب Binance</b>\n\n🧾 <code>{v0}</code>\n🔗 <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(txid)}']))
            continue
        if info and not txid:
            DBX.update_order(order["id"], status="FAILED", error_message=str(info))
            try:
                await context.bot.send_message(order["telegram_user_id"], render_bot_text('bot.services.orders.py:586:message','<b>❌ فشل السحب</b>\n\n🧾 <code>{v0}</code>\nالسبب: <code>{v1}</code>', [f"{html(order['order_code'])}",f'{html(info)}']), parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("notify user failure failed")
            await notify_admin(context, render_bot_text('notice.bot.services.orders.py:569','<b>❌ فشل سحب Binance</b>\n\n🧾 <code>{v0}</code>\nالسبب: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(info)}']))




_export_globals(globals())

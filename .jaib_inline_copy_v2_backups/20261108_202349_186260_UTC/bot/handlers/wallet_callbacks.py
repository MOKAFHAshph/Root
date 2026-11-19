"""Wallet callbacks and administrative wallet actions.

"""
from __future__ import annotations

import traceback
import asyncio
from bot.services.dynamic_withdrawals import binance_live_limits

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.ui.status_icons import wallet_status_icon, wallet_display_text, wallet_withdraw_icon
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- اعتراض الأزرار ----------------


_wallet_legacy_on_callback = on_callback


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    wallet_callbacks = (
        data.startswith("wallet_")
        or data.startswith("adm_wallet")
        or data.startswith("adm_technical")
        or data == "adm_channels_settings"
        or data == "buy_usdt"
        or data == "my_orders"
        or data == "adm_settings_overview"
    )
    if not wallet_callbacks:
        await _wallet_legacy_on_callback(update, context)
        return

    await query.answer()
    if not throttle_allowed(context, user.id):
        return
    is_admin = DBX.is_admin(user.id)

    if data == "buy_usdt":
        data = "wallet_withdraw"
    elif data == "my_orders":
        data = "wallet_history"

    if data == "wallet_balance":
        available, locked = wallet_balances(user.id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:49:message','<b>💰 رصيدي</b>\n\nالرصيد المتاح: <b>{v0} USDT</b>\nالرصيد المحجوز: <b>{v1} USDT</b>\nإجمالي الرصيد: <b>{v2} USDT</b>', [f'{format_display_amount(available)}',f'{format_display_amount(locked)}',f'{format_display_amount(available + locked)}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:55:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data == "wallet_stats":
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:60:message','{v0}', [str(wallet_stats_text(user.id))]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:63:button','📒 سجل العمليات', []), "wallet_history")], [btn(render_bot_button('bot.handlers.wallet_callbacks.py:63:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data == "wallet_history":
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:68:message','{v0}', [str(wallet_history_text(user.id))]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:71:button','📊 إحصائياتي', []), "wallet_stats")], [btn(render_bot_button('bot.handlers.wallet_callbacks.py:71:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    if data == "wallet_deposit":
        if current_state(context)[0] == "await_wallet_deposit_processing":
            await query.answer("⏳ عملية إيداع قيد التحقق. انتظر النتيجة.", show_alert=True)
            return
        customer = DBX.get_customer_by_telegram(user.id)
        if not customer or customer["status"] != "ACTIVE":
            await query.answer("حسابك غير نشط", show_alert=True)
            return
        if not wallet_deposit_enabled():
            await query.answer("الإيداع متوقف مؤقتاً", show_alert=True)
            return

        draft = wallet_prepare_deposit(user.id)
        set_state(
            context,
            "await_wallet_deposit_reference",
            deposit_id=0,
            exchange_rate_yer=draft["exchange_rate_yer"],
            prepared_at=draft["prepared_at"],
        )
        log.info("Deposit instructions shown without creating operation user=%s", user.id)
        # Compact Jaib screen. Number is in a single red native copy button,
        # not printed as a large heading or within a rich-message table.
        from bot.ui.deposit_copy_card import compact_html, copy_button, phone_to_copy
        from bot.services.menu_customization import menu_settings
        display_cfg = menu_settings()
        display_texts = display_cfg.get('texts', {})
        display_icons = display_cfg.get('status_icons', {}).get('deposit_title', {})
        receiver_phone = phone_to_copy(JAIB_SECONDARY_PHONE, JAIB_PRIMARY_PHONE)
        receiver_name = JAIB_SECONDARY_BENEFICIARY_NAME if JAIB_SECONDARY_PHONE else ''
        buttons = []
        if receiver_phone:
            try:
                buttons.append([copy_button(
                    receiver_phone,
                    emoji_id=display_icons.get('emoji_id', '5897909375028172264'),
                )])
            except ValueError:
                log.warning('Jaib deposit receiving phone is invalid; copy button suppressed')
                receiver_phone = ''
        if callable(globals().get('jaib_scan_available')) and jaib_scan_available():
            buttons.append([btn(render_bot_button(
                'bot.handlers.wallet_callbacks.py:106:button',
                '✅ تأكيد الإرسال — فحص التحويلات', []), 'jaib_scan', 'success')])
        buttons.extend([
            [InlineKeyboardButton(render_bot_button(
                'bot.handlers.wallet_callbacks.py:107:button',
                '💬 طرق دفع أخرى / الإدارة', []), url=wallet_admin_contact_url())],
            [btn(render_bot_button(
                'bot.handlers.wallet_callbacks.py:108:button', '🏠 إلغاء', []), 'main_menu')],
        ])
        await query.edit_message_text(
            compact_html(
                title=display_texts.get('deposit_title', 'إيداع عبر محفظة جيب'),
                instructions=display_texts.get('deposit_instructions',
                    'حوّل من محفظتك الشخصية المسجلة ثم أرسل رقم العملية الصحيح فقط.'),
                policy=display_texts.get('deposit_policy',
                    'لا نقبل التحويل من طرف ثالث. يجب أن يكون المرسل صاحب الحساب الشخصي المطابق لبيانات العميل.'),
                rate=format(wallet_decimal(draft['exchange_rate_yer']).normalize(), 'f'),
                minimum=int(WALLET_MIN_DEPOSIT_YER),
                beneficiary=receiver_name,
                phone=receiver_phone,
                title_icon_html=wallet_status_icon('deposit_title'),
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("wallet_deposit_ref:"):
        # Compatibility with older pending messages. The amount is no longer requested.
        deposit_id = int(data.split(":", 1)[1])
        deposit = wallet_get_deposit(deposit_id)
        if not deposit or int(deposit["telegram_user_id"]) != int(user.id):
            await query.answer("طلب الإيداع غير متاح", show_alert=True)
            return
        if deposit["status"] not in {"WAITING_REFERENCE", "WAITING_PAYMENT"}:
            await query.answer("طلب الإيداع مغلق", show_alert=True)
            return
        set_state(context, "await_wallet_deposit_reference", deposit_id=deposit_id)
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:125:message','<b>أرسل رقم العملية الآن</b>\n\n🧾 المرجع الداخلي: <code>{v0}</code>\nسيتم استخراج المبلغ الحقيقي من بيانات العملية تلقائيًا.', [f"{html(deposit['deposit_code'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:130:button','🏠 إلغاء', []), "main_menu")]]),
        )
        return

    if data == "wallet_withdraw":
        customer = DBX.get_customer_by_telegram(user.id)
        if not customer or customer["status"] != "ACTIVE":
            await query.answer("حسابك غير نشط", show_alert=True)
            return
        if not wallet_withdraw_enabled():
            await query.answer("السحب متوقف مؤقتاً", show_alert=True)
            return
        
        # إرسال رسالة انتظار أثناء جلب الحد الأدنى
        await query.edit_message_text(
            f"{wallet_status_icon('withdraw_wait_bep20')} <b>جارٍ التحقق من الرصيد وحدود السحب...</b>",
            parse_mode=ParseMode.HTML
        )
        
        state = binance_auth_state()
        if state["blocked"]:
            await query.edit_message_text(
                render_bot_text('bot.handlers.wallet_callbacks.py:151:message','⛔ السحب متوقف مؤقتًا بسبب اتصال Binance. سيُفتح تلقائيًا فور نجاح الفحص.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:153:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        available, _ = wallet_balances(user.id)
        set_state(context, "await_wallet_withdraw_amount")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:158:message','<b>💸 سحب USDT</b>\n\nرصيدك المتاح: <b>{v0} USDT</b>\nيُعرض الحد الأدنى والرسوم الحية بعد اختيار الشبكة.\nتتحمل الخدمة <b>0.01 USDT</b> من رسوم الشبكة.\n\nأرسل المبلغ المطلوب.', [f'{format_display_amount(available)}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:165:button','🏠 إلغاء', []), "main_menu")]]),
        )
        return

    if data.startswith("wallet_network:"):
        network = data.split(":", 1)[1].upper()
        draft = context.user_data.get("wallet_withdraw_draft") or {}
        if "amount" not in draft:
            await query.answer("ابدأ طلب سحب جديد", show_alert=True)
            return
        available_codes = {item["code"] for item in wallet_network_routes()}
        if network not in available_codes:
            await query.answer("الشبكة غير متاحة حالياً", show_alert=True)
            return
        await query.edit_message_text(
            f"{wallet_withdraw_icon(network)} <b>جارٍ جلب رسوم الشبكة والتحقق من حدود السحب...</b>",
            parse_mode=ParseMode.HTML,
        )
        try:
            limits = await asyncio.to_thread(binance_live_limits, network, force_refresh=True)
            live_minimum = wallet_decimal(limits["minimum"])
            system_fee = effective_network_fee(network, limits["fee"])
            charged_fee = customer_network_fee(system_fee)
            amount = wallet_decimal(draft["amount"])
            available, _ = wallet_balances(user.id)
            if amount < live_minimum:
                await query.edit_message_text(
                    f"⚠️ الحد الأدنى لهذه الشبكة {format_display_amount(live_minimum)} USDT"
                )
                return
            if amount + charged_fee > available:
                await query.edit_message_text(
                    f"⚠️ الرصيد المطلوب مع الرسوم {format_display_amount(amount + charged_fee)} USDT"
                )
                return
        except WalletUserError as exc:
            await query.edit_message_text(f"⚠️ {html(str(exc))}", parse_mode=ParseMode.HTML)
            return
        except Exception as exc:
            if is_binance_service_error(exc):
                await _notify_binance_stopped(context, reference=wallet_ref("BIN"), error=exc)
            await query.edit_message_text("⚠️ تعذر جلب رسوم الشبكة الآن. حاول مجددًا.")
            return
        draft["network_code"] = network
        draft["network_label"] = managed_network_label(network)
        draft["live_minimum"] = q8(live_minimum)
        draft["system_fee"] = q8(system_fee)
        draft["customer_fee"] = q8(charged_fee)
        context.user_data["wallet_withdraw_draft"] = draft
        set_state(context, "await_wallet_withdraw_address")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:213:message','<b>أرسل عنوان الاستلام</b>\n\n🌐 الشبكة: <b>{v0}</b>\n💸 الرسوم عليك: <b>{v1}</b>\n🎁 تتحمل الخدمة: <b>{v2} USDT</b>\n\n⚠️ أرسل عنوانًا تابعًا للشبكة المختارة فقط.', [f"{html(draft['network_label'])}",f'{html(customer_network_fee_text(system_fee))}',f"{html(format_display_amount(min(system_fee, wallet_decimal('0.01'))))}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:220:button','🏠 إلغاء', []), "main_menu")]]),
        )
        return

    if data == "wallet_withdraw_confirm":
        draft = context.user_data.get("wallet_withdraw_draft") or {}
        try:
            amount = wallet_decimal(draft["amount"])
            network = str(draft["network_code"])
            address = wallet_validate_destination(network, str(draft["address"]))
            provider = wallet_provider_for_network(network)
            limits = await asyncio.to_thread(binance_live_limits, network, force_refresh=True)
            live_minimum = wallet_decimal(limits["minimum"])
            system_fee = effective_network_fee(network, limits["fee"])
            charged_fee = customer_network_fee(system_fee)
            previous_fee = wallet_decimal(draft.get("customer_fee") or 0)
            available, _ = wallet_balances(user.id)
            if amount < live_minimum:
                raise WalletUserError(
                    f"تغير الحد الأدنى للشبكة إلى {format_display_amount(live_minimum)} USDT. ابدأ الطلب مجددًا."
                )
            if amount + charged_fee > available:
                raise WalletUserError(
                    f"رصيدك لا يغطي المبلغ والرسوم الحالية ({format_display_amount(amount + charged_fee)} USDT)."
                )
            if q8(previous_fee) != q8(charged_fee):
                draft["live_minimum"] = q8(live_minimum)
                draft["system_fee"] = q8(system_fee)
                draft["customer_fee"] = q8(charged_fee)
                context.user_data["wallet_withdraw_draft"] = draft
                await query.edit_message_text(
                    render_bot_text('bot.handlers.wallet_callbacks.py:250:message','<b>تغيرت رسوم الشبكة</b>\n\n💵 المبلغ: <b>{v0} USDT</b>\n💸 الرسوم الجديدة عليك: <b>{v1}</b>\n📦 الإجمالي المحجوز: <b>{v2} USDT</b>\n\nراجع القيمة الجديدة ثم أكد مرة أخرى.', [f'{html(format_display_amount(amount))}',f'{html(customer_network_fee_text(system_fee))}',f'{html(format_display_amount(amount + charged_fee))}']),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[btn(render_bot_button('bot.handlers.wallet_callbacks.py:258:button','✅ تأكيد بالقيمة الجديدة', []), "wallet_withdraw_confirm", "success")], [btn(render_bot_button('bot.handlers.wallet_callbacks.py:258:button','❌ إلغاء', []), "wallet_withdraw_cancel", "danger")]]
                    ),
                )
                return
            withdrawal = wallet_create_withdrawal(
                user.id,
                amount,
                network,
                address,
                provider,
                minimum_amount=live_minimum,
                customer_fee=charged_fee,
            )
            context.user_data.pop("wallet_withdraw_draft", None)
            set_state(context, None)
            await query.edit_message_text(
                (
                    f"{wallet_withdraw_icon(network)} <b>طلب السحب قيد المعالجة</b>\n\n"
                    f"🧾 <code>{html(withdrawal['withdrawal_code'])}</code>\n"
                    f"💵 <b>{html(format_display_amount(withdrawal['amount_usdt']))} USDT</b>\n"
                    f"💸 الرسوم: <b>{html(format_display_amount(withdrawal['customer_fee_usdt']))} USDT</b>\n"
                    f"🌐 {html(withdrawal['network_label'])}\n\n"
                    f"{wallet_display_text('withdraw_processing_text','تم استلام طلبك ويجري التنفيذ والتحقق من الشبكة. سيصلك إشعار بالنتيجة؛ لا تكرر الطلب.')}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:281:button','📒 سجل العمليات', []), "wallet_history")], [btn(render_bot_button('bot.handlers.wallet_callbacks.py:281:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            try:
                context.application.create_task(
                    wallet_submit_withdrawal(int(withdrawal["id"]), context, initiated_by_admin=False),
                    update=update,
                    name=f"auto-wallet-withdraw-{withdrawal['id']}",
                )
            except Exception:
                # الطلب محفوظ في الطابور؛ يلتقطه المراقب تلقائيًا.
                log.exception("Failed to schedule immediate automatic withdrawal id=%s", withdrawal["id"])
        except WalletUserError as exc:
            await query.edit_message_text(
                render_bot_text('bot.handlers.wallet_callbacks.py:293:message','❌ {v0}', [f'{html(exc)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:296:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
        except Exception as exc:
            reference = wallet_ref("ERR")
            await send_safe_wallet_error(
                query,
                context,
                reference_id=reference,
                event_type="فشل إنشاء السحب",
                exc=exc,
                telegram_user_id=user.id,
            )
        return

    if data == "wallet_withdraw_cancel":
        context.user_data.pop("wallet_withdraw_draft", None)
        set_state(context, None)
        await show_main(query, context)
        return

    # -------- الإدارة --------
    if not is_admin:
        await query.answer("للأدمن فقط", show_alert=True)
        return

    if data == "adm_wallet_panel":
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:322:message','<b>💰 نظام المحافظ الداخلية</b>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=wallet_admin_panel_kb(user.id),
        )
        return

    if data == "adm_wallet_stats":
        s = wallet_system_stats()
        coverage_note = "أدخل إعدادات المحافظ لعرض رصيد الأصول الحقيقي."
        assets_lines: list[str] = []
        if BINANCE_PROVIDER.enabled():
            try:
                binance_balance = await asyncio.to_thread(BINANCE.get_spot_free_balance, BINANCE_COIN)
                assets_lines.append(f"• Binance USDT: <b>{html(format_display_amount(binance_balance))}</b>")
            except Exception as exc:
                log.exception("Failed to read Binance balance for wallet stats admin=%s", user.id)
                assets_lines.append("• Binance USDT: تعذر القراءة")
                await send_technical_log(
                    context,
                    reference_id="WALLET-STATS",
                    event_type="فشل قراءة رصيد Binance",
                    details=traceback.format_exc(),
                    severity="WARNING",
                    telegram_user_id=user.id,
                )
        assets_text = "\n".join(assets_lines) or coverage_note
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:349:message','<b>📊 ملخص المحافظ</b>\n\n👥 المحافظ: <b>{v0}</b>\n💰 الأرصدة المتاحة: <b>{v1} USDT</b>\n🔒 الأرصدة المحجوزة: <b>{v2} USDT</b>\n📌 إجمالي الالتزامات: <b>{v3} USDT</b>\n\n📥 الإيداعات المكتملة: <b>{v4}</b> — {v5} USDT\n📤 السحوبات المكتملة: <b>{v6}</b> — {v7} USDT\n⏳ العمليات المعلقة: <b>{v8}</b>\n\n<b>الأصول الفعلية:</b>\n{v9}', [f"{s['wallets']}",f"{html(format_display_amount(s['available']))}",f"{html(format_display_amount(s['locked']))}",f"{html(format_display_amount(s['liabilities']))}",f"{s['deposit_count']}",f"{html(format_display_amount(s['deposit_usdt']))}",f"{s['withdraw_count']}",f"{html(format_display_amount(s['withdraw_usdt']))}",f"{s['pending']}",f'{assets_text}']),
            parse_mode=ParseMode.HTML,
            reply_markup=wallet_admin_panel_kb(user.id),
        )
        return

    if data == "adm_wallet_settings":
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:366:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_toggle_deposit":
        wallet_set_deposit_enabled(not wallet_deposit_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_WALLET_DEPOSIT", "settings", "wallet_deposit_enabled", str(wallet_deposit_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:372:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_toggle_withdraw":
        wallet_set_withdraw_enabled(not wallet_withdraw_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_WALLET_WITHDRAW", "settings", "wallet_withdraw_enabled", str(wallet_withdraw_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:378:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_toggle_liquidity":
        set_public_liquidity_enabled(not public_liquidity_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_PUBLIC_LIQUIDITY", "settings", "public_liquidity_enabled", str(public_liquidity_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:384:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_toggle_liquidity_alert":
        set_liquidity_alert_enabled(not liquidity_alert_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_LIQUIDITY_ALERT", "settings", "liquidity_alert_enabled", str(liquidity_alert_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:390:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_set_liquidity_alert":
        set_state(context, "await_wallet_liquidity_alert_threshold")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:395:message','أرسل حد السيولة الذي عنده يصل التنبيه للأدمن بوحدة USDT.\nمثال: <code>20</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:398:button','↩️ رجوع', []), "adm_wallet_settings")]]),
        )
        return

    if data == "adm_wallet_set_liquidity_recovery":
        set_state(context, "await_wallet_liquidity_alert_recovery")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:404:message','أرسل حد استعادة السيولة الآمنة. يجب أن يساوي حد التنبيه أو يزيد عليه.\nمثال: <code>25</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:407:button','↩️ رجوع', []), "adm_wallet_settings")]]),
        )
        return

    if data == "adm_wallet_set_display_decimals":
        set_state(context, "await_wallet_display_decimals")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:413:message','أرسل عدد الخانات العشرية المعروضة من 0 إلى 8.\nالقيمة المقترحة للعملات الرقمية: <code>4</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:416:button','↩️ رجوع', []), "adm_wallet_settings")]]),
        )
        return

    if data == "adm_wallet_toggle_withdraw_limit":
        set_withdrawal_operation_limit_enabled(not withdrawal_operation_limit_enabled())
        DBX.audit(user.id, actor_name(user), "TOGGLE_WITHDRAW_OPERATION_LIMIT", "settings", "withdrawal_operation_limit_enabled", str(withdrawal_operation_limit_enabled()))
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:423:message','{v0}', [str(wallet_admin_settings_text())]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_settings_kb())
        return

    if data == "adm_wallet_set_withdraw_limit":
        set_state(context, "await_wallet_withdraw_limit")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:428:message','أرسل الحد الأقصى الجديد لكل عملية بوحدة USDT.\nمثال: <code>100</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:431:button','↩️ رجوع', []), "adm_wallet_settings")]]),
        )
        return

    if data == "adm_wallet_set_liquidity_reserve":
        set_state(context, "await_wallet_liquidity_reserve")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:437:message','أرسل قيمة احتياطي السيولة الذي لا تريد عرضه أو إتاحته للمستخدمين.\nمثال: <code>25</code>', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:440:button','↩️ رجوع', []), "adm_wallet_settings")]]),
        )
        return

    if data == "adm_wallet_ledger":
        with DBX.connect() as con:
            rows = con.execute("SELECT * FROM wallet_ledger ORDER BY id DESC LIMIT 30").fetchall()
        body = ["<b>📒 آخر حركات المحافظ</b>"]
        for row in rows:
            body.append(
                f"• <code>{html(row['ledger_code'])}</code> | {html(row['transaction_type'])}\n"
                f"  👤 <code>{html(row['telegram_user_id'])}</code> | 💵 {html(row['amount_usdt'])} USDT | {html(row['created_at'])}"
            )
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:453:message','{v0}', [str('\n\n'.join(body)[:4000])]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data == "adm_wallet_pending":
        with DBX.connect() as con:
            withdrawals = con.execute(
                "SELECT * FROM wallet_withdrawals WHERE status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED') ORDER BY id DESC LIMIT 20"
            ).fetchall()
            deposits = con.execute(
                "SELECT * FROM wallet_deposits WHERE status IN ('WAITING_PAYMENT','PAYMENT_REVIEW','VERIFIED') ORDER BY id DESC LIMIT 20"
            ).fetchall()
        lines = ["<b>⏳ العمليات المعلقة</b>", "", "<b>السحوبات:</b>"]
        lines += [f"• <code>{html(r['withdrawal_code'])}</code> | {html(r['status'])} | {html(r['amount_usdt'])} USDT" for r in withdrawals] or ["• لا يوجد"]
        lines += ["", "<b>الإيداعات:</b>"]
        lines += [f"• <code>{html(r['deposit_code'])}</code> | {html(r['status'])} | {html(r['usdt_amount'])} USDT" for r in deposits] or ["• لا يوجد"]
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:468:message','{v0}', [str('\n'.join(lines)[:4000])]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data == "adm_wallet_adjust":
        set_state(context, "await_wallet_admin_target")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:473:message','أرسل Telegram ID للمستخدم الذي تريد تعديل رصيده.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:475:button','↩️ رجوع', []), "adm_wallet_panel")]]),
        )
        return

    if data == "adm_wallet_export":
        paths = wallet_export_csv()
        for path in paths:
            with path.open("rb") as fh:
                await context.bot.send_document(user.id, document=fh, filename=path.name)
        DBX.audit(user.id, actor_name(user), "EXPORT_WALLET_CSV", "wallet", "all")
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:485:message','✅ تم إرسال ملفات المحافظ والحركات والسحوبات.', []), reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data == "adm_payout_wallet":
        lines = ["<b>👛 حالة Binance</b>", ""]
        state = binance_auth_state()
        routes = wallet_network_routes()
        lines.append(f"• السحب: <b>{'جاهز' if not state['blocked'] and routes and BINANCE_PROVIDER.enabled() else 'متوقف'}</b>")
        lines.append(
            "• الشبكات المفعلة: <b>" +
            (html("، ".join(str(item["label"]) for item in routes)) if routes else "لا يوجد") +
            "</b>"
        )
        lines.append(f"• IP: <code>{html(state.get('egress_ip') or 'UNKNOWN')}</code>")
        if state.get("last_error"):
            lines.append(f"• السبب: <code>{html(state['last_error'])}</code>")
        if not state["blocked"] and BINANCE_PROVIDER.enabled():
            try:
                balance = await asyncio.to_thread(BINANCE.get_spot_free_balance, BINANCE_COIN)
                lines.append(f"• USDT: <b>{html(format_display_amount(balance))}</b>")
            except Exception as exc:
                lines.append(f"• خطأ: <code>{html(exc)}</code>")
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:507:message','{v0}', [str('\n'.join(lines))]), parse_mode=ParseMode.HTML, reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data.startswith("adm_wallet_deposit_view:"):
        deposit_id = int(data.split(":", 1)[1])
        row = wallet_get_deposit(deposit_id)
        if not row:
            await query.answer("غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:516:message','<b>📄 تفاصيل الإيداع</b>\n\n🧾 المرجع الداخلي: <code>{v0}</code>\n👤 Telegram ID: <code>{v1}</code>\n🔢 رقم العملية: <code>{v2}</code>\n💴 المبلغ المستخرج: <b>{v3} ريال</b>\n💲 السعر المثبت: <b>{v4}</b>\n💵 الرصيد: <b>{v5} USDT</b>\n👤 اسم المرسل: <b>{v6}</b>\n📱 الرقم الأساسي: <code>{v7}</code>\n📞 الرقم البديل: <code>{v8}</code>\n🕒 وقت العملية: <code>{v9}</code>\n🔎 مصدر المبلغ: <code>{v10}</code>\n📌 الحالة: <b>{v11}</b>\n❗ المراجعة/الخطأ: {v12}', [f"{html(row['deposit_code'])}",f"{html(row['telegram_user_id'])}",f"{html(row['bank_transaction_id'] or '—')}",f"{html(format_yer_amount(row['fiat_amount_yer']))}",f"{html(row['exchange_rate_yer'])}",f"{html(row['usdt_amount'])}",f"{html(row['payer_name'] or row['payer_display'] or '—')}",f"{html(row['payer_phone'] or '—')}",f"{html(row['payer_phone_alt'] or '—')}",f"{html(row['payment_time'] or '—')}",f"{html(row['amount_source'] or '—')}",f"{html(row['status'])}",f"{html(row['review_reason'] or row['error_message'] or '—')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=wallet_deposit_admin_kb(deposit_id),
        )
        return

    if data.startswith("adm_wallet_deposit_approve:"):
        deposit_id = int(data.split(":", 1)[1])
        row = wallet_get_deposit(deposit_id)
        if not row or row["status"] not in {"PAYMENT_REVIEW", "VERIFIED", "WAITING_PAYMENT"}:
            await query.answer("الحالة لا تسمح بالاعتماد", show_alert=True)
            return
        completed = wallet_credit_deposit(deposit_id, admin_id=user.id, notes="اعتماد يدوي من الإدارة")
        DBX.audit(user.id, actor_name(user), "APPROVE_WALLET_DEPOSIT", "wallet_deposit", completed["deposit_code"])
        try:
            available, _ = wallet_balances(completed["telegram_user_id"])
            await context.bot.send_message(
                completed["telegram_user_id"],
                render_bot_text('bot.handlers.wallet_callbacks.py:546:message','✅ تمت مراجعة إيداعك وإضافة الرصيد.\n\n➕ <b>{v0} USDT</b>\n💰 الرصيد المتاح: <b>{v1} USDT</b>', [f"{format_display_amount(completed['usdt_amount'])}",f'{format_display_amount(available)}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:552:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
        except Exception:
            log.exception(
                "Failed to notify user after admin deposit approval deposit=%s user=%s",
                completed["deposit_code"],
                completed["telegram_user_id"],
            )
        log.info(
            "Deposit manually approved admin=%s deposit=%s user=%s amount=%s",
            user.id,
            completed["deposit_code"],
            completed["telegram_user_id"],
            completed["usdt_amount"],
        )
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:567:message','✅ تم اعتماد الإيداع وإضافة الرصيد.', []), reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data.startswith("adm_wallet_deposit_reject:"):
        deposit_id = int(data.split(":", 1)[1])
        row = wallet_get_deposit(deposit_id)
        if not row or row["status"] == "COMPLETED":
            await query.answer("لا يمكن رفض هذه العملية", show_alert=True)
            return
        with DBX.connect() as con:
            con.execute("UPDATE wallet_deposits SET status='REJECTED', review_reason='ADMIN_REJECTED', updated_at=? WHERE id=?", (now_str(), deposit_id))
            con.commit()
        DBX.audit(user.id, actor_name(user), "REJECT_WALLET_DEPOSIT", "wallet_deposit", row["deposit_code"])
        try:
            await context.bot.send_message(row["telegram_user_id"], render_bot_text('bot.handlers.wallet_callbacks.py:581:message','❌ تم رفض الإيداع بعد المراجعة.\n🧾 <code>{v0}</code>', [f"{html(row['deposit_code'])}"]), parse_mode=ParseMode.HTML)
        except Exception:
            log.exception(
                "Failed to notify user after admin deposit rejection deposit=%s user=%s",
                row["deposit_code"],
                row["telegram_user_id"],
            )
        log.warning(
            "Deposit manually rejected admin=%s deposit=%s user=%s",
            user.id,
            row["deposit_code"],
            row["telegram_user_id"],
        )
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:594:message','❌ تم رفض الإيداع.', []), reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data.startswith("adm_wallet_withdraw_view:"):
        withdrawal_id = int(data.split(":", 1)[1])
        row = wallet_get_withdrawal(withdrawal_id)
        if not row:
            await query.answer("غير موجود", show_alert=True)
            return
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:603:message','{v0}', [str(wallet_withdraw_detail_text(row))]), parse_mode=ParseMode.HTML, reply_markup=wallet_withdraw_admin_kb(withdrawal_id))
        return

    if data.startswith("adm_wallet_check:"):
        withdrawal_id = int(data.split(":", 1)[1])
        row = wallet_get_withdrawal(withdrawal_id)
        if not row:
            await query.answer("غير موجود", show_alert=True)
            return
        await monitor_wallet_withdrawals(context)
        row = wallet_get_withdrawal(withdrawal_id)
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:614:message','{v0}', [str(wallet_withdraw_detail_text(row))]), parse_mode=ParseMode.HTML, reply_markup=wallet_withdraw_admin_kb(withdrawal_id))
        return

    if data.startswith("adm_wallet_release_confirm:"):
        withdrawal_id = int(data.split(":", 1)[1])
        row = wallet_get_withdrawal(withdrawal_id)
        if not row:
            await query.answer("غير موجود", show_alert=True)
            return
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:623:message','⚠️ تأكد أولاً أن المعاملة لم تُرسل ولم تنجح على الشبكة.\n\nهل تريد إعادة <b>{v0} USDT</b> إلى المستخدم؟', [f"{format_display_amount(row['amount_usdt'])}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:629:button','✅ تأكيد الإرجاع', []), f"adm_wallet_release_do:{withdrawal_id}", "danger")],
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:630:button','❌ إلغاء', []), f"adm_wallet_withdraw_view:{withdrawal_id}")],
                ]
            ),
        )
        return

    if data.startswith("adm_wallet_release_do:"):
        withdrawal_id = int(data.split(":", 1)[1])
        row = wallet_get_withdrawal(withdrawal_id)
        if not row:
            await query.answer("غير موجود", show_alert=True)
            return
        released = wallet_release_withdrawal(withdrawal_id, error_code="ADMIN_RELEASE", error_message="Released by admin", admin_id=user.id)
        DBX.audit(user.id, actor_name(user), "RELEASE_WALLET_WITHDRAW", "wallet_withdrawal", released["withdrawal_code"])
        try:
            await context.bot.send_message(
                released["telegram_user_id"],
                render_bot_text('bot.handlers.wallet_callbacks.py:645:message','↩️ تمت إعادة مبلغ السحب إلى رصيدك المتاح بعد مراجعة الإدارة.\n\n🧾 <code>{v0}</code>', [f"{html(released['withdrawal_code'])}"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.exception(
                "Failed to notify user after withdrawal release code=%s user=%s",
                released["withdrawal_code"],
                released["telegram_user_id"],
            )
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:657:message','✅ تمت إعادة الرصيد.', []), reply_markup=wallet_admin_panel_kb(user.id))
        return

    if data == "adm_channels_settings":
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:661:message','<b>📡 إعدادات القنوات</b>', []), parse_mode=ParseMode.HTML, reply_markup=channels_settings_kb())
        return

    if data == "adm_technical_channel":
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:665:message','<b>🚨 قناة الأخطاء والردود التقنية</b>\n\nالقناة الحالية: <b>{v0}</b>\nالمعرف: <code>{v1}</code>\n\nترسل إليها أخطاء API وردود الشبكات والعمليات التي تحتاج مراجعة. لا ترسل المفاتيح أو الأسرار.', [f"{html(DBX.get_setting('technical_channel_title') or 'غير محددة')}",f"{html(DBX.get_setting('technical_channel_id') or '—')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=technical_channel_kb(),
        )
        return

    if data == "adm_technical_channel_set":
        set_state(context, "await_technical_channel")
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:677:message','أرسل Channel ID أو @username لقناة الردود التقنية بعد إضافة البوت مشرفاً فيها.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.handlers.wallet_callbacks.py:679:button','↩️ رجوع', []), "adm_technical_channel")]]),
        )
        return

    if data == "adm_technical_channel_test":
        await send_technical_log(
            context,
            reference_id="TECH-TEST",
            event_type="اختبار قناة الردود",
            details="هذه رسالة اختبار. تم ربط قناة الأخطاء والردود التقنية بنجاح.",
            severity="INFO",
            telegram_user_id=user.id,
        )
        await query.answer("تم إرسال رسالة الاختبار", show_alert=True)
        return

    if data == "adm_technical_channel_clear":
        if callable(globals().get("set_admin_channel")):
            set_admin_channel("")
        else:
            for key in ("technical_channel_id", "technical_channel_title"):
                DBX.set_setting(key, "")
        DBX.set_setting("technical_channel_username", "")
        DBX.audit(user.id, actor_name(user), "CLEAR_TECHNICAL_CHANNEL", "settings", "technical_channel")
        await query.edit_message_text(render_bot_text('bot.handlers.wallet_callbacks.py:703:message','🗑️ تم حذف قناة الردود التقنية.', []), reply_markup=technical_channel_kb())
        return

    if data == "adm_settings_overview":
        await query.edit_message_text(
            render_bot_text('bot.handlers.wallet_callbacks.py:707:message','<b>⚙️ الإعدادات</b>\n\n💲 سعر 1 USDT: <b>{v0} ريال</b>\n📥 الإيداع: <b>{v1}</b>\n📤 السحب: <b>{v2}</b>\n💵 أقل سحب: <b>ديناميكي من </b>\n🛡️ رسوم العميل: <b>0.00$</b>\n📺 الإشعارات: <b>{v3}</b>\n🚨 الردود التقنية: <b>{v4}</b>', [f'{html(format_display_amount(DBX.get_rate()))}',f"{('مفعل' if wallet_deposit_enabled() else 'متوقف')}",f"{('مفعل' if wallet_withdraw_enabled() else 'متوقف')}",f"{html(DBX.get_setting('notification_channel_title') or 'غير محددة')}",f"{html(DBX.get_setting('technical_channel_title') or 'غير محددة')}"]),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:719:button','💲 تعديل السعر', []), "adm_set_rate", "primary")],
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:720:button','💰 نظام المحافظ', []), "adm_wallet_panel", "primary")],
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:721:button','📡 إعدادات القنوات', []), "adm_channels_settings")],
                    [btn(render_bot_button('bot.handlers.wallet_callbacks.py:722:button','🏠 لوحة الأدمن', []), "admin_panel")],
                ]
            ),
        )
        return


_export_globals(globals())

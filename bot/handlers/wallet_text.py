"""Wallet text states and secure technical-error routing.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.ui.status_icons import wallet_withdraw_icon, wallet_display_text
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- اعتراض حالات النص ----------------


_wallet_legacy_on_text = on_text

_wallet_active_verifications: set[int] = set()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_bot_user(user)
    state, payload = current_state(context)
    text_value = (update.effective_message.text or "").strip()

    try:

        if state == "await_wallet_deposit_reference":
            deposit_id = int(payload.get("deposit_id") or 0)
            normalized_reference = text_value.translate(ARABIC_DIGITS).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", normalized_reference):
                raise WalletUserError("رقم العملية غير صالح. أرسل رقم العملية فقط دون مسافات أو صور.")
            if deposit_id <= 0:
                locked_rate = payload.get("exchange_rate_yer")
                if not locked_rate:
                    set_state(context, None)
                    raise WalletUserError("انتهت جلسة الإيداع. اضغط زر الإيداع من جديد.")
                deposit = wallet_create_deposit(
                    user.id,
                    exchange_rate_yer=wallet_decimal(locked_rate),
                )
                deposit_id = int(deposit["id"])
                set_state(
                    context,
                    "await_wallet_deposit_reference",
                    deposit_id=deposit_id,
                    exchange_rate_yer=locked_rate,
                )
            log.info(
                "Deposit reference received user=%s deposit_id=%s",
                user.id,
                deposit_id,
            )
            # A single user may have only one financial verification in flight.
            if user.id in _wallet_active_verifications:
                await update.effective_message.reply_text(
                    "⏳ رقم العملية السابق لا يزال قيد التحقق. يرجى الانتظار وعدم الإرسال مجدداً."
                )
                return
            _wallet_active_verifications.add(user.id)
            set_state(context, "await_wallet_deposit_processing", deposit_id=deposit_id,
                      exchange_rate_yer=payload.get("exchange_rate_yer", ""))

            async def _background_deposit_verification():
                try:
                    completed = await wallet_verify_deposit_reference(
                        update, context, deposit_id, normalized_reference,
                    )
                    current, _ = current_state(context)
                    if current == "await_wallet_deposit_processing":
                        if completed:
                            set_state(context, None)
                        else:
                            set_state(context, "await_wallet_deposit_reference", deposit_id=deposit_id,
                                      exchange_rate_yer=payload.get("exchange_rate_yer", ""))
                except Exception as exc:
                    log.exception("Background deposit verification failed user=%s deposit=%s",
                                  user.id, deposit_id)
                    current, _ = current_state(context)
                    if current == "await_wallet_deposit_processing":
                        set_state(context, "await_wallet_deposit_reference", deposit_id=deposit_id,
                                  exchange_rate_yer=payload.get("exchange_rate_yer", ""))
                    try:
                        await update.effective_message.reply_text(
                            "⚠️ تعذر إكمال الفحص حالياً. لم نعتمد أي رصيد غير مؤكد. "
                            "احتفظ برقم العملية وأعد المحاولة لاحقاً."
                        )
                    except Exception:
                        pass
                finally:
                    _wallet_active_verifications.discard(user.id)
            try:
                context.application.create_task(
                    _background_deposit_verification(),
                    update=update,
                    name=f"verify-wallet-deposit-{deposit_id}",
                )
            except Exception:
                _wallet_active_verifications.discard(user.id)
                set_state(context, "await_wallet_deposit_reference", deposit_id=deposit_id,
                          exchange_rate_yer=payload.get("exchange_rate_yer", ""))
                raise
            return

        if state == "await_wallet_deposit_processing":
            await update.effective_message.reply_text(
                "⏳ يجري فحص رقم العملية السابق الآن. لا ترسل أرقاماً إضافية "
                "حتى يظهر لك تأكيد النتيجة أو رسالة تعذر التحقق."
            )
            return

        if state == "await_wallet_withdraw_amount":
            amount = wallet_decimal(parse_decimal(text_value))
            if amount < WALLET_MIN_WITHDRAW_USDT:
                raise WalletUserError(f"الحد الأدنى للسحب هو {format_display_amount(WALLET_MIN_WITHDRAW_USDT)} USDT.")
            available, _ = wallet_balances(user.id)
            if amount > available:
                raise WalletUserError(f"رصيدك غير كافٍ. المتاح {format_display_amount(available)} USDT.")
            context.user_data["wallet_withdraw_draft"] = {"amount": q8(amount)}
            set_state(context, None)
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:69:message','<b>اختر شبكة السحب</b>\n\n💵 المبلغ: <b>{v0} USDT</b>\nسيتم جلب الرسوم الحية للشبكة المختارة، وتتحمل الخدمة منها <b>0.01 USDT</b>.', [f'{html(format_display_amount(amount))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_network_kb(),
            )
            return

        if state == "await_wallet_withdraw_address":
            draft = context.user_data.get("wallet_withdraw_draft") or {}
            network = str(draft.get("network_code") or "")
            if not network:
                raise WalletUserError("اختر الشبكة من جديد.")
            address_wait = await update.effective_message.reply_text(
                f"{wallet_withdraw_icon(network)} <b>{wallet_display_text('withdraw_address_wait','جارٍ التحقق من صحة عنوان الاستلام والشبكة، يرجى الانتظار.')}</b>",
                parse_mode=ParseMode.HTML,
            )
            try:
                address = wallet_validate_destination(network, text_value)
            except WalletUserError:
                await address_wait.edit_text('❌ عنوان الاستلام غير صالح للشبكة المختارة. راجعه ثم أعد إرساله.')
                raise
            draft["address"] = address
            context.user_data["wallet_withdraw_draft"] = draft
            set_state(context, None)
            label = str(draft.get("network_label") or managed_network_label(network))
            system_fee = wallet_decimal(draft.get("system_fee") or draft.get("live_fee") or 0)
            customer_fee = wallet_decimal(draft.get("customer_fee") or 0)
            total = wallet_decimal(draft["amount"]) + customer_fee
            await address_wait.edit_text(
                render_bot_text('bot.handlers.wallet_text.py:91:message','<b>تأكيد السحب</b>\n\n💵 المبلغ: <b>{v0} USDT</b>\n🌐 الشبكة: <b>{v1}</b>\n📬 العنوان: <code>{v2}</code>\n💸 الرسوم عليك: <b>{v3}</b>\n📦 الإجمالي المحجوز: <b>{v4} USDT</b>\n\nراجع الشبكة والعنوان جيداً. التحويلات على البلوكشين لا يمكن عكسها بعد نجاحها.', [f"{html(draft['amount'])}",f'{html(label)}',f'{html(address)}',f'{html(customer_network_fee_text(system_fee))}',f'{html(format_display_amount(total))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(render_bot_button('bot.handlers.wallet_text.py:102:button','✅ تأكيد السحب', []), "wallet_withdraw_confirm", "success")],
                        [btn(render_bot_button('bot.handlers.wallet_text.py:103:button','❌ إلغاء', []), "wallet_withdraw_cancel", "danger")],
                    ]
                ),
            )
            return

        if state == "await_wallet_withdraw_limit":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_settings"):
                raise WalletUserError("لا تملك صلاحية إعدادات المحفظة.")
            amount = wallet_decimal(parse_decimal(text_value))
            set_withdrawal_operation_limit(amount)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "SET_WITHDRAW_OPERATION_LIMIT", "settings", "withdrawal_operation_limit_usdt", q8(amount))
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:116:message','✅ تم ضبط الحد الأقصى لكل عملية على <b>{v0} USDT</b>.', [f'{html(format_display_amount(amount))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_settings_kb(),
            )
            return

        if state == "await_wallet_liquidity_reserve":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_settings"):
                raise WalletUserError("لا تملك صلاحية إعدادات المحفظة.")
            amount = wallet_decimal(parse_decimal(text_value))
            set_public_liquidity_reserve(amount)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "SET_PUBLIC_LIQUIDITY_RESERVE", "settings", "public_liquidity_reserve_usdt", q8(amount))
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:130:message','✅ تم ضبط احتياطي السيولة على <b>{v0} USDT</b>.', [f'{html(format_display_amount(amount))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_settings_kb(),
            )
            return

        if state == "await_wallet_display_decimals":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_settings"):
                raise WalletUserError("لا تملك صلاحية إعدادات المحفظة.")
            try:
                decimals = set_display_decimals(text_value)
            except ValueError as exc:
                raise WalletUserError(str(exc)) from exc
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "SET_DISPLAY_DECIMALS", "settings", "display_decimals", str(decimals))
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:146:message','✅ تم ضبط عرض المبالغ على <b>{v0}</b> خانات عشرية.', [f'{decimals}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_settings_kb(),
            )
            return

        if state == "await_wallet_liquidity_alert_threshold":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_settings"):
                raise WalletUserError("لا تملك صلاحية إعدادات المحفظة.")
            amount = wallet_decimal(parse_decimal(text_value))
            set_liquidity_alert_threshold(amount)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "SET_LIQUIDITY_ALERT_THRESHOLD", "settings", "liquidity_alert_threshold_usdt", q8(amount))
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:160:message','✅ تم ضبط حد تنبيه السيولة على <b>{v0} USDT</b>.', [f'{html(format_display_amount(amount))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_settings_kb(),
            )
            return

        if state == "await_wallet_liquidity_alert_recovery":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_settings"):
                raise WalletUserError("لا تملك صلاحية إعدادات المحفظة.")
            amount = wallet_decimal(parse_decimal(text_value))
            set_liquidity_alert_recovery(amount)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "SET_LIQUIDITY_ALERT_RECOVERY", "settings", "liquidity_alert_recovery_usdt", q8(amount))
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:174:message','✅ تم ضبط حد استعادة السيولة على <b>{v0} USDT</b>.', [f'{html(format_display_amount(amount))}']),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_settings_kb(),
            )
            return

        if state == "await_wallet_admin_target":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_adjust"):
                raise WalletUserError("لا تملك صلاحية تعديل الأرصدة.")
            if not text_value.isdigit():
                raise WalletUserError("أرسل Telegram ID رقمياً.")
            target_id = int(text_value)
            customer = DBX.get_customer_by_telegram(target_id)
            if not customer:
                raise WalletUserError("لا يوجد عميل مربوط بهذا Telegram ID.")
            available, locked = wallet_balances(target_id)
            context.user_data["wallet_adjust_target"] = target_id
            set_state(context, "await_wallet_admin_amount")
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:193:message','العميل: <b>{v0}</b>\nالمتاح: <b>{v1}</b> | المحجوز: <b>{v2}</b>\n\nأرسل قيمة التعديل بالدولار.\nمثال إضافة: <code>10</code>\nمثال خصم: <code>-5</code>', [f"{html(customer['full_name'])}",f'{html(format_display_amount(available))}',f'{html(format_display_amount(locked))}']),
                parse_mode=ParseMode.HTML,
            )
            return

        if state == "await_wallet_admin_amount":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_adjust"):
                raise WalletUserError("لا تملك صلاحية تعديل الأرصدة.")
            amount = wallet_decimal(parse_decimal(text_value))
            context.user_data["wallet_adjust_amount"] = q8(amount)
            set_state(context, "await_wallet_admin_reason")
            await update.effective_message.reply_text(render_bot_text('bot.handlers.wallet_text.py:209:message','أرسل سبب التعديل ليحفظ في سجل المراجعة.', []))
            return

        if state == "await_wallet_admin_reason":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "wallet_adjust"):
                raise WalletUserError("لا تملك صلاحية تعديل الأرصدة.")
            if len(text_value) < 3:
                raise WalletUserError("اكتب سبباً واضحاً من 3 أحرف على الأقل.")
            target_id = int(context.user_data.pop("wallet_adjust_target"))
            amount = wallet_decimal(context.user_data.pop("wallet_adjust_amount"))
            row = wallet_admin_adjust(target_id, amount, admin_id=user.id, reason=text_value)
            set_state(context, None)
            DBX.audit(user.id, actor_name(user), "ADJUST_WALLET_BALANCE", "wallet", target_id, f"amount={amount}; reason={text_value}")
            await update.effective_message.reply_text(
                render_bot_text('bot.handlers.wallet_text.py:222:message','✅ تم تنفيذ التعديل وتسجيله.\n\nالرصيد المتاح الجديد: <b>{v0} USDT</b>', [f"{html(format_display_amount(row['available_balance']))}"]),
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_admin_panel_kb(user.id),
            )
            await flush_wallet_notification_outbox(context.bot, telegram_user_id=target_id)
            return

        if state == "await_technical_channel":
            if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "technical_channel_manage"):
                raise WalletUserError("لا تملك صلاحية إدارة قناة الردود.")
            identifier, _ = _parse_channel_input(text_value)
            chat = await context.bot.get_chat(identifier)
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if _member_status_value(member) not in {"administrator", "creator", "owner"}:
                raise WalletUserError("يجب إضافة البوت مشرفاً في القناة.")
            await context.bot.send_message(chat.id, render_bot_text('bot.handlers.wallet_text.py:239:message','✅ تم ربط هذه القناة بالأخطاء والردود التقنية.', []))
            title = getattr(chat, "title", None) or str(chat.id)
            if callable(globals().get("set_admin_channel")):
                set_admin_channel(str(chat.id), title)
            else:
                DBX.set_setting("technical_channel_id", str(chat.id))
                DBX.set_setting("technical_channel_title", title)
            DBX.set_setting("technical_channel_username", getattr(chat, "username", None) or "")
            DBX.set_setting("technical_channel_enabled", "1")
            DBX.audit(user.id, actor_name(user), "SET_TECHNICAL_CHANNEL", "settings", chat.id)
            set_state(context, None)
            await update.effective_message.reply_text(render_bot_text('bot.handlers.wallet_text.py:250:message','✅ تم حفظ قناة الردود التقنية.', []), reply_markup=technical_channel_kb())
            return

    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(render_bot_text('bot.handlers.wallet_text.py:254:message','❌ القيمة غير صالحة. أعد الإرسال.', []))
        return
    except WalletUserError as exc:
        await update.effective_message.reply_text(render_bot_text('bot.handlers.wallet_text.py:257:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        reference = wallet_ref("ERR")
        await update.effective_message.reply_text(render_bot_text('bot.handlers.wallet_text.py:261:message','{v0}', [str(wallet_user_status_message(reference))]), parse_mode=ParseMode.HTML)
        await send_technical_log(
            context,
            reference_id=reference,
            event_type=f"خطأ حالة نص: {state}",
            details=f"{type(exc).__name__}: {exc}",
            severity="ERROR",
            telegram_user_id=user.id,
        )
        log.exception("wallet text handler failed state=%s", state)
        return

    await _wallet_legacy_on_text(update, context)




_export_globals(globals())

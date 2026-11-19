"""Live network limits and customer withdrawal amount validation.

Provider-specific names stay in administrator/technical logs only. Customer
screens use neutral labels such as "مزود السحب" and "السيولة المتوفرة".
"""
from __future__ import annotations

import asyncio
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals

_seed_globals(globals())

_dynamic_legacy_on_callback = on_callback
_dynamic_legacy_on_message = on_message


def binance_live_limits(network_code: str, *, force_refresh: bool = True) -> dict[str, Any]:
    """Read current provider network rules while returning neutral user errors."""
    network = str(network_code or "").upper()
    if not BINANCE_PROVIDER.enabled():
        raise WalletTechnicalError("خدمة مزود السحب غير مفعلة أو بيانات API غير مكتملة.")
    info = BINANCE.get_coin_config(BINANCE_COIN, force_refresh=force_refresh)
    if not info:
        raise WalletTechnicalError("تعذر قراءة إعدادات عملة السحب من المزود.")
    if not info.get("withdrawAllEnable", False):
        raise WalletUserError("السحب متوقف حاليًا لدى مزود السيولة.")
    net: dict[str, Any] = {}
    for item in info.get("networkList", []) or []:
        if str(item.get("network", "")).upper() == network:
            net = item
            break
    if not net:
        raise WalletUserError(f"الشبكة {network} غير متاحة حاليًا.")
    if not net.get("withdrawEnable", False):
        raise WalletUserError(net.get("withdrawDesc") or "السحب متوقف مؤقتًا على هذه الشبكة.")
    if net.get("busy", False):
        raise WalletUserError("شبكة السحب مشغولة حاليًا. حاول لاحقًا.")
    return {
        "network": network,
        "label": NETWORK_LOOKUP.get(
            network, BEP20_NETWORK_LABEL if network == BEP20_NETWORK_CODE else network
        ),
        "minimum": wallet_decimal(net.get("withdrawMin") or 0),
        "fee": wallet_decimal(net.get("withdrawFee") or 0),
        "withdraw_integer_multiple": str(net.get("withdrawIntegerMultiple") or ""),
        "raw": net,
    }


def _withdrawal_limit_lines(liquidity: dict[str, Any] | None = None) -> list[str]:
    lines: list[str] = []
    if withdrawal_operation_limit_enabled():
        lines.append(
            render_bot_text('fragment.bot.services.dynamic_withdrawals.py:_withdrawal_limit_lines:62','📈 الحد الأقصى للسحب في كل عملية: <b>{v0} USDT</b>',[f'{html(format_display_amount(effective_withdrawal_ceiling()))}'])
        )
    if public_liquidity_enabled() and liquidity:
        if liquidity.get("available_known"):
            stale = " (آخر قيمة متاحة)" if liquidity.get("stale") else ""
            lines.append(
                f"💧 {html(liquidity.get('label') or PUBLIC_LIQUIDITY_LABEL)}: "
                f"<b>{html(liquidity.get('available_display') or '0')} USDT</b>{stale}"
            )
        else:
            lines.append(render_bot_text('fragment.bot.services.dynamic_withdrawals.py:_withdrawal_limit_lines:72','💧 {v0}: <b>غير متاحة مؤقتًا</b>',[f'{html(PUBLIC_LIQUIDITY_LABEL)}']))
    return lines


async def _read_liquidity(*, force_refresh: bool = False) -> dict[str, Any] | None:
    if not public_liquidity_enabled():
        return None
    return await asyncio.to_thread(get_public_liquidity, force_refresh=force_refresh)


async def _withdrawal_minimums_now(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async def read(route: dict[str, Any]) -> dict[str, Any]:
        limits = await asyncio.wait_for(
            asyncio.to_thread(binance_live_limits, route["code"], force_refresh=False),
            timeout=8,
        )
        return {
            "code": limits["network"],
            "label": route.get("label") or limits["label"],
            "minimum": limits["minimum"],
        }

    results = await asyncio.gather(*(read(route) for route in routes), return_exceptions=True)
    return [item for item in results if isinstance(item, dict)]


async def _start_amount_entry(
    query: Any, context: ContextTypes.DEFAULT_TYPE, network: str
) -> None:
    try:
        limits_task = asyncio.to_thread(binance_live_limits, network, force_refresh=True)
        liquidity_task = _read_liquidity(force_refresh=False)
        limits, liquidity = await asyncio.gather(limits_task, liquidity_task)
    except WalletUserError as exc:
        await query.edit_message_text(
            render_bot_text('bot.services.dynamic_withdrawals.py:105:message','❌ {v0}', [f'{html(exc)}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:108:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise
        return
    except Exception as exc:
        reference = wallet_ref("BIN")
        if is_binance_service_error(exc):
            await _notify_binance_stopped(context, reference=reference, error=exc)
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:119:message','⛔ السحب متوقف مؤقتًا بسبب اتصال Binance. سيُفتح تلقائيًا فور نجاح الفحص.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:121:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
        else:
            await send_safe_wallet_error(
                query, context, reference_id=reference, event_type="فشل قراءة حدود Binance",
                exc=exc, telegram_user_id=query.from_user.id,
            )
        return

    available, _ = wallet_balances(query.from_user.id)
    context.user_data["wallet_withdraw_draft"] = {"network_code": limits["network"]}
    set_state(context, "await_wallet_dynamic_withdraw_amount", network_code=limits["network"])
    extra = "\n".join(_withdrawal_limit_lines(liquidity))
    if extra:
        extra = extra + "\n"
    actual_fee = effective_network_fee(limits["network"], limits["fee"])
    charged_fee = customer_network_fee(actual_fee)
    await query.edit_message_text(
        render_bot_text('bot.services.dynamic_withdrawals.py:138:message','<b>💸 سحب USDT</b>\n\n🌐 الشبكة: <b>{v0}</b>\n💰 الرصيد المتاح لديك: <b>{v1} USDT</b>\n📉 الحد الأدنى الحالي للشبكة: <b>{v2} USDT</b>\n{v3}💸 رسوم الشبكة الفعلية: <b>{v4} USDT</b>\n🎁 الرسوم على العميل بعد مساهمة البوت بـ0.01 USDT: <b>{v5}</b>\n💳 الرسوم المضافة إلى مبلغ السحب: <b>{v6} USDT</b>\n\nأرسل المبلغ المطلوب. سيعيد النظام التحقق من الحدود والسيولة قبل التنفيذ.', [f"{html(limits['label'])}",f'{html(format_display_amount(available))}',f"{html(format_display_amount(limits['minimum']))}",f'{extra}',f'{html(format_display_amount(actual_fee))}',f'{html(customer_network_fee_text(actual_fee))}',f'{html(format_display_amount(charged_fee))}']),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:149:button','🏠 إلغاء', []), "main_menu")]]),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    if data == "wallet_withdraw":
        await safe_query_answer(query)
        customer = DBX.get_customer_by_telegram(user.id)
        if not customer or customer["status"] != "ACTIVE":
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:162:message','⛔ حسابك غير نشط.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:164:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        if not wallet_withdraw_enabled():
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:168:message','⛔ السحب متوقف مؤقتًا.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:170:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        state = binance_auth_state()
        if state["blocked"]:
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:175:message','⛔ السحب متوقف مؤقتًا بسبب اتصال Binance. سيُفتح تلقائيًا فور نجاح الفحص.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:177:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        await query.edit_message_text(
            render_bot_text('bot.services.dynamic_withdrawals.py:180:message','⏳ <b>جاري جلب الرصيد والحد الأدنى الحالي...</b>', []),
            parse_mode=ParseMode.HTML,
        )
        routes = [item for item in wallet_network_routes() if item.get("provider") == "BINANCE"]
        minimums = await _withdrawal_minimums_now(routes)
        if not minimums:
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:187:message','⚠️ تعذر جلب الحد الأدنى للسحب حاليًا. حاول مرة أخرى بعد قليل.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:189:button','🔄 إعادة المحاولة', []), "wallet_withdraw"), btn(render_bot_button('bot.services.dynamic_withdrawals.py:189:button','🏠 الرئيسية', []), "main_menu")]]),
            )
            return
        available, _ = wallet_balances(user.id)
        if len(minimums) == 1:
            minimum_body = f"     💲 {html(format_display_amount(minimums[0]['minimum']))} USDT"
        else:
            minimum_body = "\n".join(
                f"• {html(item['label'])}: <b>{html(format_display_amount(item['minimum']))} USDT</b>"
                for item in minimums
            )
        await query.edit_message_text(
            render_bot_text('bot.services.dynamic_withdrawals.py:200:message','<b>💸 سحب USDT</b>\n\n💰 رصيدك المتاح: <b>{v0} USDT</b>\n\n━━━━━━━━━━━━━━━━━━\n<b>⚠️ الحد الأدنى للسحب</b>\n{v1}\n<i>(هذا المبلغ قابل للتغيير حسب الشبكة)</i>\n━━━━━━━━━━━━━━━━━━\n\n<b>⚠️ تنبيه مهم قبل السحب</b>\n\nيجب أن يطابق عنوان الاستلام الشبكة التي ستختارها تمامًا. إدخال عنوان خاطئ أو عنوان من شبكة مختلفة قد يؤدي إلى فقدان كامل للمبلغ، ولا تتحمل الإدارة مسؤولية العنوان أو الشبكة التي يدخلها المستخدم.\n\nتابع فقط بعد التأكد من عنوانك وشبكتك.', [f'{html(format_display_amount(available))}',f'{minimum_body}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(render_bot_button('bot.services.dynamic_withdrawals.py:216:button','✅ قرأت التنبيه وأوافق', []), "wallet_withdraw_accept_disclaimer", "success")],
                    [btn(render_bot_button('bot.services.dynamic_withdrawals.py:217:button','🎥 كيف أستخرج عنوان الشبكة؟', []), "help_network_address")],
                    [btn(render_bot_button('bot.services.dynamic_withdrawals.py:218:button','🏠 إلغاء', []), "main_menu")],
                ]
            ),
        )
        return

    if data == "wallet_withdraw_accept_disclaimer":
        await safe_query_answer(query)
        routes = [item for item in wallet_network_routes() if item.get("provider") == "BINANCE"]
        if not routes:
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:228:message','⚠️ لا توجد شبكة سحب جاهزة حاليًا.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:230:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        if len(routes) == 1:
            await _start_amount_entry(query, context, routes[0]["code"])
            return
        rows = [
            [btn(render_bot_button('bot.services.dynamic_withdrawals.py:237:button','🌐 {v0}', [f"{item['label']}"]), f"wallet_dynamic_network:{item['code']}", "primary")]
            for item in routes
        ]
        rows.append([btn(render_bot_button('bot.services.dynamic_withdrawals.py:240:button','🏠 القائمة الرئيسية', []), "main_menu")])
        await query.edit_message_text(
            render_bot_text('bot.services.dynamic_withdrawals.py:241:message','<b>اختر شبكة السحب</b>\n\nسيتم جلب الحد الأدنى وحالة الشبكة تلقائيًا.', []),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("wallet_dynamic_network:"):
        await safe_query_answer(query)
        network = data.split(":", 1)[1].upper()
        available_codes = {
            item["code"] for item in wallet_network_routes() if item.get("provider") == "BINANCE"
        }
        if network not in available_codes:
            await query.edit_message_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:255:message','⚠️ الشبكة غير متاحة حاليًا.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:257:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        await _start_amount_entry(query, context, network)
        return

    if data == "wallet_withdraw_confirm":
        draft = context.user_data.get("wallet_withdraw_draft") or {}
        if not all(key in draft for key in ("amount", "network_code", "address")):
            await query.answer("تم إرسال الطلب بالفعل أو انتهت جلسة التأكيد", show_alert=True)
            try:
                await query.edit_message_text(
                    render_bot_text('bot.services.dynamic_withdrawals.py:268:message','<b>✅ تمت معالجة طلب التأكيد</b>\n\nإذا أُرسل الطلب فستجد حالته في سجل العمليات. لن ينشئ الضغط المتكرر طلبًا جديدًا.', []),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[btn(render_bot_button('bot.services.dynamic_withdrawals.py:273:button','📒 سجل العمليات', []), "wallet_history")], [btn(render_bot_button('bot.services.dynamic_withdrawals.py:273:button','🏠 القائمة الرئيسية', []), "main_menu")]]
                    ),
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            return
        network = str(draft.get("network_code") or "").upper()
        if network and "amount" in draft:
            try:
                limits_task = asyncio.to_thread(binance_live_limits, network, force_refresh=True)
                liquidity_task = _read_liquidity(force_refresh=True)
                limits, liquidity = await asyncio.gather(limits_task, liquidity_task)
                actual_fee = effective_network_fee(network, limits["fee"])
                charged_fee = customer_network_fee(actual_fee)
                amount = validate_operation_limit(draft["amount"])
                if amount < limits["minimum"]:
                    raise WalletUserError(
                        f"تغير الحد الأدنى قبل التنفيذ وأصبح {format_display_amount(limits['minimum'])} USDT."
                    )
                if public_liquidity_enabled() and liquidity and liquidity.get("available_known"):
                    available_liquidity = wallet_decimal(liquidity["available"])
                    required_liquidity = amount + actual_fee
                    if required_liquidity > available_liquidity:
                        customer_ceiling = max(wallet_decimal(0), available_liquidity - actual_fee)
                        raise WalletUserError(
                            f"المبلغ يتجاوز السيولة القابلة للتنفيذ على هذه الشبكة. "
                            f"الحد المتاح الآن نحو {format_display_amount(customer_ceiling)} USDT."
                        )
                draft["live_minimum"] = q8(limits["minimum"])
                draft["live_fee"] = q8(actual_fee)
                draft["customer_fee"] = q8(charged_fee)
                context.user_data["wallet_withdraw_draft"] = draft
            except WalletUserError as exc:
                await query.answer("راجع مبلغ السحب", show_alert=True)
                set_state(context, "await_wallet_dynamic_withdraw_amount", network_code=network)
                await query.edit_message_text(
                    render_bot_text('bot.services.dynamic_withdrawals.py:309:message','⚠️ <b>تعذر اعتماد المبلغ الحالي</b>\n\n{v0}\n\nأرسل مبلغًا جديدًا.', [f'{html(exc)}']),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:312:button','🏠 إلغاء', []), "main_menu")]]),
                )
                return
            except Exception as exc:
                reference = wallet_ref("BIN")
                if is_binance_service_error(exc):
                    await _notify_binance_stopped(context, reference=reference, error=exc)
                    await query.edit_message_text(
                        render_bot_text('bot.services.dynamic_withdrawals.py:319:message','⛔ السحب متوقف مؤقتًا بسبب اتصال Binance. سيُفتح تلقائيًا فور نجاح الفحص.', []),
                        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:321:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
                    )
                else:
                    await send_safe_wallet_error(
                        query, context, reference_id=reference, event_type="فشل التحقق النهائي من حدود Binance",
                        exc=exc, telegram_user_id=user.id,
                    )
                return

    await _dynamic_legacy_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state, payload = current_state(context)
    if state != "await_wallet_dynamic_withdraw_amount":
        await _dynamic_legacy_on_message(update, context)
        return

    user = update.effective_user
    network = str(payload.get("network_code") or "").upper()
    try:
        amount = validate_operation_limit(
            wallet_decimal(parse_decimal((update.effective_message.text or "").strip()))
        )
        limits_task = asyncio.to_thread(binance_live_limits, network, force_refresh=True)
        liquidity_task = _read_liquidity(force_refresh=False)
        limits, liquidity = await asyncio.gather(limits_task, liquidity_task)
        actual_fee = effective_network_fee(network, limits["fee"])
        charged_fee = customer_network_fee(actual_fee)
        if amount < limits["minimum"]:
            raise WalletUserError(
                f"الحد الأدنى الحالي للسحب عبر {limits['label']} هو {format_display_amount(limits['minimum'])} USDT."
            )
        available, _ = wallet_balances(user.id)
        required_balance = amount + charged_fee
        if required_balance > available:
            raise WalletUserError(
                f"رصيدك غير كافٍ. المتاح {format_display_amount(available)} USDT، "
                f"والمطلوب مع الرسوم {format_display_amount(required_balance)} USDT."
            )
        if public_liquidity_enabled() and liquidity and liquidity.get("available_known"):
            available_liquidity = wallet_decimal(liquidity["available"])
            required_liquidity = amount + actual_fee
            if required_liquidity > available_liquidity:
                customer_ceiling = max(wallet_decimal(0), available_liquidity - actual_fee)
                raise WalletUserError(
                    f"المبلغ يتجاوز السيولة القابلة للتنفيذ على هذه الشبكة. "
                    f"الحد المتاح الآن نحو {format_display_amount(customer_ceiling)} USDT."
                )
    except (WalletUserError, InvalidOperation, ValueError) as exc:
        await update.effective_message.reply_text(render_bot_text('bot.services.dynamic_withdrawals.py:371:message','❌ {v0}', [f'{html(exc)}']), parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        reference = wallet_ref("BIN")
        if is_binance_service_error(exc):
            await _notify_binance_stopped(context, reference=reference, error=exc)
            await update.effective_message.reply_text(
                render_bot_text('bot.services.dynamic_withdrawals.py:377:message','⛔ السحب متوقف مؤقتًا بسبب اتصال Binance. سيُفتح تلقائيًا فور نجاح الفحص.', [])
            )
        else:
            await send_safe_wallet_error(
                update, context, reference_id=reference, event_type="فشل التحقق من مبلغ السحب",
                exc=exc, telegram_user_id=user.id,
            )
        return

    context.user_data["wallet_withdraw_draft"] = {
        "amount": q8(amount),
        "network_code": network,
        "live_minimum": q8(limits["minimum"]),
        "live_fee": q8(actual_fee),
        "customer_fee": q8(charged_fee),
    }
    set_state(context, "await_wallet_withdraw_address")
    warning = "⚠️ اختر الشبكة المطابقة تمامًا في المحفظة المستلمة. التحويل الخاطئ لا يمكن عكسه."
    await update.effective_message.reply_text(
        render_bot_text('bot.services.dynamic_withdrawals.py:396:message','<b>أرسل عنوان الاستلام</b>\n\n🌐 الشبكة: <b>{v0}</b>\n💵 المبلغ: <b>{v1} USDT</b>\n💸 الرسوم على العميل: <b>{v2}</b>\n💳 الإجمالي المحجوز: <b>{v3} USDT</b>\n📉 الحد الحالي: <b>{v4} USDT</b>\n{v5}', [f"{html(limits['label'])}",f'{html(format_display_amount(amount))}',f'{html(customer_network_fee_text(actual_fee))}',f'{html(format_display_amount(amount + charged_fee))}',f"{html(format_display_amount(limits['minimum']))}",f'{warning}']),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.dynamic_withdrawals.py:405:button','🏠 إلغاء', []), "main_menu")]]),
    )


_export_globals(globals())

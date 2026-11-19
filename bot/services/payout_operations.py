"""Network-fee handling, payout tracking, notification channel, and backup extensions.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- رسوم الشبكة والسحب ----------------


def _binance_address_matches(network_info: dict[str, Any], address: str) -> bool:
    regex = str(network_info.get("addressRegex") or "").strip()
    if regex:
        try:
            if re.fullmatch(regex, address) is None:
                return False
        except re.error:
            log.warning("invalid Binance address regex: %s", regex)
    return True


async def submit_onchain_withdraw(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: int,
    network_code: str,
    address: str,
):
    order = DBX.get_order(order_id)
    if not order:
        await send_or_edit(query, render_bot_text('bot.services.payout_operations.py:32:message','❌ الطلب غير موجود.', []))
        return
    if order["status"] not in {"PAYMENT_VERIFIED", "WAITING_DESTINATION", "FAILED"}:
        await send_or_edit(query, render_bot_text('bot.services.payout_operations.py:35:message','⚠️ الطلب ليس جاهزًا للسحب الآن.', []))
        return
    network_code = network_code.upper()
    network_label = NETWORK_LOOKUP.get(network_code, network_code)
    customer_amount = Decimal(order["crypto_amount"])
    DBX.update_order(
        order_id,
        delivery_type="ONCHAIN",
        network_code=network_code,
        network_label=network_label,
        destination_address=address,
        status="WAITING_DESTINATION",
    )
    if not validate_address_for_network(network_code, address):
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:49:message','<b>❌ العنوان غير مطابق للشبكة المختارة</b>\n\n🌐 الشبكة: <b>{v0}</b>\n📬 العنوان: <code>{v1}</code>', [f'{html(network_label)}',f'{html(address)}']),
            reply_markup=network_kb(order_id),
        )
        return
    if not AUTO_ONCHAIN_WITHDRAW or not BINANCE_WITHDRAW_ENABLED or not BINANCE.enabled() or not DBX.auto_payout_enabled():
        DBX.update_order(order_id, status="WAITING_ADMIN_PAYOUT")
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:59:message','<b>✅ تم حفظ عنوان الاستلام</b>\n\n🧾 الطلب: <code>{v0}</code>\n🌐 الشبكة: <b>{v1}</b>\n📬 العنوان: <code>{v2}</code>\n\nالصرف التلقائي غير مفعل، لذلك تم تحويل الطلب إلى الإدارة.', [f"{html(order['order_code'])}",f'{html(network_label)}',f'{html(address)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.payout_operations.py:66:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:65','<b>🌐 طلب بانتظار الصرف اليدوي</b>\n\n🧾 <code>{v0}</code>\n💵 {v1} USDT\n🌐 {v2}\n📬 <code>{v3}</code>',[f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f'{html(network_label)}',f'{html(address)}']),
            reply_markup=admin_manual_order_kb(order_id),
        )
        return

    try:
        quote = BINANCE.get_withdraw_quote(BINANCE_COIN, network_code, customer_amount)
        live_fee = quote["fee"]
        if REQUIRE_LIVE_FEE_MATCH and live_fee != SYSTEM_NETWORK_FEE_USDT:
            raise RuntimeError(
                f"رسوم Binance الحالية على {network_code} هي {format_display_amount(live_fee)} USDT، "
                f"بينما الرسوم المضبوطة في النظام هي {format_display_amount(SYSTEM_NETWORK_FEE_USDT)} USDT. "
                "تم إيقاف السحب لحماية المبلغ."
            )
        withdraw_amount = (
            customer_amount + SYSTEM_NETWORK_FEE_USDT
            if WITHDRAW_AMOUNT_INCLUDES_FEE
            else customer_amount
        )
        if withdraw_amount < quote["min_amount"]:
            raise RuntimeError(
                f"الحد الأدنى للسحب على شبكة {network_code} هو {format_display_amount(quote['min_amount'])} {BINANCE_COIN}"
            )
        if not _binance_address_matches(quote["net"], address):
            raise RuntimeError(f"عنوان المحفظة لا يطابق صيغة شبكة {network_code} وفق إعدادات Binance")
        if quote["net"].get("withdrawTag"):
            raise RuntimeError(f"شبكة {network_code} تحتاج MEMO/TAG وهذه النسخة لا تدعمه")
        free_balance = BINANCE.get_spot_free_balance(BINANCE_COIN)
        conservative_required = withdraw_amount + live_fee
        if free_balance < conservative_required:
            raise RuntimeError(
                f"الرصيد غير كافٍ. المتاح {format_display_amount(free_balance)} {BINANCE_COIN}، "
                f"والمطلوب احتياطيًا {format_display_amount(conservative_required)} {BINANCE_COIN}."
            )
    except Exception as exc:
        DBX.update_order(order_id, status="PAYMENT_VERIFIED", error_message=str(exc))
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:110:message','<b>❌ تعذر تجهيز طلب السحب</b>\n\nالسبب الحقيقي: <code>{v0}</code>\n\nلم يتم إرسال أي طلب سحب إلى Binance، ويمكنك إعادة اختيار الشبكة أو العنوان.', [f'{html(exc)}']),
            reply_markup=network_kb(order_id),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:112','<b>❌ فشل تجهيز السحب</b>\n\n🧾 <code>{v0}</code>\nالخطأ: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(exc)}']),
        )
        log.exception("withdraw preparation failed order=%s", order["order_code"])
        return

    payout_request_id = order["payout_request_id"] or f"WD-{order['order_code']}"
    DBX.update_order(
        order_id,
        status="READY_FOR_PAYOUT",
        payout_request_id=payout_request_id,
        withdraw_amount=q8(withdraw_amount),
        network_fee=q8(SYSTEM_NETWORK_FEE_USDT),
        error_message=None,
    )
    if not DBX.mark_processing_payout(order_id):
        await send_or_edit(query, render_bot_text('bot.services.payout_operations.py:136:message','⚠️ الطلب دخل المعالجة مسبقًا ولا يمكن تكرار السحب.', []))
        return

    try:
        response = BINANCE.withdraw(
            coin=BINANCE_COIN,
            network=network_code,
            address=address,
            amount=withdraw_amount,
            withdraw_order_id=payout_request_id,
            wallet_type=BINANCE_WALLET_TYPE,
        )
        withdraw_id = response.get("id")
        DBX.update_order(
            order_id,
            status="WITHDRAW_SUBMITTED",
            binance_withdraw_id=withdraw_id,
            payout_started_at=now_str(),
            error_message=None,
        )
        DBX.audit(
            getattr(query, "from_user", None).id if getattr(query, "from_user", None) else order["telegram_user_id"],
            actor_name(getattr(query, "from_user", None)),
            "SUBMIT_WITHDRAW",
            "order",
            order["order_code"],
            f"network={network_code}; customer_amount={customer_amount}; request_amount={withdraw_amount}; fee={SYSTEM_NETWORK_FEE_USDT}; id={withdraw_id}",
        )
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:164:message','<b>✅ تم إرسال طلب السحب إلى Binance</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 كمية العميل: <b>{v1} USDT</b>\n💸 مبلغ طلب السحب: <b>{v2} USDT</b>\n🛡️ رسوم يتحملها النظام: <b>{v3} USDT</b>\n🌐 الشبكة: <b>{v4}</b>\n📬 العنوان: <code>{v5}</code>\n🆔 Binance Withdraw ID: <code>{v6}</code>\n\n🔄 ستتم متابعة TxID وإشعارك عند الاكتمال.', [f"{html(order['order_code'])}",f'{html(format_display_amount(customer_amount))}',f'{html(format_display_amount(withdraw_amount))}',f'{html(format_display_amount(SYSTEM_NETWORK_FEE_USDT))}',f'{html(network_label)}',f'{html(address)}',f"{html(withdraw_id or '—')}"]),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.payout_operations.py:175:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:164','<b>🚀 تم إرسال طلب سحب</b>\n\n🧾 <code>{v0}</code>\n💵 للعميل: {v1} USDT\n💸 الطلب: {v2} USDT\n🆔 <code>{v3}</code>',[f"{html(order['order_code'])}",f'{html(format_display_amount(customer_amount))}',f'{html(format_display_amount(withdraw_amount))}',f"{html(withdraw_id or '—')}"]),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        # لا نعيد الإرسال تلقائياً عند انقطاع الشبكة؛ قد يكون Binance استلم الطلب بالفعل.
        DBX.update_order(order_id, status="WITHDRAW_STATUS_UNKNOWN", error_message=str(exc))
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:188:message','<b>⚠️ حالة السحب غير مؤكدة</b>\n\nانقطع الاتصال أثناء إرسال الطلب. لن يعيد البوت السحب تلقائيًا لمنع التكرار، وسيبحث عن الطلب بواسطة معرفه الفريد.', []),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.payout_operations.py:193:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:180','<b>⚠️ حالة سحب غير مؤكدة</b>\n\n🧾 <code>{v0}</code>\nمعرف الطلب: <code>{v1}</code>\nالخطأ: <code>{v2}</code>',[f"{html(order['order_code'])}",f'{html(payout_request_id)}',f'{html(exc)}']),
        )
        log.exception("withdraw connection uncertainty order=%s", order["order_code"])
    except Exception as exc:
        DBX.update_order(order_id, status="FAILED", error_message=str(exc))
        await send_or_edit(
            query,
            render_bot_text('bot.services.payout_operations.py:205:message','<b>❌ فشل طلب السحب من Binance</b>\n\nالسبب الحقيقي: <code>{v0}</code>', [f'{html(exc)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.payout_operations.py:209:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:195','<b>❌ فشل سحب Binance</b>\n\n🧾 <code>{v0}</code>\nالخطأ: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(exc)}']),
        )
        log.exception("withdraw failed order=%s", order["order_code"])


# ---------------- قناة إشعارات العمليات ----------------


def mask_payer_name(value: str) -> str:
    text = re.sub(r"\s+-\s+\d+\s*$", "", str(value or "")).strip()
    if not text:
        return "مستخدم"
    if "*" in text:
        return text
    chars = list(text)
    visible_positions = [i for i, char in enumerate(chars) if not char.isspace()]
    if len(visible_positions) <= 2:
        return chars[0] + "*" if chars else "مستخدم"
    first = visible_positions[0]
    last = visible_positions[-1]
    for i in visible_positions[1:-1]:
        chars[i] = "*"
    chars[first] = chars[first]
    chars[last] = chars[last]
    return "".join(chars)


async def publish_success_to_channel(context: ContextTypes.DEFAULT_TYPE, order: sqlite3.Row) -> None:
    channel_id = (
        users_channel_id()
        if callable(globals().get("users_channel_id"))
        else DBX.get_setting("notification_channel_id")
    )
    if not channel_id or int(order["channel_notified"] or 0) == 1:
        return
    try:
        await context.bot.send_message(
            chat_id=_as_chat_identifier(channel_id),
            text=(
                render_bot_text('bot.services.payout_operations.py:251:message','<b>✅ تمت عملية بيع وسحب بنجاح</b>\n\n👤 الاسم: <b>{v0}</b>\n💰 المبلغ: <b>{v1} ريال يمني</b>\n💵 الكمية: <b>{v2} USDT</b>\n🌐 الشبكة: <b>{v3}</b>\n📌 الحالة: <b>تمت بنجاح</b>', [f"{html(mask_payer_name(order['payer_display'] or order['payer_name'] or order['full_name'] or 'مستخدم'))}",f"{html(order['paid_yer_amount'] or order['expected_yer_amount'])}",f"{html(order['crypto_amount'])}",f"{html(order['network_label'] or order['network_code'] or '—')}"])
            ),
            parse_mode=ParseMode.HTML,
        )
        DBX.update_order(order["id"], channel_notified=1)
    except TelegramError as exc:
        log.exception("notification channel publish failed order=%s", order["order_code"])
        await notify_admin(
            context,
            render_bot_text('notice.bot.services.payout_operations.py:245','<b>⚠️ تعذر النشر في قناة الإشعارات</b>\n\n🧾 <code>{v0}</code>\nالخطأ: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(exc)}']),
        )


_legacy_monitor_withdrawals = monitor_withdrawals


async def monitor_withdrawals(context: ContextTypes.DEFAULT_TYPE):
    # نستخدم منطق المتابعة السابق، مع إضافة الحالات غير المؤكدة وقناة الإشعارات.
    if BINANCE.enabled() and BINANCE_WITHDRAW_ENABLED:
        pending = DBX.list_orders_by_status(("WITHDRAW_SUBMITTED", "PROCESSING_PAYOUT", "WITHDRAW_STATUS_UNKNOWN"))
        for order in pending:
            request_id = order["payout_request_id"]
            if not request_id:
                continue
            try:
                items = BINANCE.withdraw_history(withdraw_order_id=request_id, coin=BINANCE_COIN)
            except Exception as exc:
                log.warning("withdraw monitor failed request=%s: %s", request_id, exc)
                continue
            if not items:
                continue
            record = items[0]
            txid = normalize_txid(record.get("txId"))
            complete_time = str(record.get("completeTime") or "")
            info = str(record.get("info") or "")
            status_value = record.get("status")
            # Binance status 6 means completed in the withdrawal history response.
            completed = bool(status_value == 6 or (txid and complete_time))
            if completed:
                DBX.update_order(order["id"], status="COMPLETED", txid=txid, completed_at=now_str(), error_message=None)
                refreshed = DBX.get_order(order["id"])
                try:
                    await context.bot.send_message(
                        order["telegram_user_id"],
                        render_bot_text('bot.services.payout_operations.py:303:message','<b>✅ تم تنفيذ السحب بنجاح</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 الكمية: <b>{v1} USDT</b>\n🌐 الشبكة: <b>{v2}</b>\n📬 العنوان: <code>{v3}</code>\n\n━━━━━━━━━━━━━━━━━━━━\n🔖 رقم العملية (TxID):\n<code>{v4}</code>\n━━━━━━━━━━━━━━━━━━━━\n⚠️ احتفظ بهذا الرقم لتأكيد أي عملية دفع', [f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f"{html(order['network_label'] or order['network_code'])}",f"{html(order['destination_address'])}",f"{html(txid or 'غير متوفر حتى الآن')}"]),
                        parse_mode=ParseMode.HTML,
                        reply_markup=txid_copy_keyboard(txid),
                    )
                except TelegramError:
                    log.exception("notify user completion failed")
                await notify_admin(
                    context,
                    render_bot_text('notice.bot.services.payout_operations.py:290','<b>✅ اكتمل سحب Binance</b>\n\n🧾 <code>{v0}</code>\n🔗 <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(txid)}']),
                )
                await publish_success_to_channel(context, refreshed)
                continue
            if status_value in {1, 3, 5}:
                failure_reason = info or f"Binance withdrawal status={status_value}"
                DBX.update_order(order["id"], status="FAILED", error_message=failure_reason)
                try:
                    await context.bot.send_message(
                        order["telegram_user_id"],
                        render_bot_text('bot.services.payout_operations.py:332:message','<b>❌ فشل السحب</b>\n\n🧾 <code>{v0}</code>\nالسبب: <code>{v1}</code>', [f"{html(order['order_code'])}",f'{html(failure_reason)}']),
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    log.exception("notify user failure failed")
                await notify_admin(
                    context,
                    render_bot_text('notice.bot.services.payout_operations.py:309','<b>❌ فشل سحب Binance</b>\n\n🧾 <code>{v0}</code>\nالسبب: <code>{v1}</code>',[f"{html(order['order_code'])}",f'{html(failure_reason)}']),
                )
    with DBX.connect() as con:
        completed_rows = con.execute(
            "SELECT * FROM orders_v2 WHERE status = 'COMPLETED' AND COALESCE(channel_notified,0) = 0 ORDER BY id ASC LIMIT 20"
        ).fetchall()
    for completed_order in completed_rows:
        await publish_success_to_channel(context, completed_order)


# ---------------- نسخ احتياطي محسنة ----------------

_original_export_snapshot = DB.export_snapshot
_original_import_snapshot = DB.import_snapshot


def _enhanced_export_snapshot(self: DB) -> dict[str, Any]:
    payload = _original_export_snapshot(self)
    with self.connect() as con:
        payload["bot_users"] = [dict(row) for row in con.execute("SELECT * FROM bot_users ORDER BY telegram_id").fetchall()]
        payload["required_channels"] = [dict(row) for row in con.execute("SELECT * FROM required_channels ORDER BY id").fetchall()]
        payload["broadcasts"] = [dict(row) for row in con.execute("SELECT * FROM broadcasts ORDER BY id").fetchall()]
    return payload


def _enhanced_import_snapshot(self: DB, payload: dict[str, Any]):
    _original_import_snapshot(self, payload)
    with self.connect() as con:
        con.execute("DELETE FROM bot_users")
        con.execute("DELETE FROM required_channels")
        con.execute("DELETE FROM broadcasts")
        for row in payload.get("bot_users", []):
            con.execute(
                """
                INSERT INTO bot_users(
                    telegram_id, username, full_name, first_seen_at, last_seen_at,
                    welcome_sent_at, is_blocked, blocked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["telegram_id"], row.get("username"), row.get("full_name"),
                    row.get("first_seen_at", now_str()), row.get("last_seen_at", now_str()),
                    row.get("welcome_sent_at"), row.get("is_blocked", 0), row.get("blocked_at"),
                ),
            )
        for row in payload.get("required_channels", []):
            con.execute(
                """
                INSERT INTO required_channels(
                    id, chat_id, username, title, invite_link, active,
                    added_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"), row["chat_id"], row.get("username"), row.get("title"),
                    row.get("invite_link"), row.get("active", 1), row.get("added_by"),
                    row.get("created_at", now_str()), row.get("updated_at", now_str()),
                ),
            )
        for row in payload.get("broadcasts", []):
            con.execute(
                """
                INSERT INTO broadcasts(
                    id, admin_id, source_chat_id, source_message_id, total_users,
                    sent_count, failed_count, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"), row["admin_id"], row["source_chat_id"], row["source_message_id"],
                    row.get("total_users", 0), row.get("sent_count", 0), row.get("failed_count", 0),
                    row.get("created_at", now_str()), row.get("finished_at", now_str()),
                ),
            )
        con.commit()


DB.export_snapshot = _enhanced_export_snapshot
DB.import_snapshot = _enhanced_import_snapshot




_export_globals(globals())

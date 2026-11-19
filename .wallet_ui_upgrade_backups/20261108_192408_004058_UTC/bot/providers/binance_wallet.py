"""تنفيذ ومراقبة سحب المحفظة عبر Binance فقط."""
from __future__ import annotations

import sqlite3
from typing import Any

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())


def wallet_network_routes() -> list[dict[str, str]]:
    if not BEP20_ENABLED or not BINANCE_PROVIDER.enabled():
        return []
    return [{"code": BEP20_NETWORK_CODE, "label": BEP20_NETWORK_LABEL, "provider": "BINANCE"}]


def wallet_network_kb() -> InlineKeyboardMarkup:
    rows = [
        [btn(render_bot_button('bot.providers.binance_wallet.py:19:button','🌐 {v0}', [f"{item['label']}"]), f"wallet_network:{item['code']}", "primary")]
        for item in wallet_network_routes()
    ]
    if not rows:
        rows.append([btn(render_bot_button('bot.providers.binance_wallet.py:23:button','⚠️ شبكة Binance غير جاهزة', []), "main_menu")])
    rows.append([btn(render_bot_button('bot.providers.binance_wallet.py:24:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


def wallet_validate_destination(network_code: str, address: str) -> str:
    code = str(network_code or "").upper()
    value = str(address or "").strip()
    if code != str(BEP20_NETWORK_CODE).upper():
        raise WalletUserError("الشبكة المختارة غير متاحة حاليًا.")
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise WalletUserError("عنوان BSC غير صالح. يجب أن يبدأ بـ 0x ويتكون من 40 خانة Hex.")
    return value


def wallet_provider_for_network(network_code: str) -> str:
    if str(network_code or "").upper() != str(BEP20_NETWORK_CODE).upper():
        raise WalletUserError("الشبكة المختارة غير متاحة حاليًا.")
    if not BEP20_ENABLED or not BINANCE_PROVIDER.enabled():
        raise WalletTechnicalError("Binance غير جاهز للسحب")
    return "BINANCE"


def wallet_withdraw_admin_kb(withdrawal_id: int) -> InlineKeyboardMarkup:
    withdrawal = wallet_get_withdrawal(withdrawal_id)
    status = str(withdrawal["status"] if withdrawal else "").upper()
    rows: list[list[Any]] = []
    if status in {"PROCESSING", "SUBMITTED", "STATUS_UNKNOWN"}:
        rows.append([btn(render_bot_button('bot.providers.binance_wallet.py:51:button','🔄 إعادة التحقق', []), f"adm_wallet_check:{withdrawal_id}", "primary")])
    if status in {"AUTO_PENDING", "FAILED_RETRYABLE"}:
        rows.append([btn(render_bot_button('bot.providers.binance_wallet.py:53:button','💰 إلغاء وإعادة الرصيد', []), f"adm_wallet_release_confirm:{withdrawal_id}", "danger")])
    rows.append([btn(render_bot_button('bot.providers.binance_wallet.py:54:button','📄 تفاصيل العملية', []), f"adm_wallet_withdraw_view:{withdrawal_id}")])
    return InlineKeyboardMarkup(rows)


async def _notify_binance_stopped(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reference: str,
    error: BaseException,
) -> None:
    if not binance_claim_failure_notification():
        return
    state = binance_auth_state()
    await notify_admin(
        context,
        render_bot_text('notice.bot.providers.binance_wallet.py:70','<b>🚨 تم إيقاف سحب Binance مؤقتًا</b>\n\nالسبب: <code>{v0}</code>\nAPI code: <code>{v1}</code>\nIP: <code>{v2}</code>\nالمرجع: <code>{v3}</code>\n\nسيتم فحص الاتصال كل 30 ثانية وفتح السحب تلقائيًا عند نجاح الفحص.',[f'{html(error)}',f"{html(getattr(error, 'code', '') or '—')}",f"{html(state.get('egress_ip') or 'UNKNOWN')}",f'{html(reference)}']),
    )


async def _notify_pending_during_outage(
    context: ContextTypes.DEFAULT_TYPE,
    withdrawal: sqlite3.Row,
) -> None:
    try:
        await context.bot.send_message(
            chat_id=int(withdrawal["telegram_user_id"]),
            text=(
                render_bot_text('bot.providers.binance_wallet.py:83:message','<b>🕒 طلب السحب معلّق مؤقتًا</b>\n\n🧾 <code>{v0}</code>\nسيتم تنفيذ الطلب تلقائيًا فور عودة اتصال Binance. بقي المبلغ محجوزًا ولم يتم فقدانه.', [f"{html(withdrawal['withdrawal_code'])}"])
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.warning("Failed to notify queued withdrawal code=%s", withdrawal["withdrawal_code"])


def _wallet_notification_chat(value: Any) -> int | str:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else text


async def wallet_publish_withdrawal_success(
    context: ContextTypes.DEFAULT_TYPE,
    withdrawal: sqlite3.Row,
) -> bool:
    """نشر سحب مكتمل مرة واحدة في قناة المستخدمين العامة فقط."""
    channel = users_channel_id() if callable(globals().get("users_channel_id")) else None
    channel_id = str(channel or DBX.get_setting("notification_channel_id") or "").strip()
    if not channel_id or int(withdrawal["channel_notified"] or 0) == 1:
        return False
    try:
        await context.bot.send_message(
            chat_id=_wallet_notification_chat(channel_id),
            text=(
                render_bot_text('bot.providers.binance_wallet.py:111:message','<b>✅ إشعار سحب ناجح</b>\n\n🧾 المرجع: <code>{v0}</code>\n💵 المبلغ: <b>{v1} USDT</b>\n💸 الرسوم: <b>{v2} USDT</b>\n🌐 الشبكة: <b>{v3}</b>\n📬 العنوان: <code>{v4}</code>\n🔗 TxID: <code>{v5}</code>\n📌 الحالة: <b>مكتمل</b>', [f"{html(withdrawal['withdrawal_code'])}",f"{html(format_display_amount(withdrawal['amount_usdt']))}",f"{html(format_display_amount(withdrawal['customer_fee_usdt']))}",f"{html(withdrawal['network_label'] or withdrawal['network_code'])}",f"{html(wallet_mask(withdrawal['destination_address'], 8, 6))}",f"{html(mask_txid(withdrawal['txid']))}"])
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        with DBX.connect() as con:
            con.execute(
                "UPDATE wallet_withdrawals SET channel_notified=1, channel_notify_last_error=NULL, updated_at=? WHERE id=?",
                (now_str(), int(withdrawal["id"])),
            )
            con.commit()
        return True
    except Exception as exc:
        with DBX.connect() as con:
            con.execute(
                """
                UPDATE wallet_withdrawals
                SET channel_notify_attempts=channel_notify_attempts+1,
                    channel_notify_last_error=?, updated_at=?
                WHERE id=?
                """,
                (f"{type(exc).__name__}: {exc}"[:1000], now_str(), int(withdrawal["id"])),
            )
            con.commit()
        log.exception(
            "Withdrawal channel notification failed code=%s channel=%s",
            withdrawal["withdrawal_code"],
            channel_id,
        )
        return False


async def wallet_flush_completed_withdrawal_channels(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    limit: int = 50,
) -> int:
    channel = users_channel_id() if callable(globals().get("users_channel_id")) else None
    if not str(channel or DBX.get_setting("notification_channel_id") or "").strip():
        return 0
    with DBX.connect() as con:
        rows = con.execute(
            """
            SELECT * FROM wallet_withdrawals
            WHERE status='COMPLETED' AND COALESCE(channel_notified, 0)=0
            ORDER BY id ASC LIMIT ?
            """,
            (max(1, min(200, int(limit))),),
        ).fetchall()
    published = 0
    for row in rows:
        published += int(await wallet_publish_withdrawal_success(context, row))
    return published


async def wallet_submit_withdrawal(
    withdrawal_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target: Any = None,
    initiated_by_admin: bool = False,
) -> None:
    withdrawal = wallet_get_withdrawal(withdrawal_id)
    if not withdrawal:
        raise WalletTechnicalError("عملية السحب غير موجودة")
    if str(withdrawal["provider"] or "").upper() != "BINANCE":
        released = wallet_release_withdrawal(
            withdrawal_id,
            error_code="PROVIDER_REMOVED",
            error_message="المزود المسجل لم يعد مدعومًا",
        )
        try:
            await context.bot.send_message(
                chat_id=int(released["telegram_user_id"]),
                text=render_bot_text('bot.providers.binance_wallet.py:193:message','⚠️ تعذر تنفيذ طلب السحب وتمت إعادة المبلغ إلى رصيدك.', []),
            )
        except Exception:
            pass
        return
    if withdrawal["status"] == "COMPLETED":
        if target:
            await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:202:message','✅ العملية مكتملة مسبقًا.', []))
        return
    if withdrawal["status"] == "SUBMITTED":
        if target:
            await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:206:message','⏳ العملية مرسلة بالفعل وتنتظر تأكيد الشبكة.', []))
        return
    if withdrawal["submission_started_at"] or withdrawal["provider_withdraw_id"] or withdrawal["txid"]:
        raise WalletTechnicalError("لا يمكن إعادة إرسال عملية بدأ إرسالها. سيتم التحقق من حالتها فقط.")

    if binance_auth_state()["blocked"]:
        queued = wallet_requeue_withdrawal(
            withdrawal_id,
            error_code="BINANCE_UNAVAILABLE",
            error_message=binance_auth_state()["last_error"] or "اتصال Binance متوقف",
        )
        if target:
            await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:218:message','🕒 الطلب معلّق وسيُنفذ تلقائيًا عند عودة اتصال Binance.', []))
        return

    with DBX.connect() as con:
        changed = con.execute(
            """
            UPDATE wallet_withdrawals
            SET status='PROCESSING', retry_count=retry_count+1,
                processing_started_at=COALESCE(processing_started_at, ?),
                next_check_at=NULL, expires_at=NULL, updated_at=?
            WHERE id=? AND status IN ('AUTO_PENDING','FAILED_RETRYABLE')
            """,
            (now_str(), now_str(), int(withdrawal_id)),
        )
        con.commit()
    if changed.rowcount != 1:
        refreshed = wallet_get_withdrawal(withdrawal_id)
        if refreshed and refreshed["status"] == "PROCESSING":
            return
        raise WalletTechnicalError(f"حالة السحب لا تسمح بالإرسال: {refreshed['status'] if refreshed else 'MISSING'}")

    withdrawal = wallet_get_withdrawal(withdrawal_id)
    amount = wallet_decimal(withdrawal["amount_usdt"])
    address = withdrawal["destination_address"]
    reference = withdrawal["withdrawal_code"]
    submission_started = False
    try:
        quote = await asyncio.to_thread(
            BINANCE_PROVIDER.quote,
            str(withdrawal["network_code"]),
            address,
            amount,
        )
        wallet_mark_submission_started(withdrawal_id)
        submission_started = True
        result = await asyncio.to_thread(BINANCE_PROVIDER.submit, quote, address, withdrawal["request_id"])
        provider_id = str(result.get("id") or "")
        if not provider_id:
            raise WalletUnknownResultError(f"Binance لم يرجع Withdrawal ID: {result}")
        wallet_mark_submitted(
            withdrawal_id,
            provider_withdraw_id=provider_id,
            network_fee_asset="USDT",
            network_fee_amount=q8(quote.fee),
            raw_json={"quote": quote.raw_network, "submit": result},
        )
        if target:
            await send_or_edit(
                target,
                render_bot_text('bot.providers.binance_wallet.py:265:message','<b>⏳ تم إرسال طلب السحب</b>\n\n🧾 <code>{v0}</code>\n💵 <b>{v1} USDT</b>\n🌐 {v2}', [f'{html(reference)}',f'{html(format_display_amount(amount))}',f"{html(withdrawal['network_label'] or withdrawal['network_code'])}"]),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.providers.binance_wallet.py:271:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
    except BinanceIndividualError as exc:
        released = wallet_release_withdrawal(
            withdrawal_id,
            error_code="INDIVIDUAL_REQUEST_ERROR",
            error_message=str(exc),
        )
        try:
            await context.bot.send_message(
                chat_id=int(withdrawal["telegram_user_id"]),
                text=(
                    render_bot_text('bot.providers.binance_wallet.py:280:message','⚠️ {v0}\n\nتمت إعادة المبلغ المحجوز فورًا إلى رصيدك.', [f'{html(exc)}'])
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.warning("Failed to notify individual withdrawal failure code=%s", reference)
        if target:
            await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:291:message','❌ {v0}', [f'{html(exc)}']))
    except WalletUnknownResultError as exc:
        mark_binance_unavailable(exc)
        wallet_mark_manual_review(withdrawal_id, "STATUS_UNKNOWN", str(exc))
        await _notify_binance_stopped(context, reference=reference, error=exc)
        try:
            await context.bot.send_message(
                chat_id=int(withdrawal["telegram_user_id"]),
                text=(
                    render_bot_text('bot.providers.binance_wallet.py:297:message','<b>🕒 السحب ما زال قيد التحقق</b>\n\n🧾 <code>{v0}</code>\nسيتم فحص العملية تلقائيًا فور عودة اتصال Binance.', [f'{html(reference)}'])
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception as exc:
        refreshed = wallet_get_withdrawal(withdrawal_id)
        provider_may_have_received = bool(
            submission_started
            or (refreshed and refreshed["submission_started_at"])
            or (refreshed and (refreshed["provider_withdraw_id"] or refreshed["txid"]))
        )
        if is_binance_service_error(exc):
            mark_binance_unavailable(exc, code=getattr(exc, "code", ""))
            # رد Binance الصريح يعني أن الطلب لم يُقبل ويمكن إعادته للطابور.
            explicit_rejection = isinstance(exc, BinanceAPIError) and not isinstance(exc, BinanceTransportError)
            if provider_may_have_received and not explicit_rejection:
                wallet_mark_manual_review(withdrawal_id, "STATUS_UNKNOWN", str(exc))
            else:
                queued = wallet_requeue_withdrawal(
                    withdrawal_id,
                    error_code="BINANCE_UNAVAILABLE",
                    error_message=str(exc),
                    clear_submission_started=bool(explicit_rejection),
                )
                await _notify_pending_during_outage(context, queued)
            await _notify_binance_stopped(context, reference=reference, error=exc)
            if target:
                await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:331:message','🕒 الطلب معلّق وسيُنفذ تلقائيًا عند عودة اتصال Binance.', []))
            return

        if provider_may_have_received:
            wallet_mark_manual_review(withdrawal_id, "STATUS_UNKNOWN", type(exc).__name__)
            return
        # A failure before submission is safe to release.
        released = wallet_release_withdrawal(
            withdrawal_id,
            error_code="WITHDRAWAL_FAILED",
            error_message=str(exc),
        )
        try:
            await context.bot.send_message(
                chat_id=int(withdrawal["telegram_user_id"]),
                text=(
                    render_bot_text('bot.providers.binance_wallet.py:344:message','⚠️ تعذر تنفيذ السحب: {v0}\n\nتمت إعادة المبلغ المحجوز فورًا إلى رصيدك.', [f'{html(exc)}'])
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.warning("Failed to notify released withdrawal code=%s", reference)
        if target:
            await send_or_edit(target, render_bot_text('bot.providers.binance_wallet.py:355:message','❌ {v0}', [f'{html(exc)}']))


def _withdrawal_age_seconds(row: sqlite3.Row, field: str = "updated_at") -> float:
    value = row[field] or row["created_at"]
    try:
        return max(0.0, (utc_now() - datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")).total_seconds())
    except Exception:
        return 0.0


async def _notify_released_withdrawal(
    context: ContextTypes.DEFAULT_TYPE,
    row: sqlite3.Row,
    reason: str,
) -> None:
    try:
        await context.bot.send_message(
            chat_id=int(row["telegram_user_id"]),
            text=(
                render_bot_text('bot.providers.binance_wallet.py:372:message','⚠️ تعذر تنفيذ طلب السحب وتمت إعادة المبلغ المحجوز إلى رصيدك.\n\n🧾 <code>{v0}</code>\nالسبب: {v1}', [f"{html(row['withdrawal_code'])}",f'{html(reason)}'])
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.providers.binance_wallet.py:380:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
    except Exception:
        log.warning("Failed to notify released withdrawal code=%s", row["withdrawal_code"])


async def recover_wallet_withdrawals_on_startup(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    """إعادة الحالات غير المرسلة إلى الطابور، دون انتهاء صلاحيتها أثناء الانقطاع."""
    requeued = 0
    unsupported_released = 0
    with DBX.connect() as con:
        rows = con.execute(
            """
            SELECT * FROM wallet_withdrawals
            WHERE status NOT IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED','CANCELLED')
            ORDER BY id ASC LIMIT 500
            """
        ).fetchall()
    for row in rows:
        if row["provider"] == "TON_HOT":
            continue
        if str(row["provider"] or "").upper() != "BINANCE":
            if not row["submission_started_at"] and not row["provider_withdraw_id"] and not row["txid"]:
                released = wallet_release_withdrawal(
                    int(row["id"]),
                    error_code="PROVIDER_REMOVED",
                    error_message="المزود المسجل لم يعد مدعومًا",
                )
                await _notify_released_withdrawal(context, released, "المزود السابق لم يعد مدعومًا")
                unsupported_released += 1
            continue
        if row["status"] == "PROCESSING" and not row["submission_started_at"]:
            wallet_requeue_withdrawal(
                int(row["id"]),
                error_code="STARTUP_REQUEUE",
                error_message="أعيد الطلب للطابور بعد إعادة التشغيل",
            )
            requeued += 1
        else:
            with DBX.connect() as con:
                con.execute(
                    "UPDATE wallet_withdrawals SET next_check_at=?, recovered_at=?, updated_at=? WHERE id=?",
                    (now_str(), now_str(), now_str(), int(row["id"])),
                )
                con.commit()
    log.info(
        "Wallet startup recovery requeued=%s unsupported_released=%s",
        requeued,
        unsupported_released,
    )
    return {"requeued": requeued, "unsupported_released": unsupported_released}


async def monitor_binance_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    """عند توقف Binance، يفحص الاتصال كل 30 ثانية ويفتح السحب فور النجاح."""
    state = binance_auth_state()
    if not state["blocked"]:
        return
    if binance_claim_failure_notification():
        await notify_admin(
            context,
            render_bot_text('notice.bot.providers.binance_wallet.py:424','<b>🚨 تم إيقاف سحب Binance مؤقتًا</b>\n\nالسبب: <code>{v0}</code>\nIP: <code>{v1}</code>\n\nسيتم الفحص كل 30 ثانية وفتح السحب فور نجاح الفحص.',[f"{html(state.get('last_error') or 'غير معروف')}",f"{html(state.get('egress_ip') or 'UNKNOWN')}"]),
        )
    result = await asyncio.to_thread(binance_health_check, force_ip=True)
    if not result.get("ok"):
        return
    await notify_admin(
        context,
        render_bot_text('notice.bot.providers.binance_wallet.py:434','<b>✅ عاد اتصال Binance</b>\n\nIP: <code>{v0}</code>\nتم فتح السحب تلقائيًا وبدأت معالجة الطلبات المعلّقة.',[f"{html(result.get('egress_ip') or 'UNKNOWN')}"]),
    )
    await monitor_wallet_withdrawals(context)


async def monitor_wallet_withdrawals(context: ContextTypes.DEFAULT_TYPE) -> None:
    await wallet_flush_completed_withdrawal_channels(context)
    if binance_auth_state()["blocked"]:
        return

    with DBX.connect() as con:
        auto_rows = con.execute(
            """
            SELECT id FROM wallet_withdrawals
            WHERE provider='BINANCE' AND status='AUTO_PENDING'
              AND (next_check_at IS NULL OR next_check_at <= ?)
            ORDER BY id ASC LIMIT 50
            """,
            (now_str(),),
        ).fetchall()
    for pending in auto_rows:
        await wallet_submit_withdrawal(int(pending["id"]), context, initiated_by_admin=False)
        if binance_auth_state()["blocked"]:
            return

    with DBX.connect() as con:
        rows = con.execute(
            """
            SELECT * FROM wallet_withdrawals
            WHERE provider='BINANCE'
              AND status IN ('PROCESSING','SUBMITTED','STATUS_UNKNOWN')
              AND (next_check_at IS NULL OR next_check_at <= ?)
            ORDER BY id ASC LIMIT 100
            """,
            (now_str(),),
        ).fetchall()

    for withdrawal in rows:
        try:
            fake = {
                "payout_request_id": withdrawal["request_id"],
                "provider_withdraw_id": withdrawal["provider_withdraw_id"],
            }
            status = await asyncio.to_thread(BINANCE_PROVIDER.status, fake)
            if not status:
                if (
                    withdrawal["status"] == "PROCESSING"
                    and not withdrawal["submission_started_at"]
                    and _withdrawal_age_seconds(withdrawal, "processing_started_at") >= 60
                ):
                    wallet_requeue_withdrawal(
                        int(withdrawal["id"]),
                        error_code="PROCESSING_REQUEUED",
                        error_message="لم يبدأ إرسال الطلب وأعيد للطابور",
                    )
                    continue
                wallet_schedule_monitor(int(withdrawal["id"]), success=True, minimum_delay_seconds=30)
                continue

            provider_id = str(status.get("provider_withdraw_id") or "")
            if withdrawal["status"] == "PROCESSING" and provider_id:
                withdrawal = wallet_mark_submitted(
                    int(withdrawal["id"]),
                    provider_withdraw_id=provider_id,
                    txid=str(status.get("txid") or ""),
                    raw_json=status.get("raw"),
                )

            if status.get("completed"):
                completed_txid = normalize_txid(status.get("txid") or withdrawal["txid"])
                if not completed_txid:
                    # Binance may report the terminal state a moment before the
                    # chain hash is indexed. Keep the balance locked and retry;
                    # a completed user operation must always retain its TxID.
                    wallet_schedule_monitor(
                        int(withdrawal["id"]),
                        success=True,
                        minimum_delay_seconds=30,
                    )
                    continue
                completed = wallet_complete_withdrawal(
                    int(withdrawal["id"]),
                    completed_txid,
                    status.get("raw"),
                )
                try:
                    await context.bot.send_message(
                        chat_id=int(withdrawal["telegram_user_id"]),
                        text=(
                            render_bot_text('bot.providers.binance_wallet.py:539:message','<b>✅ اكتمل السحب بنجاح</b>\n\n🧾 <code>{v0}</code>\n💵 <b>{v1} USDT</b>\n🌐 {v2}\n\n━━━━━━━━━━━━━━━━━━━━\n🔖 رقم العملية (TxID):\n<code>{v3}</code>\n━━━━━━━━━━━━━━━━━━━━\n⚠️ احتفظ بهذا الرقم لتأكيد أي عملية دفع', [f"{html(withdrawal['withdrawal_code'])}",f"{html(format_display_amount(withdrawal['amount_usdt']))}",f"{html(withdrawal['network_label'] or withdrawal['network_code'])}",f"{html(normalize_txid(completed['txid']) or 'غير متوفر حتى الآن')}"])
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=txid_copy_keyboard(completed["txid"]),
                    )
                except Exception:
                    log.warning("Failed to notify completed withdrawal code=%s", withdrawal["withdrawal_code"])
                await wallet_publish_withdrawal_success(context, completed)
            elif status.get("failed"):
                reason = str(status.get("error") or status.get("status") or "فشل السحب")
                released = wallet_release_withdrawal(
                    int(withdrawal["id"]),
                    error_code="BINANCE_FAILED",
                    error_message=reason,
                )
                await _notify_released_withdrawal(context, released, reason)
            else:
                wallet_schedule_monitor(int(withdrawal["id"]), success=True, minimum_delay_seconds=30)
        except Exception as exc:
            if is_binance_service_error(exc):
                mark_binance_unavailable(exc, code=getattr(exc, "code", ""))
                await _notify_binance_stopped(
                    context,
                    reference=withdrawal["withdrawal_code"],
                    error=exc,
                )
                return
            # الخطأ غير العام يخص هذه العملية فقط.
            wallet_schedule_monitor(int(withdrawal["id"]), success=False, error=str(exc))
            log.warning("Withdrawal monitoring uncertain id=%s error=%s", withdrawal['id'], type(exc).__name__)

    await wallet_flush_completed_withdrawal_channels(context)


_export_globals(globals())

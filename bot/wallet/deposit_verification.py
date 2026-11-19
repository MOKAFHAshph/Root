"""Jaib-backed amount-free wallet deposit verification workflow."""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.ui.status_icons import wallet_status_icon, wallet_display_text
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# Limit concurrent Jaib queries from Telegram while leaving the message loop free.
_wallet_jaib_verification_gate = asyncio.Semaphore(2)


def wallet_deposit_admin_kb(deposit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(render_bot_button('bot.wallet.deposit_verification.py:18:button','✅ قبول وإضافة الرصيد', []), f"adm_wallet_deposit_approve:{deposit_id}", "success")],
            [btn(render_bot_button('bot.wallet.deposit_verification.py:19:button','❌ رفض', []), f"adm_wallet_deposit_reject:{deposit_id}", "danger")],
            [btn(render_bot_button('bot.wallet.deposit_verification.py:20:button','📄 تفاصيل', []), f"adm_wallet_deposit_view:{deposit_id}")],
        ]
    )


def _is_debit_transaction(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    value = raw.get("isDebitTranID")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "debit", "outgoing"}


def _phone_values(details: Any) -> list[str]:
    values: list[str] = []
    # دمج الهواتف والمعرفات (التي تحتوي غالباً على الرقم البديل للمحفظة)
    candidates = (getattr(details, "phone_candidates", None) or []) + (getattr(details, "identifiers", None) or [])
    for item in candidates:
        value = str(item.get("value") or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def strict_jaib_sender(details, account):
    # Auto discovery requires an explicitly labelled sender field, never a generic
    # phone, recipient field, free text, or suffix match of an unrelated identifier.
    candidates=(getattr(details,'phone_candidates',None) or [])+(getattr(details,'identifiers',None) or [])
    def canonical(value):
        value=normalize_phone(value)
        if len(value)==14 and value.startswith('00967'):value=value[5:]
        elif len(value)==12 and value.startswith('967'):value=value[3:]
        return value
    expected=canonical(account)
    return bool(expected) and any(_is_payer_label(c.get('source','')) and canonical(c.get('value'))==expected for c in candidates)


def strict_jaib_time(since, api_time):
    lower=parse_payment_datetime(since); actual=parse_payment_datetime(api_time)
    if lower is None or actual is None:return False
    actual-=timedelta(hours=JAIB_API_UTC_OFFSET_HOURS)
    return lower<=actual<=utc_now()+timedelta(minutes=5)


def _deposit_financial_values(deposit: sqlite3.Row, tx_amount: Any) -> tuple[Decimal, Decimal, Decimal]:
    amount_yer = Decimal(str(tx_amount))
    if not amount_yer.is_finite() or amount_yer <= 0:
        raise WalletTechnicalError("المبلغ المالي المعاد من جيب غير صالح")
    rate = Decimal(str(deposit["exchange_rate_yer"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rate <= 0:
        raise WalletTechnicalError("سعر الصرف المثبت في طلب الإيداع غير صالح")
    usdt_amount = (amount_yer / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return amount_yer, rate, usdt_amount


def _persist_deposit_transaction(
    deposit_id: int,
    *,
    status: str,
    amount_yer: Decimal,
    rate: Decimal,
    usdt_amount: Decimal,
    details: Any,
    raw_json: str,
    payer_phone: str = "",
    payer_phone_alt: str = "",
    payer_match_source: str = "",
    review_reason: str = "",
    error_message: str = "",
    verified: bool = False,
) -> None:
    identifiers_json = json.dumps(
        getattr(details, "identifiers", None) or [],
        ensure_ascii=False,
        default=str,
    )
    payer_name = str(getattr(details, "payer_display", "") or "").strip()
    payment_time = str(getattr(details, "tx_date", "") or "").strip()
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE wallet_deposits
            SET fiat_amount_yer=?, exchange_rate_yer=?, usdt_amount=?, status=?,
                payer_display=?, payer_name=?, payer_phone=?, payer_phone_alt=?,
                payer_identifiers_json=?, payer_match_source=?, payment_time=?, raw_json=?,
                review_reason=?, error_message=?, verified_at=?, updated_at=?
            WHERE id=? AND status NOT IN ('COMPLETED','REJECTED')
            """,
            (
                q2(amount_yer),
                q2(rate),
                q8(usdt_amount),
                status,
                payer_name or None,
                payer_name or None,
                payer_phone or None,
                payer_phone_alt or None,
                identifiers_json,
                payer_match_source or None,
                payment_time or None,
                raw_json,
                review_reason or None,
                error_message or None,
                now_str() if verified else None,
                now_str(),
                int(deposit_id),
            ),
        )
        con.commit()


async def wallet_verify_deposit_reference_core(
    deposit_id: int,
    telegram_user_id: int,
    reference: str,
    *, strict_sender: bool = False, minimum_payment_time: str | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Verify one Jaib reference using shared, UI-independent business logic.

    Both Telegram handlers and the Mini App call this function. It never sends
    messages. Its result describes whether the operation is final, retryable,
    awaiting review, rejected, or completed.
    """
    deposit = wallet_get_deposit(deposit_id)
    user_id = int(telegram_user_id)
    if not deposit or int(deposit["telegram_user_id"]) != user_id:
        raise WalletUserError("طلب الإيداع غير موجود.")
    if deposit["status"] not in {"WAITING_REFERENCE", "WAITING_PAYMENT"}:
        raise WalletUserError("طلب الإيداع لا يقبل رقم عملية جديداً.")

    reference = str(reference).strip()
    reference_claimed = False

    try:
        def _query_jaib_reference() -> tuple[bool, Any]:
            if not JaibClient.enabled():
                return False, "إعدادات Jaib غير مكتملة"
            client = JaibClient()
            # Bound the financial reference *read* time independently from health checks.
            client.init_timeout = min(float(client.init_timeout), 7.0)
            client.request_timeout = min(float(client.request_timeout), 7.0)
            if not client.init():
                return False, "فشل الاتصال بخوادم Jaib"
            login_ok, login_message = client.login()
            if not login_ok:
                return False, f"فشل تسجيل الدخول إلى Jaib: {login_message}"
            # Query only a bounded set of advertised hosts; no arbitrary new endpoints.
            # Disabled when JAIB_DEPOSIT_QUERY_HOST_LIMIT=0 for legacy routing.
            host_limit = max(0, min(10, int(os.getenv('JAIB_DEPOSIT_QUERY_HOST_LIMIT', '3'))))
            if host_limit:
                client.servers = client.servers[:host_limit]
            try:
                return client.query_ref(reference)
            finally:
                client.session.close()

        log.info(
            "Deposit verification started user=%s deposit=%s",
            user_id,
            deposit["deposit_code"],
        )
        ok = False
        result: Any = "تعذر العثور على رقم العملية"
        attempts = max(1, min(3, int(max_attempts if max_attempts is not None else os.getenv('JAIB_DEPOSIT_VERIFY_ATTEMPTS', '1'))))
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            ok, result = await asyncio.to_thread(_query_jaib_reference)
            elapsed = time.monotonic() - started
            log.info(
                "Deposit Jaib attempt=%s/%s user=%s deposit=%s ok=%s elapsed=%.2fs result_type=%s",
                attempt,
                attempts,
                user_id,
                deposit["deposit_code"],
                bool(ok and isinstance(result, JaibTx)),
                elapsed,
                type(result).__name__,
            )
            if ok and isinstance(result, JaibTx):
                break
            if attempt < attempts and JAIB_VERIFY_RETRY_DELAY_SECONDS:
                await asyncio.sleep(JAIB_VERIFY_RETRY_DELAY_SECONDS)
        if not ok or not isinstance(result, JaibTx):
            with DBX.connect() as con:
                con.execute(
                    """
                    UPDATE wallet_deposits
                    SET status='WAITING_REFERENCE', error_message=?, updated_at=?
                    WHERE id=? AND status IN ('WAITING_REFERENCE','WAITING_PAYMENT')
                    """,
                    (str(result), now_str(), int(deposit_id)),
                )
                con.commit()
            return {
                "outcome": "RETRY_REQUIRED",
                "final": False,
                "status": "WAITING_REFERENCE",
                "deposit_code": deposit["deposit_code"],
                "error": str(result),
            }

        tx = result
        reference = str(tx.reference_id).strip()
        details = getattr(tx, "api_details", None) or extract_payment_api_details(tx.raw)
        customer = DBX.get_customer(int(deposit["customer_id"]))
        if not customer:
            raise WalletTechnicalError("العميل المرتبط بطلب الإيداع غير موجود")

        amount_yer, rate, usdt_amount = _deposit_financial_values(deposit, tx.amount)
        raw_json = json.dumps(tx.raw, ensure_ascii=False, default=str)
        phones = _phone_values(details)
        payer_phone = phones[0] if phones else ""
        payer_phone_alt = phones[1] if len(phones) > 1 else ""

        ok_claim, used_by = wallet_claim_bank_reference(deposit_id, reference)
        if not ok_claim:
            raise WalletUserError(f"رقم العملية مستخدم مسبقاً في المرجع {used_by}.")
        reference_claimed = True

        # Fractional YER payments never credit automatically. Keep them available
        # for manual reconciliation rather than silently rounding the value.
        if amount_yer != amount_yer.to_integral_value():
            _persist_deposit_transaction(
                deposit_id, status="PAYMENT_REVIEW", amount_yer=amount_yer,
                rate=rate, usdt_amount=usdt_amount, details=details,
                raw_json=raw_json, payer_phone=payer_phone,
                payer_phone_alt=payer_phone_alt,
                review_reason="NON_INTEGER_YER_AMOUNT",
                error_message="Real YER payment contains a fractional amount",
            )
            return {
                "outcome": "PAYMENT_REVIEW", "final": False,
                "status": "PAYMENT_REVIEW", "reason": "NON_INTEGER_YER_AMOUNT",
                "deposit_id": int(deposit_id),
                "deposit_code": deposit["deposit_code"],
                "fiat_amount_yer": q2(amount_yer),
                "usdt_amount": q8(usdt_amount),
                "bank_reference": reference,
            }
        if amount_yer < WALLET_MIN_DEPOSIT_YER or usdt_amount <= 0:
            _persist_deposit_transaction(
                deposit_id,
                status="REJECTED",
                amount_yer=amount_yer,
                rate=rate,
                usdt_amount=usdt_amount,
                details=details,
                raw_json=raw_json,
                payer_phone=payer_phone,
                payer_phone_alt=payer_phone_alt,
                review_reason="AMOUNT_BELOW_MINIMUM",
                error_message=f"received={amount_yer}; minimum={WALLET_MIN_DEPOSIT_YER}",
            )
            return {
                "outcome": "REJECTED",
                "final": True,
                "status": "REJECTED",
                "reason": "AMOUNT_BELOW_MINIMUM",
                "deposit_code": deposit["deposit_code"],
                "fiat_amount_yer": q2(amount_yer),
                "usdt_amount": q8(usdt_amount),
            }

        if _is_debit_transaction(tx.raw):
            _persist_deposit_transaction(
                deposit_id,
                status="REJECTED",
                amount_yer=amount_yer,
                rate=rate,
                usdt_amount=usdt_amount,
                details=details,
                raw_json=raw_json,
                payer_phone=payer_phone,
                payer_phone_alt=payer_phone_alt,
                review_reason="DEBIT_TRANSACTION",
                error_message="Referenced transaction is outgoing",
            )
            return {
                "outcome": "REJECTED",
                "final": True,
                "status": "REJECTED",
                "reason": "DEBIT_TRANSACTION",
                "deposit_code": deposit["deposit_code"],
                "fiat_amount_yer": q2(amount_yer),
                "usdt_amount": q8(usdt_amount),
            }

        # Sender/recipient separation is mandatory: a phone appearing in a
        # recipient or generic transaction field cannot establish the payer.
        all_candidates = (
            (getattr(details, "phone_candidates", None) or [])
            + (getattr(details, "identifiers", None) or [])
        )
        matched_phone = None
        actual_phone = payer_phone
        match_source = ""
        fn_match = globals().get("phones_match")
        fn_norm = globals().get("normalize_phone")
        customer_phones = [customer['phone_alt'], customer['phone_primary']]
        if fn_match and fn_norm:
            for expected in customer_phones:
                if not expected:
                    continue
                for item in all_candidates:
                    if not isinstance(item, dict) or not _is_payer_label(item.get('source', '')):
                        continue
                    if fn_match(item.get('value'), expected):
                        matched_phone = fn_norm(expected)
                        actual_phone = item.get('value')
                        match_source = item.get('source')
                        break
                if matched_phone:
                    break
        if strict_sender:
            # Strict API path is limited to the registered alternate payer phone.
            matched_phone = str(customer['phone_alt']) if strict_jaib_sender(details, customer['phone_alt']) else None
        # Do NOT bypass the third-party policy for "trusted" customers. If the
        # provider omits a verified sender identifier, require manual review.

        time_valid, time_reason = payment_time_validation(minimum_payment_time or deposit["created_at"], details.tx_date)
        review_reason = ""
        if strict_sender and str(tx.currency).strip().upper() not in ('YER','ريال يمني','ريال يمني جديد'):
            review_reason = "CURRENCY_MISMATCH"
        elif strict_sender and not strict_jaib_time(minimum_payment_time,details.tx_date):
            review_reason = "PAYMENT_TIME_NOT_VALID"
        elif not time_valid:
            review_reason = time_reason
        elif not matched_phone:
            review_reason = "PHONE_NOT_VISIBLE" if not all_candidates else "PHONE_MISMATCH"

        if review_reason:
            _persist_deposit_transaction(
                deposit_id,
                status="PAYMENT_REVIEW",
                amount_yer=amount_yer,
                rate=rate,
                usdt_amount=usdt_amount,
                details=details,
                raw_json=raw_json,
                payer_phone=actual_phone or payer_phone,
                payer_phone_alt=payer_phone_alt,
                payer_match_source=match_source or "",
                review_reason=review_reason,
                error_message=review_reason,
            )
            return {
                "outcome": "PAYMENT_REVIEW",
                "final": False,
                "status": "PAYMENT_REVIEW",
                "reason": review_reason,
                "deposit_id": int(deposit_id),
                "deposit_code": deposit["deposit_code"],
                "fiat_amount_yer": q2(amount_yer),
                "usdt_amount": q8(usdt_amount),
                "payer_display": str(getattr(details, "payer_display", "") or ""),
                "phones": getattr(details, "phone_candidates", None) or [],
                "identifiers": getattr(details, "identifiers", None) or [],
                "payment_time": str(getattr(details, "tx_date", "") or ""),
                "bank_reference": reference,
            }

        _persist_deposit_transaction(
            deposit_id,
            status="VERIFIED",
            amount_yer=amount_yer,
            rate=rate,
            usdt_amount=usdt_amount,
            details=details,
            raw_json=raw_json,
            payer_phone=actual_phone or payer_phone,
            payer_phone_alt=payer_phone_alt,
            payer_match_source=match_source or "",
            verified=True,
        )
        completed = wallet_credit_deposit(deposit_id)
        available, _ = wallet_balances(user_id)
        return {
            "outcome": "COMPLETED",
            "final": True,
            "status": "COMPLETED",
            "deposit_id": int(deposit_id),
            "deposit_code": completed["deposit_code"],
            "fiat_amount_yer": completed["fiat_amount_yer"],
            "exchange_rate_yer": completed["exchange_rate_yer"],
            "usdt_amount": completed["usdt_amount"],
            "payer_name": completed["payer_name"] or completed["payer_display"] or "",
            "payer_phone": completed["payer_phone"] or "",
            "payment_time": completed["payment_time"] or "",
            "available_balance": q8(available),
            "bank_reference": reference,
            "customer_name": customer["full_name"],
        }

    except WalletUserError:
        raise
    except Exception as exc:
        log.exception(
            "Deposit verification crashed user=%s deposit=%s",
            user_id,
            deposit["deposit_code"],
        )
        if reference_claimed:
            with DBX.connect() as con:
                con.execute(
                    """
                    UPDATE wallet_deposits
                    SET status='PAYMENT_REVIEW', review_reason='TECHNICAL_FAILURE',
                        error_message=?, updated_at=? WHERE id=? AND status != 'COMPLETED'
                    """,
                    (f"{type(exc).__name__}: {exc}", now_str(), int(deposit_id)),
                )
                con.commit()
            return {
                "outcome": "PAYMENT_REVIEW",
                "final": False,
                "status": "PAYMENT_REVIEW",
                "reason": "TECHNICAL_FAILURE",
                "deposit_id": int(deposit_id),
                "deposit_code": deposit["deposit_code"],
                "error": f"{type(exc).__name__}: {exc}",
            }
        with DBX.connect() as con:
            con.execute(
                """
                UPDATE wallet_deposits
                SET status='WAITING_REFERENCE', error_message=?, updated_at=? WHERE id=? AND status != 'COMPLETED'
                """,
                (f"{type(exc).__name__}: {exc}", now_str(), int(deposit_id)),
            )
            con.commit()
        return {
            "outcome": "RETRY_REQUIRED",
            "final": False,
            "status": "WAITING_REFERENCE",
            "deposit_code": deposit["deposit_code"],
            "error": f"{type(exc).__name__}: {exc}",
        }


async def wallet_verify_deposit_reference(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deposit_id: int,
    reference: str,
) -> bool:
    """Telegram adapter for the shared deposit-verification service."""
    user = update.effective_user
    if not user:
        raise WalletUserError("تعذر تحديد المستخدم.")
    wait = await update.effective_message.reply_text(
        f"{wallet_status_icon('deposit_wait')} <b>{wallet_display_text('deposit_wait_text','جارٍ التحقق من رقم العملية عبر جيب. يرجى الانتظار وعدم إعادة إرسال الرقم.')}</b>\n"
        "<i>ستصلك النتيجة هنا؛ لن يُضاف أي رصيد دون تحقق مؤكد.</i>",
        parse_mode=ParseMode.HTML,
    )
    async def _show_progress(stop: asyncio.Event):
        stages = (
            'طلبك في قائمة انتظار التحقق أو جارٍ فحصه عبر جيب...',
            'ما زلنا ننتظر رد خوادم جيب، لا تعِد إرسال الرقم.',
            'قد يتأخر مزود المحفظة؛ لم يتم اعتماد أي رصيد حتى الآن.',
        )
        index = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=12)
                break
            except asyncio.TimeoutError:
                try:
                    await wait.edit_text(
                        f"{wallet_status_icon('deposit_wait')} <b>عملية التحقق قيد التنفيذ</b>\n\n"
                        f"{stages[min(index,len(stages)-1)]}", parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass  # Message updates must never interrupt financial validation.
                index += 1

    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_show_progress(stop))
    try:
        async with _wallet_jaib_verification_gate:
            result = await wallet_verify_deposit_reference_core(
                deposit_id, user.id, reference, max_attempts=1,
            )
    finally:
        stop.set()
        await heartbeat
    outcome = result["outcome"]

    if outcome == "RETRY_REQUIRED":
        await wait.edit_text(
            "⚠️ <b>تعذر التأكد من رقم العملية حالياً</b>\n\n"
            "لم يُضف أي رصيد. قد تكون خوادم جيب متأخرة؛ احتفظ برقم العملية "
            "ولا تكرر إرسال الطلب بسرعة، ويمكنك إعادة المحاولة بعد عودة الاتصال.",
            parse_mode=ParseMode.HTML,
        )
        return False

    if outcome == "REJECTED":
        message = (
            "❌ مبلغ العملية أقل من الحد الأدنى المسموح، لذلك لم تتم إضافة الرصيد."
            if result.get("reason") == "AMOUNT_BELOW_MINIMUM"
            else "❌ العملية المشار إليها ليست تحويلًا واردًا، لذلك تم رفضها."
        )
        await wait.edit_text(
            message,
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.wallet.deposit_verification.py:477:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return True

    if outcome == "PAYMENT_REVIEW":
        if result.get("fiat_amount_yer"):
            await wait.edit_text(
                "⏳ تم العثور على العملية واستخراج المبلغ، لكنها تحتاج مراجعة الإدارة قبل إضافة الرصيد.\n\n"
                f"🧾 المرجع: <code>{html(result['deposit_code'])}</code>\n"
                f"💴 المبلغ المستخرج: <b>{html(format_yer_amount(result['fiat_amount_yer']))} ريال</b>\n"
                f"💵 الرصيد المتوقع: <b>{html(result['usdt_amount'])} USDT</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.wallet.deposit_verification.py:489:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
        else:
            await wait.edit_text(
                wallet_user_status_message(result["deposit_code"]),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.wallet.deposit_verification.py:495:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
        await send_technical_log(
            context,
            reference_id=result["deposit_code"],
            event_type="إيداع بانتظار المراجعة",
            details=(
                f"reason={result.get('reason', '')}\nuser={user.id}\n"
                f"amount={result.get('fiat_amount_yer', '')} YER\n"
                f"usdt={result.get('usdt_amount', '')}\n"
                f"bank_reference={result.get('bank_reference', reference)}\n"
                f"payer={result.get('payer_display', '')}\n"
                f"phones={result.get('phones', [])}\n"
                f"identifiers={result.get('identifiers', [])}\n"
                f"time={result.get('payment_time', '')}\n"
                f"error={result.get('error', '')}"
            ),
            severity="WARNING",
            telegram_user_id=user.id,
            reply_markup=wallet_deposit_admin_kb(deposit_id),
        )
        return True

    await wait.edit_text(
        f"{wallet_status_icon('success_bep20')} <b>تم شحن رصيدك بنجاح</b>\n\n"
        f"💴 المبلغ المستخرج: <b>{html(format_yer_amount(result['fiat_amount_yer']))} ريال</b>\n"
        f"💲 سعر الصرف المثبت: <b>{html(result['exchange_rate_yer'])}</b>\n"
        f"➕ المضاف: <b>{html(result['usdt_amount'])} USDT</b>\n"
        f"👤 المرسل: <b>{html(result['payer_name'] or '—')}</b>\n"
        f"📱 الرقم المطابق: <code>{html(result['payer_phone'] or '—')}</code>\n"
        f"🕒 وقت العملية: <code>{html(result['payment_time'] or '—')}</code>\n"
        f"💰 رصيدك المتاح: <b>{html(result['available_balance'])} USDT</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn(render_bot_button('bot.wallet.deposit_verification.py:530:button','💸 سحب USDT', []), "wallet_withdraw", "primary")],
                [btn(render_bot_button('bot.wallet.deposit_verification.py:531:button','🏠 القائمة الرئيسية', []), "main_menu")],
            ]
        ),
    )
    await notify_admin(
        context,
        wallet_status_icon('success_bep20') + " " + render_bot_text('notice.bot.wallet.deposit_verification.py:538','<b>إيداع محفظة داخلي ناجح</b>\n\n🧾 <code>{v0}</code>\n👤 {v1}\n💴 {v2} ريال\n💵 {v3} USDT\n🔢 <code>{v4}</code>\n📱 {v5}',[f"{html(result['deposit_code'])}",f"{html(result['customer_name'])}",f"{html(format_yer_amount(result['fiat_amount_yer']))}",f"{html(result['usdt_amount'])}",f'{html(reference)}',f"{html(result['payer_phone'] or '—')}"]),
    )
    return True


_export_globals(globals())

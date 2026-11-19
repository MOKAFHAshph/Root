"""Administrative balance adjustment and withdrawal state transitions.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

def wallet_admin_adjust(
    telegram_user_id: int,
    amount: Decimal,
    *,
    admin_id: int,
    reason: str,
) -> sqlite3.Row:
    customer = DBX.get_customer_by_telegram(int(telegram_user_id))
    wallet_ensure(int(telegram_user_id), int(customer["id"]) if customer else None)
    amount = wallet_decimal(amount)
    if amount == 0:
        raise WalletUserError("قيمة التعديل لا يمكن أن تكون صفراً.")
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (int(telegram_user_id),)).fetchone()
        available_before = wallet_decimal(row["available_balance"])
        locked_before = wallet_decimal(row["locked_balance"])
        available_after = available_before + amount
        if available_after < 0:
            con.rollback()
            raise WalletUserError("لا يمكن أن يصبح الرصيد المتاح سالباً.")
        con.execute(
            "UPDATE user_wallets SET available_balance=?, updated_at=? WHERE telegram_user_id=?",
            (q8(available_after), now_str(), int(telegram_user_id)),
        )
        ledger_id = _wallet_insert_ledger(
            con,
            telegram_user_id=int(telegram_user_id),
            customer_id=row["customer_id"],
            transaction_type="ADMIN_CREDIT" if amount > 0 else "ADMIN_DEBIT",
            status="COMPLETED",
            amount_usdt=abs(amount),
            available_before=available_before,
            available_after=available_after,
            locked_before=locked_before,
            locked_after=locked_before,
            admin_id=admin_id,
            notes=reason,
        )
        operation_title = "تمت إضافة رصيد إلى محفظتك" if amount > 0 else "تم خصم رصيد من محفظتك"
        sign = "+" if amount > 0 else "-"
        _wallet_enqueue_notification(
            con,
            event_key=f"WALLET_ADJUST:{ledger_id}",
            telegram_user_id=int(telegram_user_id),
            notification_type="BALANCE_ADJUSTMENT",
            payload={
                "text": (
                    render_bot_text('notice.bot.wallet.withdrawals.py:58','<b>{v0} {v1}</b>\n\nالقيمة: <b>{v2}{v3} USDT</b>\nالرصيد المتاح لديك: <b>{v4} USDT</b>\nالسبب: {v5}',[f"{('✅' if amount > 0 else '➖')}",f'{operation_title}',f'{sign}',f'{format_display_amount(abs(amount))}',f'{format_display_amount(available_after)}',f'{html(reason)}'])
                ),
                "parse_mode": "HTML",
            },
        )
        con.commit()
        result = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (int(telegram_user_id),)).fetchone()
        log.info(
            "Wallet balance adjusted admin=%s user=%s amount=%s reason=%s",
            admin_id,
            telegram_user_id,
            q8(amount),
            reason,
        )
        return result


TERMINAL_WITHDRAWAL_STATUSES = {"COMPLETED", "FAILED_RELEASED", "EXPIRED_RELEASED", "CANCELLED"}


def _wallet_future_time(*, minutes: int = 0, hours: int = 0, seconds: int = 0) -> str:
    return (utc_now() + timedelta(minutes=minutes, hours=hours, seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def wallet_mark_submission_started(withdrawal_id: int) -> sqlite3.Row:
    """تثبيت لحظة بدء استدعاء المزود قبل إرسال الطلب؛ بعدها لا يعاد الرصيد عند نتيجة مجهولة."""
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET submission_started_at=COALESCE(submission_started_at, ?), updated_at=?
            WHERE id=? AND status='PROCESSING'
            """,
            (now_str(), now_str(), int(withdrawal_id)),
        )
        con.commit()
        row = con.execute("SELECT * FROM wallet_withdrawals WHERE id=?", (int(withdrawal_id),)).fetchone()
    if not row:
        raise WalletTechnicalError("عملية السحب غير موجودة")
    return row


def wallet_schedule_monitor(
    withdrawal_id: int,
    *,
    success: bool,
    error: str = "",
    minimum_delay_seconds: int = 30,
) -> bool:
    """تحديث موعد الفحص التالي وإرجاع هل يسمح بتنبيه الإدارة الآن."""
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM wallet_withdrawals WHERE id=?", (int(withdrawal_id),)).fetchone()
        if not row:
            con.rollback()
            return False
        now = utc_now()
        if success:
            delay = max(30, int(minimum_delay_seconds))
            con.execute(
                """
                UPDATE wallet_withdrawals
                SET last_checked_at=?, next_check_at=?, monitor_error_count=0,
                    last_monitor_error=NULL, updated_at=?
                WHERE id=?
                """,
                (now_str(), (now + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S"), now_str(), int(withdrawal_id)),
            )
            con.commit()
            return False

        count = int(row["monitor_error_count"] or 0) + 1
        delay = min(
            int(globals().get("WALLET_MONITOR_MAX_BACKOFF_SECONDS", 600)),
            max(int(minimum_delay_seconds), 30 * (2 ** min(count - 1, 5))),
        )
        last_alert = row["last_monitor_alert_at"]
        should_alert = True
        if last_alert:
            try:
                elapsed = now - datetime.strptime(str(last_alert), "%Y-%m-%d %H:%M:%S")
                should_alert = elapsed.total_seconds() >= int(globals().get("WALLET_MONITOR_ALERT_COOLDOWN_MINUTES", 30)) * 60
            except Exception:
                should_alert = True
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET last_checked_at=?, next_check_at=?, monitor_error_count=?,
                last_monitor_error=?, last_monitor_alert_at=CASE WHEN ? THEN ? ELSE last_monitor_alert_at END,
                updated_at=?
            WHERE id=?
            """,
            (
                now_str(),
                (now + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S"),
                count,
                str(error)[:1000],
                1 if should_alert else 0,
                now_str(),
                now_str(),
                int(withdrawal_id),
            ),
        )
        con.commit()
        return should_alert


def wallet_expire_safe_pending() -> list[sqlite3.Row]:
    """إلغاء الحالات القديمة التي لم تبدأ مطلقًا في الاتصال بمزود السحب."""
    now = utc_now()
    with DBX.connect() as con:
        candidates = con.execute(
            """
            SELECT * FROM wallet_withdrawals
            WHERE status IN ('PENDING_REVIEW','RESERVED','FAILED_RETRYABLE')
              AND provider_withdraw_id IS NULL AND txid IS NULL
              AND submission_started_at IS NULL
            ORDER BY id ASC LIMIT 200
            """
        ).fetchall()

        due_ids: list[int] = []
        for row in candidates:
            expires_text = str(row["expires_at"] or "").strip()
            if expires_text:
                try:
                    expires_at = datetime.strptime(expires_text, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    expires_at = now
            else:
                try:
                    created_at = datetime.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    created_at = now
                if str(row["status"]).upper() == "PENDING_REVIEW":
                    expires_at = created_at + timedelta(
                        hours=int(globals().get("WALLET_MANUAL_PENDING_TTL_HOURS", 24))
                    )
                else:
                    expires_at = created_at + timedelta(
                        minutes=int(globals().get("WALLET_AUTO_PENDING_TTL_MINUTES", 15))
                    )
                con.execute(
                    "UPDATE wallet_withdrawals SET expires_at=?, updated_at=updated_at WHERE id=?",
                    (expires_at.strftime("%Y-%m-%d %H:%M:%S"), int(row["id"])),
                )
            if expires_at <= now:
                due_ids.append(int(row["id"]))
        con.commit()

    expired: list[sqlite3.Row] = []
    for withdrawal_id in due_ids[:100]:
        try:
            expired.append(
                wallet_release_withdrawal(
                    withdrawal_id,
                    error_code="REQUEST_EXPIRED",
                    error_message="انتهت صلاحية طلب السحب قبل بدء الإرسال",
                    final_status="EXPIRED_RELEASED",
                )
            )
        except Exception:
            row = wallet_get_withdrawal(withdrawal_id)
            log.exception(
                "Failed to expire wallet withdrawal code=%s",
                row["withdrawal_code"] if row else withdrawal_id,
            )
    return expired


def wallet_create_withdrawal(
    telegram_user_id: int,
    amount: Decimal,
    network_code: str,
    address: str,
    provider: str = "BINANCE",
    *,
    minimum_amount: Optional[Any] = None,
    customer_fee: Optional[Any] = None,
    destination_memo: str = "",
    confirmation_key: Optional[str] = None,
    liquidity_snapshot: Optional[dict] = None,
) -> sqlite3.Row:
    customer = DBX.get_customer_by_telegram(int(telegram_user_id))
    if not customer or customer["status"] != "ACTIVE":
        raise WalletUserError("حساب العميل غير نشط.")
    amount = validate_operation_limit(amount)
    if amount <= 0:
        raise WalletUserError("مبلغ السحب يجب أن يكون أكبر من صفر.")
    if minimum_amount not in (None, ""):
        minimum = wallet_decimal(minimum_amount)
        if amount < minimum:
            raise WalletUserError(
                f"المبلغ أقل من الحد الأدنى الحالي. الحد الأدنى المسموح: {format_display_amount(minimum)} USDT."
            )
    network_code = str(network_code or "").upper()
    network = get_managed_network(network_code) if callable(globals().get("get_managed_network")) else None
    if not network or not int(network["enabled"] or 0):
        raise WalletUserError("الشبكة المختارة غير متاحة حاليًا.")
    provider = str(provider or "BINANCE").upper()
    if provider not in {"BINANCE", "TON_HOT"}:
        raise WalletUserError("مزود السحب غير متاح.")
    if str(network["provider"]).upper() != provider:
        raise WalletUserError("مزود السحب لا يطابق الشبكة المختارة.")
    if provider == "TON_HOT" and network_code != "TON_USDT":
        raise WalletUserError("مسار TON غير صالح")
    if provider == "TON_HOT" and wallet_decimal(customer_fee or 0) != 0:
        raise WalletUserError("رسوم TON على البوت")
    fee = max(wallet_decimal(0), wallet_decimal(customer_fee or 0))
    total_hold = amount + fee
    network_label = str(network["display_label"] or network_code)

    code = wallet_ref("WD")
    request_id = f"WALLET{uuid.uuid4().hex[:26]}"
    initial_status = "AUTO_PENDING"
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if confirmation_key:
            existing = con.execute("SELECT * FROM wallet_withdrawals WHERE telegram_user_id=? AND confirmation_key=?", (int(telegram_user_id), confirmation_key)).fetchone()
            if existing:
                con.commit()
                return existing
        if provider=='TON_HOT':
            if not liquidity_snapshot:raise WalletTechnicalError('Missing verified TON liquidity snapshot')
            reserved_usdt,reserved_ton=ton_reserved(con)
            gas=wallet_decimal(DBX.get_setting('ton_gas_budget') or '0.1')
            if wallet_decimal(liquidity_snapshot['usdt'])-reserved_usdt<amount or wallet_decimal(liquidity_snapshot['ton'])-reserved_ton<gas+Decimal('0.05'):
                raise WalletUserError('تغيرت سيولة TON بسبب طلب آخر؛ أعد المحاولة لاحقاً.')
        wallet = con.execute(
            "SELECT * FROM user_wallets WHERE telegram_user_id = ?",
            (int(telegram_user_id),),
        ).fetchone()
        if not wallet:
            con.execute(
                "INSERT INTO user_wallets VALUES (?, ?, '0', '0', '0', '0', ?, ?)",
                (int(telegram_user_id), customer["id"], now_str(), now_str()),
            )
            wallet = con.execute(
                "SELECT * FROM user_wallets WHERE telegram_user_id = ?",
                (int(telegram_user_id),),
            ).fetchone()
        available_before = wallet_decimal(wallet["available_balance"])
        locked_before = wallet_decimal(wallet["locked_balance"])
        if available_before < total_hold:
            con.rollback()
            raise WalletUserError(
                f"رصيدك غير كافٍ. المتاح {format_display_amount(available_before)} USDT "
                f"والمطلوب مع الرسوم {format_display_amount(total_hold)} USDT."
            )
        available_after = available_before - total_hold
        locked_after = locked_before + total_hold
        con.execute(
            "UPDATE user_wallets SET available_balance=?, locked_balance=?, updated_at=? WHERE telegram_user_id=?",
            (q8(available_after), q8(locked_after), now_str(), int(telegram_user_id)),
        )
        cur = con.execute(
            """
            INSERT INTO wallet_withdrawals(
                withdrawal_code, telegram_user_id, customer_id, amount_usdt,
                customer_fee_usdt, network_code, network_label, destination_address,
                provider, status, request_id, created_at, updated_at, expires_at, next_check_at, destination_memo, confirmation_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                code,
                int(telegram_user_id),
                customer["id"],
                q8(amount),
                q8(fee),
                network_code,
                network_label,
                address.strip(),
                provider,
                initial_status,
                request_id,
                now_str(),
                now_str(),
                now_str(),
                destination_memo,
                confirmation_key,
            ),
        )
        withdrawal_id = int(cur.lastrowid)
        _wallet_insert_ledger(
            con,
            telegram_user_id=int(telegram_user_id),
            customer_id=customer["id"],
            transaction_type="WITHDRAWAL_HOLD",
            status="COMPLETED",
            amount_usdt=total_hold,
            available_before=available_before,
            available_after=available_after,
            locked_before=locked_before,
            locked_after=locked_after,
            withdrawal_id=withdrawal_id,
            network_code=network_code,
            destination_address=address.strip(),
            notes=f"حجز مبلغ السحب والرسوم؛ المبلغ={q8(amount)} الرسوم={q8(fee)}",
        )
        con.commit()
        result = con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id = ?",
            (withdrawal_id,),
        ).fetchone()
    log.info(
        "Withdrawal created status=%s code=%s user=%s amount=%s network=%s provider=%s",
        result["status"],
        result["withdrawal_code"],
        telegram_user_id,
        result["amount_usdt"],
        result["network_code"],
        provider,
    )
    return result


def wallet_requeue_withdrawal(
    withdrawal_id: int,
    *,
    error_code: str,
    error_message: str,
    clear_submission_started: bool = False,
) -> sqlite3.Row:
    """إبقاء المبلغ محجوزًا وإعادة الطلب لطابور Binance حتى يعود الاتصال."""
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id=?",
            (int(withdrawal_id),),
        ).fetchone()
        if not row:
            con.rollback()
            raise WalletTechnicalError("عملية السحب غير موجودة")
        if row["status"] in TERMINAL_WITHDRAWAL_STATUSES:
            con.commit()
            return row
        if (row["provider_withdraw_id"] or row["txid"]) and not clear_submission_started:
            con.rollback()
            raise WalletTechnicalError("لا يمكن إعادة إرسال عملية لديها مرجع مزود")
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET status='AUTO_PENDING', error_code=?, error_message=?,
                processing_started_at=NULL,
                submission_started_at=CASE WHEN ? THEN NULL ELSE submission_started_at END,
                next_check_at=?, expires_at=NULL, updated_at=?
            WHERE id=?
            """,
            (
                error_code,
                str(error_message)[:1000],
                1 if clear_submission_started else 0,
                now_str(),
                now_str(),
                int(withdrawal_id),
            ),
        )
        con.commit()
        return con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id=?",
            (int(withdrawal_id),),
        ).fetchone()


async def flush_wallet_notification_outbox(
    bot: Any,
    *,
    telegram_user_id: Optional[int] = None,
    limit: int = 100,
) -> dict[str, int]:
    """إرسال الإشعارات المالية المعلقة مع منع التكرار بين البوت وMini App."""
    sent = 0
    failed = 0
    rows = wallet_pending_notifications(limit=limit, telegram_user_id=telegram_user_id)
    for row in rows:
        if not wallet_claim_notification(int(row["id"])):
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
            await bot.send_message(
                chat_id=int(row["telegram_user_id"]),
                text=render_bot_text('bot.wallet.withdrawals.py:428:message','{v0}', [str(str(payload.get('text') or 'تم تحديث محفظتك.'))]),
                parse_mode=payload.get("parse_mode") or ParseMode.HTML,
                disable_web_page_preview=bool(payload.get("disable_web_page_preview", True)),
            )
            wallet_mark_notification_sent(int(row["id"]))
            sent += 1
        except Exception as exc:
            wallet_mark_notification_failed(int(row["id"]), exc)
            failed += 1
            log.warning(
                "Wallet notification delivery failed id=%s user=%s error=%s",
                row["id"],
                row["telegram_user_id"],
                type(exc).__name__,
            )
    return {"sent": sent, "failed": failed}


async def monitor_wallet_notification_outbox(context: ContextTypes.DEFAULT_TYPE) -> None:
    await flush_wallet_notification_outbox(context.bot, limit=100)


def wallet_get_withdrawal(withdrawal_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute("SELECT * FROM wallet_withdrawals WHERE id = ?", (int(withdrawal_id),)).fetchone()


def wallet_mark_submitted(
    withdrawal_id: int,
    *,
    provider_withdraw_id: str = "",
    txid: str = "",
    network_fee_asset: str = "",
    network_fee_amount: Any = "",
    raw_json: Any = None,
) -> sqlite3.Row:
    """Persist provider submission without reopening a terminal withdrawal."""
    clean_txid = normalize_txid(txid)
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id = ?",
            (int(withdrawal_id),),
        ).fetchone()
        if not current:
            con.rollback()
            raise WalletTechnicalError("عملية السحب غير موجودة")
        if current["status"] == "COMPLETED":
            con.commit()
            return current
        if current["status"] in {"FAILED_RELEASED", "EXPIRED_RELEASED", "CANCELLED"}:
            con.rollback()
            raise WalletTechnicalError("لا يمكن إرسال عملية أعيد رصيدها أو ألغيت")

        con.execute(
            """
            UPDATE wallet_withdrawals
            SET status='SUBMITTED', provider_withdraw_id=?, txid=?, network_fee_asset=?,
                network_fee_amount=?, raw_json=?, submitted_at=?, updated_at=?, error_code=NULL, error_message=NULL,
                expires_at=NULL, monitor_error_count=0, last_monitor_error=NULL, next_check_at=?
            WHERE id=?
            """,
            (
                provider_withdraw_id or current["provider_withdraw_id"],
                clean_txid or normalize_txid(current["txid"]),
                network_fee_asset or current["network_fee_asset"],
                str(network_fee_amount) if network_fee_amount not in (None, "") else current["network_fee_amount"],
                json.dumps(raw_json, ensure_ascii=False, default=str) if raw_json is not None else current["raw_json"],
                now_str(),
                now_str(),
                now_str(),
                int(withdrawal_id),
            ),
        )
        con.commit()
        result = con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id = ?",
            (int(withdrawal_id),),
        ).fetchone()
    log.info(
        "Withdrawal submitted code=%s user=%s amount=%s provider_id=%s txid=%s",
        result["withdrawal_code"],
        result["telegram_user_id"],
        result["amount_usdt"],
        result["provider_withdraw_id"],
        result["txid"],
    )
    return result


def wallet_complete_withdrawal(withdrawal_id: int, txid: str, raw_json: Any = None, *, ton_verified: bool = False) -> sqlite3.Row:
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        withdrawal = con.execute("SELECT * FROM wallet_withdrawals WHERE id = ?", (int(withdrawal_id),)).fetchone()
        if not withdrawal:
            con.rollback()
            raise WalletTechnicalError("عملية السحب غير موجودة")
        if withdrawal["status"] == "COMPLETED":
            if withdrawal['provider']=='TON_HOT' and ton_verified and isinstance(raw_json,dict):
                if str(raw_json.get('txid'))!=str(withdrawal['txid']):raise WalletTechnicalError('Transaction identity changed')
                __import__('bot.runtime',fromlist=['get']).get('ton_record_receipt')(con,'customer',withdrawal_id,raw_json)
                if raw_json.get('actual_cost_ton') is not None:
                    con.execute("UPDATE wallet_withdrawals SET network_fee_amount=?,network_fee_asset='TON' WHERE id=?",(raw_json['actual_cost_ton'],withdrawal_id))
            con.commit()
            return withdrawal
        if withdrawal["status"] in {"FAILED_RELEASED", "EXPIRED_RELEASED", "CANCELLED"}:
            con.rollback()
            raise WalletTechnicalError("الرصيد أعيد سابقاً ولا يمكن تثبيت الخصم آلياً")
        if withdrawal['provider']=='TON_HOT' and not ton_verified:
            raise WalletUserError('سحب TON يكتمل فقط بعد التحقق الآلي من استلام USDT على الشبكة.')
        resolved_txid = normalize_txid(txid or withdrawal["txid"])
        if not resolved_txid:
            con.rollback()
            raise WalletTechnicalError("لا يمكن إكمال السحب قبل توفر TxID من الشبكة")
        wallet = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (withdrawal["telegram_user_id"],)).fetchone()
        available_before = wallet_decimal(wallet["available_balance"])
        locked_before = wallet_decimal(wallet["locked_balance"])
        amount = wallet_decimal(withdrawal["amount_usdt"])
        fee = wallet_decimal(withdrawal["customer_fee_usdt"])
        total = amount + fee
        if locked_before < total:
            con.rollback()
            raise WalletTechnicalError("الرصيد المحجوز أقل من مبلغ السحب")
        locked_after = locked_before - total
        total_withdrawn = wallet_decimal(wallet["total_withdrawn"]) + total
        con.execute(
            "UPDATE user_wallets SET locked_balance=?, total_withdrawn=?, updated_at=? WHERE telegram_user_id=?",
            (q8(locked_after), q8(total_withdrawn), now_str(), withdrawal["telegram_user_id"]),
        )
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET status='COMPLETED', txid=?, raw_json=COALESCE(?, raw_json), completed_at=?, updated_at=?,
                error_code=NULL, error_message=NULL, next_check_at=NULL, expires_at=NULL,
                monitor_error_count=0, last_monitor_error=NULL
            WHERE id=?
            """,
            (
                resolved_txid,
                json.dumps(raw_json, ensure_ascii=False, default=str) if raw_json is not None else None,
                now_str(), now_str(), int(withdrawal_id),
            ),
        )
        _wallet_insert_ledger(
            con,
            telegram_user_id=withdrawal["telegram_user_id"],
            customer_id=withdrawal["customer_id"],
            transaction_type="WITHDRAWAL",
            status="COMPLETED",
            amount_usdt=total,
            available_before=available_before,
            available_after=available_before,
            locked_before=locked_before,
            locked_after=locked_after,
            withdrawal_id=int(withdrawal_id),
            network_code=withdrawal["network_code"],
            destination_address=withdrawal["destination_address"],
            txid=resolved_txid,
            notes=f"تثبيت السحب؛ المبلغ={q8(amount)} الرسوم={q8(fee)}",
        )
        if withdrawal['provider']=='TON_HOT':
            con.execute('UPDATE ton_outbox SET last_error=NULL WHERE withdrawal_id=?',(withdrawal_id,))
        if withdrawal['provider']=='TON_HOT' and isinstance(raw_json,dict):
            __import__('bot.runtime',fromlist=['get']).get('ton_record_receipt')(con,'customer',int(withdrawal_id),raw_json)
            if raw_json.get('actual_cost_ton') is not None:
                con.execute("UPDATE wallet_withdrawals SET network_fee_amount=?,network_fee_asset='TON' WHERE id=?",(raw_json['actual_cost_ton'],withdrawal_id))
        if withdrawal['provider']=='TON_HOT':
            for recipient in {int(ADMIN_ID)} | {int(r[0]) for r in con.execute("SELECT telegram_id FROM admin_users WHERE status='ACTIVE'")}:
                _wallet_enqueue_notification(con,event_key=f"TON_ADMIN_DONE:{withdrawal_id}:{recipient}",
                    telegram_user_id=recipient,notification_type='TON_ADMIN_COMPLETED',payload={
                        'text':render_bot_text('notice.bot.wallet.withdrawals.py:604','✅ اكتمل سحب TON {v0}\nالمبلغ: {v1} USDT\nتكلفة المحفظة: {v2} TON\nTxID: {v3}',[f"{html(withdrawal['withdrawal_code'])}",f"{withdrawal['amount_usdt']}",f"{html(str((raw_json or {}).get('actual_cost_ton') or 'قيد القراءة'))}",f'{html(resolved_txid)}']),'parse_mode':'HTML'})
            _wallet_enqueue_notification(con,event_key=f"TON_DONE:{withdrawal_id}",
                telegram_user_id=int(withdrawal['telegram_user_id']),notification_type='TON_COMPLETED',
                payload={'text':render_bot_text('notice.bot.wallet.withdrawals.py:607','✅ اكتمل سحب {v0} USDT عبر TON\nTxID: {v1}',[f"{withdrawal['amount_usdt']}",f'{html(resolved_txid)}']),'parse_mode':'HTML'})
        con.commit()
        result = con.execute("SELECT * FROM wallet_withdrawals WHERE id = ?", (int(withdrawal_id),)).fetchone()
        log.info(
            "Withdrawal completed code=%s user=%s amount=%s txid=%s",
            result["withdrawal_code"],
            result["telegram_user_id"],
            result["amount_usdt"],
            result["txid"],
        )
        return result


def wallet_release_withdrawal(
    withdrawal_id: int,
    *,
    error_code: str,
    error_message: str,
    admin_id: Optional[int] = None,
    final_status: str = "FAILED_RELEASED",
) -> sqlite3.Row:
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        withdrawal = con.execute("SELECT * FROM wallet_withdrawals WHERE id = ?", (int(withdrawal_id),)).fetchone()
        if not withdrawal:
            con.rollback()
            raise WalletTechnicalError("عملية السحب غير موجودة")
        if withdrawal["status"] in {"FAILED_RELEASED", "EXPIRED_RELEASED"}:
            con.commit()
            return withdrawal
        if withdrawal["status"] == "COMPLETED":
            con.rollback()
            raise WalletTechnicalError("لا يمكن إعادة رصيد عملية مكتملة")
        if withdrawal['provider'] == 'TON_HOT' and withdrawal['submission_started_at']:
            raise WalletTechnicalError('لا يمكن إرجاع سحب TON بدأ توقيعه قبل مطابقة الشبكة؛ يبقى الرصيد محجوزاً.')
        wallet = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (withdrawal["telegram_user_id"],)).fetchone()
        available_before = wallet_decimal(wallet["available_balance"])
        locked_before = wallet_decimal(wallet["locked_balance"])
        amount = wallet_decimal(withdrawal["amount_usdt"])
        fee = wallet_decimal(withdrawal["customer_fee_usdt"])
        total = amount + fee
        if locked_before < total:
            con.rollback()
            raise WalletTechnicalError("الرصيد المحجوز أقل من مبلغ الإرجاع")
        available_after = available_before + total
        locked_after = locked_before - total
        con.execute(
            "UPDATE user_wallets SET available_balance=?, locked_balance=?, updated_at=? WHERE telegram_user_id=?",
            (q8(available_after), q8(locked_after), now_str(), withdrawal["telegram_user_id"]),
        )
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET status=?, error_code=?, error_message=?, updated_at=?, expires_at=NULL, next_check_at=NULL,
                monitor_error_count=0, last_monitor_error=NULL
            WHERE id=?
            """,
            (
                "EXPIRED_RELEASED" if str(final_status).upper() == "EXPIRED_RELEASED" else "FAILED_RELEASED",
                error_code, error_message, now_str(), int(withdrawal_id),
            ),
        )
        _wallet_insert_ledger(
            con,
            telegram_user_id=withdrawal["telegram_user_id"],
            customer_id=withdrawal["customer_id"],
            transaction_type="WITHDRAWAL_RELEASE",
            status="COMPLETED",
            amount_usdt=total,
            available_before=available_before,
            available_after=available_after,
            locked_before=locked_before,
            locked_after=locked_after,
            withdrawal_id=int(withdrawal_id),
            network_code=withdrawal["network_code"],
            destination_address=withdrawal["destination_address"],
            admin_id=admin_id,
            notes=f"إعادة الرصيد بعد فشل مؤكد: {error_code}",
        )
        con.commit()
        result = con.execute(
            "SELECT * FROM wallet_withdrawals WHERE id = ?",
            (int(withdrawal_id),),
        ).fetchone()
        log.warning(
            "Withdrawal released code=%s user=%s amount=%s error_code=%s",
            result["withdrawal_code"],
            result["telegram_user_id"],
            result["amount_usdt"],
            error_code,
        )
        return result


def wallet_mark_manual_review(withdrawal_id: int, code: str, message: str, raw_json: Any = None) -> None:
    with DBX.connect() as con:
        con.execute(
            """
            UPDATE wallet_withdrawals
            SET status=?, error_code=?, error_message=?, raw_json=COALESCE(?, raw_json),
                updated_at=?, expires_at=NULL, next_check_at=?
            WHERE id=?
            """,
            (
                "STATUS_UNKNOWN" if str(code).upper() == "STATUS_UNKNOWN" else "MANUAL_REVIEW",
                code,
                message,
                json.dumps(raw_json, ensure_ascii=False, default=str) if raw_json is not None else None,
                now_str(),
                _wallet_future_time(seconds=60),
                int(withdrawal_id),
            ),
        )
        con.commit()
    log.warning(
        "Withdrawal moved to exception review id=%s code=%s message=%s",
        withdrawal_id,
        code,
        message,
    )


_export_globals(globals())

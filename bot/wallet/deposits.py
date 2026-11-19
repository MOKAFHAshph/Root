"""Deposit creation, reference claiming, ledger insertion, and balance credit.

"""
from __future__ import annotations

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

def wallet_prepare_deposit(telegram_user_id: int) -> dict[str, str]:
    """Prepare payment instructions without creating a financial operation.

    This keeps button clicks and abandoned UI sessions out of the database,
    reports, and statistics. The actual deposit row is created only when the
    user submits a transaction reference for verification.
    """
    customer = DBX.get_customer_by_telegram(int(telegram_user_id))
    if not customer or customer["status"] != "ACTIVE":
        raise WalletUserError("حساب العميل غير نشط.")
    rate = effective_customer_rate(int(telegram_user_id), customer=customer)
    if rate <= 0:
        raise WalletTechnicalError("سعر الصرف في النظام غير صالح")
    return {
        "exchange_rate_yer": q2(rate),
        "prepared_at": now_str(),
    }


def wallet_create_deposit(
    telegram_user_id: int,
    amount_yer: Optional[Decimal] = None,
    *,
    exchange_rate_yer: Optional[Decimal] = None,
) -> sqlite3.Row:
    """Create a deposit session.

    When ``amount_yer`` is omitted, the user is not asked for an amount. The
    actual YER value is extracted later from the Jaib transaction reference.
    The exchange rate is locked when the transfer instructions are displayed.
    """
    customer = DBX.get_customer_by_telegram(int(telegram_user_id))
    if not customer or customer["status"] != "ACTIVE":
        raise WalletUserError("حساب العميل غير نشط.")

    rate = (
        wallet_decimal(exchange_rate_yer)
        if exchange_rate_yer is not None
        else effective_customer_rate(int(telegram_user_id), customer=customer)
    )
    if rate <= 0:
        raise WalletTechnicalError("سعر الصرف في النظام غير صالح")

    amount = Decimal("0")
    usdt_amount = Decimal("0")
    status = "WAITING_REFERENCE"
    if amount_yer is not None:
        raw_amount = Decimal(str(amount_yer))
        if not raw_amount.is_finite() or raw_amount != raw_amount.to_integral_value():
            raise WalletUserError("يُقبل الإيداع بمبلغ ريال يمني صحيح فقط دون كسور عشرية.")
        amount = raw_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount < WALLET_MIN_DEPOSIT_YER:
            raise WalletUserError(f"أقل مبلغ إيداع هو {format_display_amount(WALLET_MIN_DEPOSIT_YER)} ريال.")
        usdt_amount = (amount / rate).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        if usdt_amount <= 0:
            raise WalletUserError("المبلغ المدخل صغير جداً.")
        status = "WAITING_PAYMENT"

    code = wallet_ref("DEP")
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """
            INSERT INTO wallet_deposits(
                deposit_code, telegram_user_id, customer_id, fiat_amount_yer,
                exchange_rate_yer, usdt_amount, status, created_at, updated_at, amount_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                int(telegram_user_id),
                int(customer["id"]),
                q2(amount),
                q2(rate),
                q8(usdt_amount),
                status,
                now_str(),
                now_str(),
                "USER_INPUT" if amount_yer is not None else "JAIB_TRANSACTION",
            ),
        )
        deposit_id = int(cur.lastrowid)
        con.commit()
        row = con.execute("SELECT * FROM wallet_deposits WHERE id = ?", (deposit_id,)).fetchone()
    log.info(
        "Deposit session created code=%s user=%s status=%s rate=%s",
        row["deposit_code"],
        telegram_user_id,
        row["status"],
        row["exchange_rate_yer"],
    )
    return row


def wallet_get_deposit(deposit_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute("SELECT * FROM wallet_deposits WHERE id = ?", (int(deposit_id),)).fetchone()


def wallet_claim_bank_reference(deposit_id: int, reference: str) -> tuple[bool, Optional[str]]:
    """Atomically reserve a transaction reference for one open deposit.

    The claim only succeeds while the deposit is still waiting for a reference
    and has no different reference attached. This prevents concurrent messages
    or old inline buttons from replacing a previously consumed transaction ID.
    """
    reference = str(reference).strip()
    if not reference:
        raise WalletUserError("رقم العملية فارغ.")

    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        deposit = con.execute(
            "SELECT deposit_code, status, bank_transaction_id FROM wallet_deposits WHERE id = ?",
            (int(deposit_id),),
        ).fetchone()
        if not deposit:
            con.rollback()
            raise WalletTechnicalError("طلب الإيداع غير موجود")

        current_reference = str(deposit["bank_transaction_id"] or "").strip()
        if current_reference:
            con.rollback()
            if current_reference == reference:
                return False, str(deposit["deposit_code"])
            return False, str(deposit["deposit_code"])
        if str(deposit["status"]) not in {"WAITING_REFERENCE", "WAITING_PAYMENT"}:
            con.rollback()
            return False, str(deposit["deposit_code"])

        verification = con.execute("SELECT request_code FROM account_verification_requests WHERE verification_reference=?", (reference,)).fetchone()
        if verification:
            con.rollback()
            return False, str(verification[0])
        old = con.execute(
            "SELECT order_code FROM orders_v2 WHERE payment_reference = ? LIMIT 1",
            (reference,),
        ).fetchone()
        if old:
            con.rollback()
            return False, str(old["order_code"])

        other = con.execute(
            "SELECT deposit_code FROM wallet_deposits WHERE bank_transaction_id = ? AND id != ? LIMIT 1",
            (reference, int(deposit_id)),
        ).fetchone()
        if other:
            con.rollback()
            return False, str(other["deposit_code"])

        try:
            cursor = con.execute(
                """
                UPDATE wallet_deposits
                SET bank_transaction_id = ?, reference_submitted_at = ?, updated_at = ?
                WHERE id = ?
                  AND bank_transaction_id IS NULL
                  AND status IN ('WAITING_REFERENCE', 'WAITING_PAYMENT')
                """,
                (reference, now_str(), now_str(), int(deposit_id)),
            )
        except sqlite3.IntegrityError:
            con.rollback()
            other = con.execute(
                "SELECT deposit_code FROM wallet_deposits WHERE bank_transaction_id = ? LIMIT 1",
                (reference,),
            ).fetchone()
            return False, str(other["deposit_code"]) if other else "عملية أخرى"

        if cursor.rowcount != 1:
            con.rollback()
            return False, str(deposit["deposit_code"])
        con.commit()
    return True, None


def _wallet_insert_ledger(
    con: sqlite3.Connection,
    *,
    telegram_user_id: int,
    customer_id: Optional[int],
    transaction_type: str,
    status: str,
    amount_usdt: Decimal,
    available_before: Decimal,
    available_after: Decimal,
    locked_before: Decimal,
    locked_after: Decimal,
    fiat_amount_yer: Optional[Any] = None,
    exchange_rate_yer: Optional[Any] = None,
    deposit_id: Optional[int] = None,
    withdrawal_id: Optional[int] = None,
    bank_transaction_id: Optional[str] = None,
    network_code: Optional[str] = None,
    destination_address: Optional[str] = None,
    txid: Optional[str] = None,
    admin_id: Optional[int] = None,
    notes: str = "",
) -> int:
    cur = con.execute(
        """
        INSERT INTO wallet_ledger(
            ledger_code, telegram_user_id, customer_id, transaction_type, status,
            amount_usdt, fiat_amount_yer, exchange_rate_yer,
            available_before, available_after, locked_before, locked_after,
            deposit_id, withdrawal_id, bank_transaction_id, network_code,
            destination_address, txid, admin_id, notes, created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet_ref("LED"), telegram_user_id, customer_id, transaction_type, status,
            q8(amount_usdt), str(fiat_amount_yer) if fiat_amount_yer is not None else None,
            str(exchange_rate_yer) if exchange_rate_yer is not None else None,
            q8(available_before), q8(available_after), q8(locked_before), q8(locked_after),
            deposit_id, withdrawal_id, bank_transaction_id, network_code,
            destination_address, txid, admin_id, notes, now_str(), now_str() if status == "COMPLETED" else None,
        ),
    )
    return int(cur.lastrowid)


def wallet_credit_deposit(deposit_id: int, *, admin_id: Optional[int] = None, notes: str = "") -> sqlite3.Row:
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        deposit = con.execute("SELECT * FROM wallet_deposits WHERE id = ?", (int(deposit_id),)).fetchone()
        if not deposit:
            con.rollback()
            raise WalletTechnicalError("طلب الإيداع غير موجود")
        if deposit["status"] == "COMPLETED":
            con.commit()
            return deposit
        if deposit["status"] not in {"VERIFIED", "PAYMENT_REVIEW", "WAITING_PAYMENT"}:
            con.rollback()
            raise WalletTechnicalError(f"حالة الإيداع لا تسمح بالإضافة: {deposit['status']}")
        wallet = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (deposit["telegram_user_id"],)).fetchone()
        if not wallet:
            con.execute(
                "INSERT INTO user_wallets VALUES (?, ?, '0', '0', '0', '0', ?, ?)",
                (deposit["telegram_user_id"], deposit["customer_id"], now_str(), now_str()),
            )
            wallet = con.execute("SELECT * FROM user_wallets WHERE telegram_user_id = ?", (deposit["telegram_user_id"],)).fetchone()
        available_before = wallet_decimal(wallet["available_balance"])
        locked_before = wallet_decimal(wallet["locked_balance"])
        amount = wallet_decimal(deposit["usdt_amount"])
        if amount <= 0:
            con.rollback()
            raise WalletTechnicalError("قيمة الإيداع المستخرجة غير صالحة للإضافة")
        available_after = available_before + amount
        total_deposited = wallet_decimal(wallet["total_deposited"]) + amount
        con.execute(
            """
            UPDATE user_wallets
            SET available_balance=?, total_deposited=?, updated_at=?
            WHERE telegram_user_id=?
            """,
            (q8(available_after), q8(total_deposited), now_str(), deposit["telegram_user_id"]),
        )
        con.execute(
            """
            UPDATE wallet_deposits
            SET status='COMPLETED', verified_at=COALESCE(verified_at, ?), completed_at=?, updated_at=?, error_message=NULL
            WHERE id=?
            """,
            (now_str(), now_str(), now_str(), int(deposit_id)),
        )
        _wallet_insert_ledger(
            con,
            telegram_user_id=deposit["telegram_user_id"],
            customer_id=deposit["customer_id"],
            transaction_type="DEPOSIT",
            status="COMPLETED",
            amount_usdt=amount,
            fiat_amount_yer=deposit["fiat_amount_yer"],
            exchange_rate_yer=deposit["exchange_rate_yer"],
            available_before=available_before,
            available_after=available_after,
            locked_before=locked_before,
            locked_after=locked_before,
            deposit_id=int(deposit_id),
            bank_transaction_id=deposit["bank_transaction_id"],
            admin_id=admin_id,
            notes=notes,
        )
        con.commit()
        result = con.execute(
            "SELECT * FROM wallet_deposits WHERE id = ?",
            (int(deposit_id),),
        ).fetchone()
    log.info(
        "Deposit credited code=%s user=%s amount_usdt=%s amount_yer=%s admin=%s",
        result["deposit_code"],
        result["telegram_user_id"],
        result["usdt_amount"],
        result["fiat_amount_yer"],
        admin_id,
    )
    return result




_export_globals(globals())

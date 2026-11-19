"""مطابقة مدفوعات Jaib وتنفيذ السحب عبر Binance فقط."""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

JAIB_RAW_LOG_PATH = BASE_DIR / env("JAIB_RAW_LOG_FILE", "exports/jaib_payment_raw.jsonl")
JAIB_RAW_LOG_ENABLED = env("JAIB_RAW_LOG_ENABLED", "true").lower() == "true"
JAIB_RAW_LOG_MAX_BYTES = max(1_048_576, int(env("JAIB_RAW_LOG_MAX_BYTES", str(10 * 1024 * 1024))))
JAIB_RAW_LOG_BACKUP_COUNT = max(1, min(int(env("JAIB_RAW_LOG_BACKUP_COUNT", "3")), 20))
MAX_AUTO_PAYOUT_FEE_USDT = Decimal(env("MAX_AUTO_PAYOUT_FEE_USDT", "5"))


def _payout_schema() -> None:
    with DBX.connect() as con:
        _ensure_column(con, "customers", "is_trusted", "INTEGER NOT NULL DEFAULT 0")
        for column, declaration in {
            "payout_provider": "TEXT",
            "provider_withdraw_id": "TEXT",
            "provider_status": "TEXT",
            "provider_raw_json": "TEXT",
        }.items():
            _ensure_column(con, "orders_v2", column, declaration)
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_provider_status ON orders_v2(payout_provider, status)")
        con.commit()


_payout_schema()


def _rotate_jaib_raw_log() -> None:
    if not JAIB_RAW_LOG_PATH.exists() or JAIB_RAW_LOG_PATH.stat().st_size < JAIB_RAW_LOG_MAX_BYTES:
        return
    oldest = JAIB_RAW_LOG_PATH.with_name(f"{JAIB_RAW_LOG_PATH.name}.{JAIB_RAW_LOG_BACKUP_COUNT}")
    oldest.unlink(missing_ok=True)
    for index in range(JAIB_RAW_LOG_BACKUP_COUNT - 1, 0, -1):
        source = JAIB_RAW_LOG_PATH.with_name(f"{JAIB_RAW_LOG_PATH.name}.{index}")
        if source.exists():
            source.replace(JAIB_RAW_LOG_PATH.with_name(f"{JAIB_RAW_LOG_PATH.name}.{index + 1}"))
    JAIB_RAW_LOG_PATH.replace(JAIB_RAW_LOG_PATH.with_name(f"{JAIB_RAW_LOG_PATH.name}.1"))


def log_jaib_raw_transaction(
    *,
    order: sqlite3.Row,
    customer: sqlite3.Row,
    telegram_user: Any,
    tx: JaibTx,
    details: PaymentApiDetails,
    matched_registered: Optional[str],
    actual_candidate: Optional[str],
    match_source: Optional[str],
    stage: str = "MATCH_RESULT",
) -> None:
    if not JAIB_RAW_LOG_ENABLED:
        return
    try:
        primary = customer["phone_primary"] or ""
        alternate = customer["phone_alt"] or ""
        record = {
            "logged_at": now_str(),
            "stage": stage,
            "order_id": int(order["id"]),
            "order_code": order["order_code"],
            "telegram_id": int(getattr(telegram_user, "id", order["telegram_user_id"])),
            "customer_id": int(customer["id"]),
            "customer_name": customer["full_name"],
            "customer_is_trusted": bool(customer["is_trusted"]) if "is_trusted" in customer.keys() else False,
            "registered": {
                "phone_primary": primary,
                "phone_primary_variants": sorted(phone_variants(primary)),
                "phone_alt": alternate,
                "phone_alt_variants": sorted(phone_variants(alternate)),
            },
            "transaction": {
                "reference_id": tx.reference_id,
                "amount": str(tx.amount),
                "currency": tx.currency,
                "payer_display": details.payer_display,
                "phone_candidates": details.phone_candidates,
                "identifiers": details.identifiers,
                "tx_date": details.tx_date,
                "tx_type": details.tx_type,
                "note": details.note,
            },
            "match": {
                "matched_registered": matched_registered,
                "actual_candidate": actual_candidate,
                "match_source": match_source,
            },
            "raw": tx.raw,
        }
        JAIB_RAW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_jaib_raw_log()
        with JAIB_RAW_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        try:
            os.chmod(JAIB_RAW_LOG_PATH, 0o600)
        except OSError:
            log.debug("Could not apply restrictive permissions to Jaib raw log", exc_info=True)
    except Exception:
        log.exception("failed to write Jaib raw transaction log")


def match_customer_payment_details(
    customer: sqlite3.Row,
    details: PaymentApiDetails,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    primary = customer["phone_primary"] or ""
    alternate = customer["phone_alt"] or ""
    for candidate in details.phone_candidates:
        if phones_match(candidate["value"], primary):
            return normalize_phone(primary), candidate["value"], f"PRIMARY::{candidate['source']}"
    if alternate:
        for candidate in details.phone_candidates:
            if phones_match(candidate["value"], alternate):
                return normalize_phone(alternate), candidate["value"], f"ALTERNATE::{candidate['source']}"
    return None, None, None


@dataclass
class PayoutQuote:
    provider: str
    coin: str
    network_code: str
    network_label: str
    customer_amount: Decimal
    request_amount: Decimal
    fee: Decimal
    percentage_fee: Decimal
    total_balance_required: Decimal
    min_amount: Decimal
    raw_network: dict[str, Any]


class BinancePayoutProvider:
    name = "BINANCE"

    def enabled(self) -> bool:
        return BINANCE.enabled() and BINANCE_WITHDRAW_ENABLED

    def quote(self, network_code: str, address: str, customer_amount: Decimal) -> PayoutQuote:
        network_code = str(network_code or "").upper()
        managed = get_managed_network(network_code) if callable(globals().get("get_managed_network")) else None
        if not managed or not int(managed["enabled"] or 0):
            raise BinanceIndividualError("الشبكة المختارة غير متاحة حاليًا.")
        quote = BINANCE.get_withdraw_quote(BINANCE_COIN, network_code, customer_amount)
        fee = quote["fee"]
        request_amount = customer_amount + fee
        if customer_amount < quote["min_amount"]:
            raise BinanceIndividualError(
                f"الحد الأدنى للسحب على {network_code} هو {format_display_amount(quote['min_amount'])} {BINANCE_COIN}"
            )
        try:
            wallet_validate_destination(network_code, address)
        except Exception as exc:
            raise BinanceIndividualError(str(exc)) from exc
        if quote["net"].get("withdrawTag"):
            raise BinanceIndividualError(f"شبكة {network_code} تحتاج MEMO/TAG وهذه النسخة لا تدعمه")
        free = BINANCE.get_spot_free_balance(BINANCE_COIN)
        if free < request_amount:
            error = BinanceServiceUnavailableError(
                f"رصيد Binance غير كافٍ. المتاح {format_display_amount(free)} والمطلوب {format_display_amount(request_amount)} {BINANCE_COIN}"
            )
            mark_binance_unavailable(error)
            raise error
        if fee > MAX_AUTO_PAYOUT_FEE_USDT:
            error = BinanceServiceUnavailableError(
                f"رسوم Binance الحالية {format_display_amount(fee)} USDT تتجاوز الحد المسموح {format_display_amount(MAX_AUTO_PAYOUT_FEE_USDT)} USDT"
            )
            mark_binance_unavailable(error)
            raise error
        return PayoutQuote(
            provider=self.name,
            coin=BINANCE_COIN,
            network_code=network_code,
            network_label=str(managed["display_label"] or network_code),
            customer_amount=customer_amount,
            request_amount=request_amount,
            fee=fee,
            percentage_fee=Decimal("0"),
            total_balance_required=request_amount,
            min_amount=quote["min_amount"],
            raw_network=quote["net"],
        )

    def submit(self, quote: PayoutQuote, address: str, request_id: str) -> dict[str, Any]:
        return BINANCE.withdraw(
            coin=quote.coin,
            network=quote.network_code,
            address=address,
            amount=quote.request_amount,
            withdraw_order_id=request_id,
            wallet_type=BINANCE_WALLET_TYPE,
            validate=False,
        )

    def status(self, order: Any) -> Optional[dict[str, Any]]:
        request_id = order["payout_request_id"]
        provider_id = order["provider_withdraw_id"]
        records = BINANCE.withdraw_history(withdraw_order_id=request_id, coin=BINANCE_COIN)
        if not records:
            return None
        row = records[0]
        status_value = row.get("status")
        txid = normalize_txid(row.get("txId"))
        completed = bool(status_value == 6 or (txid and row.get("completeTime")))
        failed = status_value in {1, 3, 5}
        return {
            "completed": completed,
            "failed": failed,
            "status": str(status_value),
            "txid": txid,
            "provider_withdraw_id": str(row.get("id") or provider_id or ""),
            "error": str(row.get("info") or ""),
            "raw": row,
        }


BINANCE_PROVIDER = BinancePayoutProvider()


def payout_provider_for_network(network_code: str) -> BinancePayoutProvider:
    code = str(network_code or "").upper()
    managed = get_managed_network(code) if callable(globals().get("get_managed_network")) else None
    if not managed or not int(managed["enabled"] or 0):
        raise BinanceIndividualError("الشبكة المختارة غير متاحة حاليًا.")
    return BINANCE_PROVIDER


def payout_provider_by_name(name: str) -> BinancePayoutProvider:
    if str(name or "BINANCE").upper() != "BINANCE":
        raise BinanceIndividualError("المزود المسجل غير مدعوم. المزود الوحيد هو Binance.")
    return BINANCE_PROVIDER


def available_network_routes() -> list[dict[str, str]]:
    if not BINANCE_PROVIDER.enabled():
        return []
    if callable(globals().get("wallet_network_routes")):
        return [item for item in wallet_network_routes() if item.get("provider") == "BINANCE"]
    return []


def network_kb(order_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn(render_bot_button('bot.providers.payouts.py:247:button','🌐 {v0}', [f"{route['label']}"]), f"net:{order_id}:{route['code']}", "primary")]
        for route in available_network_routes()
    ]
    if not rows:
        rows.append([btn(render_bot_button('bot.providers.payouts.py:251:button','⚠️ شبكة Binance غير جاهزة', []), "main_menu")])
    rows.append([btn(render_bot_button('bot.providers.payouts.py:252:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


async def submit_onchain_withdraw(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: int,
    network_code: str,
    address: str,
):
    order = DBX.get_order(order_id)
    if not order:
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:265:message','❌ الطلب غير موجود.', []))
        return
    if order["status"] not in {"PAYMENT_VERIFIED", "WAITING_DESTINATION", "FAILED"}:
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:268:message','⚠️ الطلب ليس جاهزًا للسحب الآن.', []))
        return
    try:
        provider = payout_provider_for_network(network_code)
    except BinanceIndividualError as exc:
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:273:message','❌ {v0}', [f'{html(exc)}']), reply_markup=network_kb(order_id))
        return
    network_code = str(network_code or "").upper()
    network_label = managed_network_label(network_code) if callable(globals().get("managed_network_label")) else network_code
    customer_amount = Decimal(order["crypto_amount"])
    DBX.update_order(
        order_id,
        delivery_type="CHAIN_ADDRESS",
        network_code=network_code,
        network_label=network_label,
        destination_address=address.strip(),
        payout_provider="BINANCE",
        status="WAITING_DESTINATION",
    )
    if not AUTO_ONCHAIN_WITHDRAW or not DBX.auto_payout_enabled() or not provider.enabled():
        DBX.update_order(order_id, status="WAITING_ADMIN_PAYOUT")
        await send_or_edit(
            query,
            render_bot_text('bot.providers.payouts.py:289:message','<b>✅ تم حفظ عنوان الاستلام</b>\n\n🌐 الشبكة: <b>{v0}</b>\n📬 العنوان: <code>{v1}</code>', [f'{html(network_label)}',f'{html(address)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.providers.payouts.py:294:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
        return

    try:
        quote = await asyncio.to_thread(provider.quote, network_code, address.strip(), customer_amount)
    except BinanceIndividualError as exc:
        DBX.update_order(order_id, status="PAYMENT_VERIFIED", error_message=str(exc))
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:302:message','❌ {v0}', [f'{html(exc)}']), reply_markup=network_kb(order_id))
        return
    except Exception as exc:
        DBX.update_order(order_id, status="PAYMENT_VERIFIED", error_message=str(exc))
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:306:message','⚠️ خدمة السحب متوقفة مؤقتًا حتى عودة اتصال Binance.', []))
        if binance_claim_failure_notification():
            await notify_admin(context, render_bot_text('notice.bot.providers.payouts.py:307','<b>🚨 توقف سحب Binance</b>\n\n<code>{v0}</code>',[f'{html(exc)}']))
        return

    request_id = order["payout_request_id"] or ("BN" + uuid.uuid4().hex[:28])
    DBX.update_order(
        order_id,
        status="READY_FOR_PAYOUT",
        payout_request_id=request_id,
        payout_provider="BINANCE",
        withdraw_amount=q8(quote.request_amount),
        network_fee=q8(quote.fee),
        network_code=network_code,
        network_label=network_label,
        provider_raw_json=json.dumps({"quote": quote.raw_network}, ensure_ascii=False, default=str),
        error_message=None,
    )
    if not DBX.mark_processing_payout(order_id):
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:325:message','⚠️ الطلب دخل المعالجة مسبقًا ولا يمكن تكرار السحب.', []))
        return

    try:
        response = await asyncio.to_thread(provider.submit, quote, address.strip(), request_id)
        withdraw_id = str(response.get("id") or "")
        DBX.update_order(
            order_id,
            status="WITHDRAW_SUBMITTED",
            payout_provider="BINANCE",
            provider_withdraw_id=withdraw_id,
            binance_withdraw_id=withdraw_id,
            provider_status="SUBMITTED",
            provider_raw_json=json.dumps({"quote": quote.raw_network, "submit": response}, ensure_ascii=False, default=str),
            payout_started_at=now_str(),
            error_message=None,
        )
        await send_or_edit(
            query,
            render_bot_text('bot.providers.payouts.py:342:message','<b>✅ تم إرسال طلب السحب</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 سيستلم العميل: <b>{v1} USDT</b>\n🌐 الشبكة: <b>{v2}</b>\n📬 العنوان: <code>{v3}</code>', [f"{html(order['order_code'])}",f'{html(format_display_amount(customer_amount))}',f'{html(network_label)}',f'{html(address)}']),
            reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.providers.payouts.py:349:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
        )
    except Exception as exc:
        if is_binance_service_error(exc):
            DBX.update_order(order_id, status="WITHDRAW_STATUS_UNKNOWN", provider_status="UNKNOWN", error_message=str(exc))
            if binance_claim_failure_notification():
                await notify_admin(context, render_bot_text('notice.bot.providers.payouts.py:350','<b>🚨 توقف سحب Binance</b>\n\n<code>{v0}</code>',[f'{html(exc)}']))
        else:
            DBX.update_order(order_id, status="FAILED", provider_status="FAILED", error_message=str(exc))
        await send_or_edit(query, render_bot_text('bot.providers.payouts.py:358:message','⚠️ تعذر تنفيذ السحب: <code>{v0}</code>', [f'{html(exc)}']))


async def monitor_withdrawals(context: ContextTypes.DEFAULT_TYPE):
    if binance_auth_state()["blocked"]:
        return
    pending = DBX.list_orders_by_status(("WITHDRAW_SUBMITTED", "PROCESSING_PAYOUT", "WITHDRAW_STATUS_UNKNOWN"))
    for order in pending:
        try:
            status = await asyncio.to_thread(BINANCE_PROVIDER.status, order)
        except Exception as exc:
            if is_binance_service_error(exc) and binance_claim_failure_notification():
                await notify_admin(context, render_bot_text('notice.bot.providers.payouts.py:365','<b>🚨 توقف سحب Binance</b>\n\n<code>{v0}</code>',[f'{html(exc)}']))
            continue
        if not status:
            continue
        DBX.update_order(
            order["id"],
            provider_status=status["status"],
            provider_raw_json=json.dumps(status["raw"], ensure_ascii=False, default=str),
        )
        if status["completed"]:
            DBX.update_order(
                order["id"],
                status="COMPLETED",
                txid=status["txid"],
                completed_at=now_str(),
                error_message=None,
            )
            refreshed = DBX.get_order(order["id"])
            try:
                await context.bot.send_message(
                    order["telegram_user_id"],
                    render_bot_text('bot.providers.payouts.py:389:message','<b>✅ تم تنفيذ السحب بنجاح</b>\n\n🧾 الطلب: <code>{v0}</code>\n💵 الكمية: <b>{v1} USDT</b>\n🌐 الشبكة: <b>{v2}</b>\n\n━━━━━━━━━━━━━━━━━━━━\n🔖 رقم العملية (TxID):\n<code>{v3}</code>\n━━━━━━━━━━━━━━━━━━━━\n⚠️ احتفظ بهذا الرقم لتأكيد أي عملية دفع', [f"{html(order['order_code'])}",f"{html(order['crypto_amount'])}",f"{html(order['network_label'] or order['network_code'])}",f"{html(normalize_txid(status['txid']) or 'غير متوفر حتى الآن')}"]),
                    parse_mode=ParseMode.HTML,
                    reply_markup=txid_copy_keyboard(status["txid"]),
                )
            except TelegramError:
                log.exception("notify payout completion failed")
            await publish_success_to_channel(context, refreshed)
        elif status["failed"]:
            failure = status["error"] or f"Binance status={status['status']}"
            DBX.update_order(order["id"], status="FAILED", error_message=failure)
            try:
                await context.bot.send_message(
                    order["telegram_user_id"],
                    render_bot_text('bot.providers.payouts.py:408:message','<b>❌ فشل السحب</b>\n\n🧾 <code>{v0}</code>\nالسبب: <code>{v1}</code>', [f"{html(order['order_code'])}",f'{html(failure)}']),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                log.exception("notify payout failure failed")

    with DBX.connect() as con:
        rows = con.execute(
            "SELECT * FROM orders_v2 WHERE status = 'COMPLETED' AND COALESCE(channel_notified,0) = 0 ORDER BY id ASC LIMIT 20"
        ).fetchall()
    for row in rows:
        await publish_success_to_channel(context, row)


_export_globals(globals())

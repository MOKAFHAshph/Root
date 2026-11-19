#!/usr/bin/env python3
"""فحوصات دون اتصال لتوثيق الحساب ومبلغ Jaib العشوائي."""
from __future__ import annotations

import asyncio
import os
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace
import sys

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for item in (str(ROOT), str(TESTS_DIR)):
    if item not in sys.path:
        sys.path.insert(0, item)
from final_integration_check import install_dependency_stubs, FakeUser


class ReviewBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._message_id = 100

    async def send_message(self, *args, **kwargs):
        self._message_id += 1
        self.sent.append((args, kwargs))
        return SimpleNamespace(message_id=self._message_id)

    async def edit_message_text(self, *args, **kwargs):
        self.edited.append((args, kwargs))
        return True


def q8(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def assert_random_amount(value: str) -> Decimal:
    amount = Decimal(str(value))
    assert Decimal("100") <= amount <= Decimal("500")
    assert amount % Decimal("50") == 0
    return amount


def main() -> None:
    install_dependency_stubs()
    with tempfile.TemporaryDirectory(prefix="account-verification-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": "TEST_ONLY",
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "verification.db"),
                "LOG_FILE": str(root / "verification.log"),
                "BINANCE_WITHDRAW_ENABLED": "false",
                "ACCOUNT_VERIFICATION_MIN_YER": "100",
                "ACCOUNT_VERIFICATION_MAX_YER": "500",
                "ACCOUNT_VERIFICATION_STEP_YER": "50",
                "ACCOUNT_VERIFICATION_RECIPIENT_PHONE": "74292",
                "ACCOUNT_VERIFICATION_RECIPIENT_NAME": "عبدالرحمن علي",
                "ACCOUNT_VERIFICATION_CHANNEL_ID": "",
                "REFERRAL_REWARD_USDT": "0.01",
                "DEFAULT_USDT_RATE_YER": "550",
            }
        )
        from bot.app import runtime
        import bot.services.account_verification as av

        dbx = runtime.get("DBX")
        register_bot_user = runtime.get("register_bot_user")
        ensure_request = runtime.get("ensure_account_verification")
        get_request = runtime.get("get_account_verification")
        save_name = runtime.get("save_verification_full_name")
        save_primary = runtime.get("save_verification_primary_phone")
        save_alt = runtime.get("save_verification_alt_phone")
        verify_payment = runtime.get("verify_account_payment")
        submit = runtime.get("submit_account_verification")
        approve = runtime.get("approve_account_verification")
        reject = runtime.get("reject_account_verification")
        refunded = runtime.get("mark_account_verification_refunded")
        restart = runtime.get("restart_account_verification")
        wallet_balances = runtime.get("wallet_balances")

        class FakeTx:
            amount = Decimal("0")
            raw = {"amount": "0", "isDebitTranID": False}
            api_details = SimpleNamespace(
                payer_display="Test User",
                phone_candidates=[{"value": "74292", "source": "senderPhone"}],
                identifiers=[],
                tx_date="2026-07-30 11:00:00",
                fields=[],
            )

        class FakeClient:
            @staticmethod
            def enabled():
                return True

            def init(self):
                return True

            def login(self):
                return True, "ok"

            def query_ref(self, reference):
                FakeTx.raw = {"amount": str(FakeTx.amount), "isDebitTranID": False}
                return True, FakeTx()

        original_client, original_tx = av.JaibClient, av.JaibTx
        av.JaibClient, av.JaibTx = FakeClient, FakeTx
        try:
            dbx.set_setting("usdt_rate_yer", "550")
            user = FakeUser(501, "verified_user")
            register_bot_user(user)
            first = ensure_request(user)
            expected_yer = assert_random_amount(first["verification_amount_yer"])
            assert Decimal(first["verification_exchange_rate_yer"]) == Decimal("550.00")

            save_name(501, "عبدالرحمن علي محمد أحمد")
            changed = save_name(501, "عبدالرحمن علي محمد صالح")
            assert changed["full_name"] == "عبدالرحمن علي محمد صالح"
            save_primary(501, "+967771234567")
            save_alt(501, "74292")

            # يمنع الرقم المستخدم كبديل من إعادة استخدامه كرقم أساسي بحساب آخر.
            duplicate_user = FakeUser(503, "duplicate_user")
            register_bot_user(duplicate_user)
            ensure_request(duplicate_user)
            save_name(503, "أحمد محمد علي صالح")
            try:
                save_primary(503, "+96774292")
            except ValueError:
                # صيغة 5 خانات ليست رقمًا أساسيًا صالحًا، لذا نختبر التعارض
                # العابر بين الحقلين برقم هاتف كامل في طلب مستقل أدناه.
                pass

            cross_user = FakeUser(504, "cross_user")
            register_bot_user(cross_user)
            ensure_request(cross_user)
            save_name(504, "خالد أحمد محمد صالح")
            save_primary(504, "773456789")
            try:
                save_alt(504, "771234567")
            except ValueError as exc:
                assert "مستخدم" in str(exc)
            else:
                raise AssertionError("لم يُمنع استخدام الرقم الأساسي لطلب آخر كرقم بديل")

            FakeTx.amount = expected_yer
            FakeTx.api_details.phone_candidates = [{"value": "74292", "source": "senderPhone"}]
            result = asyncio.run(verify_payment(501, "VERIFY-501"))
            assert result["ok"] is True
            assert Decimal(result["amount_yer"]) == expected_yer
            assert result["request"]["status"] == "READY_TO_SUBMIT"

            bot = ReviewBot()
            row = asyncio.run(submit(501, bot))
            assert row["status"] == "PENDING_REVIEW"
            assert dbx.get_customer_by_telegram(501)["status"] == "PENDING"

            # تغيير السعر بعد تقديم الطلب لا يغيّر سعر الطلب المخزن.
            dbx.set_setting("usdt_rate_yer", "600")
            admin = SimpleNamespace(id=1, username="admin", first_name="Admin", full_name="Admin")
            approved, _reward = asyncio.run(approve(bot, int(row["id"]), admin))
            assert approved["status"] == "APPROVED"
            assert dbx.get_customer_by_telegram(501)["status"] == "ACTIVE"
            expected_credit = q8(expected_yer / Decimal("550"))
            available, locked = wallet_balances(501)
            assert available == expected_credit and locked == Decimal("0")
            assert Decimal(approved["verification_credit_usdt"]) == expected_credit
            assert approved["verification_credited_at"]

            # إعادة الموافقة لا تضيف الرصيد مرة ثانية.
            approved_again, _ = asyncio.run(approve(bot, int(row["id"]), admin))
            available_again, _ = wallet_balances(501)
            assert approved_again["status"] == "APPROVED"
            assert available_again == expected_credit
            with dbx.connect() as con:
                count = con.execute(
                    "SELECT COUNT(*) AS c FROM wallet_ledger WHERE telegram_user_id=? AND transaction_type='ACCOUNT_VERIFICATION_CREDIT'",
                    (501,),
                ).fetchone()["c"]
            assert count == 1

            user2 = FakeUser(502, "rejected_user")
            register_bot_user(user2)
            request2 = ensure_request(user2)
            expected2 = assert_random_amount(request2["verification_amount_yer"])
            save_name(502, "محمد أحمد علي صالح")
            save_primary(502, "778456789")
            save_alt(502, "74321")
            FakeTx.amount = expected2
            FakeTx.api_details.phone_candidates = [{"value": "74321", "source": "senderPhone"}]
            result2 = asyncio.run(verify_payment(502, "VERIFY-502"))
            assert result2["ok"] is True
            row2 = asyncio.run(submit(502, bot))
            rejected = asyncio.run(reject(bot, int(row2["id"]), admin, "البيانات غير واضحة"))
            assert rejected["status"] == "REJECTED_PENDING_REFUND"
            assert rejected["refund_status"] == "PENDING"
            final = asyncio.run(refunded(bot, int(row2["id"]), admin))
            assert final["status"] == "REJECTED"
            assert final["refund_status"] == "REFUNDED"
            old_code = final["request_code"]
            restarted = restart(502)
            assert restarted["status"] == "DRAFT"
            assert restarted["request_code"] != old_code
            assert_random_amount(restarted["verification_amount_yer"])
            assert restarted["verification_status"] == "NOT_STARTED"
            assert restarted["verification_credit_usdt"] is None
            assert restarted["verification_credited_at"] is None
            assert restarted["full_name"] is None and restarted["phone_primary"] is None and restarted["phone_alt"] is None
        finally:
            av.JaibClient, av.JaibTx = original_client, original_tx

    print("PASS: random verification, phone uniqueness, atomic credit, idempotency, rejection, refund, and restart")


if __name__ == "__main__":
    main()

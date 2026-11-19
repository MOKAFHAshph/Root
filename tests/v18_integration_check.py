#!/usr/bin/env python3
"""Offline V18 checks for operational controls and financial invariants."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from final_integration_check import FakeBot, FakeContext, FakeUser, install_dependency_stubs


class FakeQuery:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = FakeUser(user_id, "admin")
        self.answers: list[tuple[tuple, dict]] = []
        self.edited: tuple[str, dict] | None = None

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs):
        self.edited = (text, kwargs)
        return SimpleNamespace(message_id=1, chat_id=self.from_user.id)


def main() -> None:
    install_dependency_stubs()
    with tempfile.TemporaryDirectory(prefix="jaib-v18-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": "TEST_ONLY_TELEGRAM_TOKEN_NOT_VALID",
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "v18.db"),
                "LOG_FILE": str(root / "v18.log"),
                "BINANCE_WITHDRAW_ENABLED": "false",
                "JAIB_VERIFY_RETRY_DELAY_SECONDS": "0",
                "PUBLIC_LIQUIDITY_ENABLED_DEFAULT": "false",
            }
        )

        from bot.app import runtime
        from bot.services import v18_features

        dbx = runtime.get("DBX")
        assert runtime.get("APP_VERSION") == "20.0.0"

        # أرقام Jaib ليست أرقام هاتف؛ الصفر البادئ جزء من هوية المحفظة.
        normalize_alt = runtime.get("normalize_jaib_alternate")
        assert normalize_alt("0012345") == "0012345"
        assert normalize_alt("0123456789") == "0123456789"

        # حقول التسجيل قابلة للتعديل حتى القبول، وتغيير رقم Jaib يلغي تحقق الرقم القديم.
        request_user = FakeUser(701, "verification_user")
        verification = runtime.get("ensure_account_verification")(request_user)
        runtime.get("save_verification_full_name")(701, "أحمد محمد علي عبدالله")
        runtime.get("save_verification_primary_phone")(701, "777100701")
        runtime.get("save_verification_alt_phone")(701, "0012345")
        with dbx.connect() as con:
            con.execute(
                """
                UPDATE account_verification_requests
                SET status='PENDING_REVIEW', verification_status='VERIFIED', verification_reference='OLD-REF'
                WHERE id=?
                """,
                (int(verification["id"]),),
            )
            con.commit()
        runtime.get("save_verification_alt_phone")(701, "0098765")
        updated_verification = runtime.get("get_account_verification")(701)
        assert updated_verification["phone_alt"] == "0098765"
        assert updated_verification["status"] == "DRAFT"
        assert updated_verification["verification_status"] == "NOT_STARTED"
        assert updated_verification["verification_reference"] is None

        # القبول التلقائي يحترم مدة الأدمن ويستخدم مسار التفعيل المالي نفسه.
        dbx.set_setting("account_approval_mode", "AUTO")
        dbx.set_setting("account_auto_approval_delay_seconds", "0")
        auto_user = FakeUser(702, "auto_user")
        auto_request = runtime.get("ensure_account_verification")(auto_user)
        runtime.get("save_verification_full_name")(702, "سالم أحمد محمد علي")
        runtime.get("save_verification_primary_phone")(702, "778100702")
        runtime.get("save_verification_alt_phone")(702, "0087654")
        with dbx.connect() as con:
            con.execute(
                """
                UPDATE account_verification_requests
                SET verification_status='VERIFIED', verification_reference='AUTO-REF', status='READY_TO_SUBMIT'
                WHERE id=?
                """,
                (int(auto_request["id"]),),
            )
            con.commit()
        submitted = asyncio.run(runtime.get("submit_account_verification")(702, FakeBot()))
        assert submitted["approval_mode"] == "AUTO" and submitted["auto_approve_at_epoch"] is not None
        auto_context = FakeContext()
        asyncio.run(runtime.get("monitor_auto_account_approvals")(auto_context))
        assert runtime.get("get_account_verification")(702)["status"] == "APPROVED"
        assert dbx.get_customer_by_telegram(702)["status"] == "ACTIVE"
        dbx.set_setting("account_approval_mode", "MANUAL")

        fee = runtime.get("customer_network_fee")
        assert fee("0") == Decimal("0")
        assert fee("0.01") == Decimal("0")
        assert fee("0.009") == Decimal("0")
        assert fee("0.02") == Decimal("0.01000000")
        assert runtime.get("format_yer_amount")("124.50") == "125"

        runtime.get("set_maintenance")(True, "ترقية الخادم")
        assert runtime.get("maintenance_enabled")() is True
        assert runtime.get("maintenance_reason")() == "ترقية الخادم"
        runtime.get("set_maintenance")(False)

        # إدارة شبكة مخصصة: إنشاء ثم تفعيل وتحقق من العنوان ثم حذف آمن.
        custom = runtime.get("create_managed_network")(
            asset_symbol="USDT",
            network_code="TESTNET",
            display_label="Test Network",
            address_regex=r"T[0-9]{8}",
            wallet_address="T12345678",
            fee_override="0.04",
            enabled=True,
        )
        assert runtime.get("wallet_validate_destination")("TESTNET", "T12345678") == "T12345678"
        assert runtime.get("effective_network_fee")("TESTNET", "0.50") == Decimal("0.04")
        runtime.get("set_managed_network_enabled")(int(custom["id"]), False)
        runtime.get("delete_managed_network")(int(custom["id"]))

        # الرسم يُحجز مع المبلغ ويُعاد كاملًا عند الفشل المؤكد.
        customer_id = dbx.create_customer("Fee User", "777100700", telegram_id=700)
        runtime.get("wallet_ensure")(700, customer_id)
        runtime.get("wallet_admin_adjust")(700, Decimal("10"), admin_id=1, reason="اختبار دون تحويل حقيقي")
        withdrawal = runtime.get("wallet_create_withdrawal")(
            700,
            Decimal("2"),
            "BSC",
            "0x" + "a" * 40,
            "BINANCE",
            customer_fee=Decimal("0.03"),
        )
        available, locked = runtime.get("wallet_balances")(700)
        assert available == Decimal("7.97000000")
        assert locked == Decimal("2.03000000")
        assert Decimal(withdrawal["customer_fee_usdt"]) == Decimal("0.03000000")
        runtime.get("wallet_release_withdrawal")(
            int(withdrawal["id"]), error_code="V18_TEST", error_message="اختبار محلي"
        )
        available, locked = runtime.get("wallet_balances")(700)
        assert available == Decimal("10.00000000") and locked == Decimal("0E-8")

        # زر العميل الموثوق يعمل فعليًا ويعكس القيمة في قاعدة البيانات والواجهة.
        query = FakeQuery(f"cust_trusted_toggle:{customer_id}")
        update = SimpleNamespace(callback_query=query)
        context = FakeContext()
        asyncio.run(v18_features.on_callback(update, context))
        assert int(dbx.get_customer(customer_id)["is_trusted"]) == 1
        assert query.edited and "عميل موثوق" in query.edited[0]
        asyncio.run(v18_features.on_callback(update, context))
        assert int(dbx.get_customer(customer_id)["is_trusted"]) == 0

        html = (ROOT / "bot" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
        for feature in ("DeviceStorage", "safeAreaInset", "addToHomeScreen", "homeScreenAdded"):
            assert feature in html

        print(
            "PASS: V18 fee sharing, fee-safe ledger, managed networks, maintenance, "
            "trusted-customer toggle, leading-zero Jaib IDs, integer YER, and Telegram Mini App features"
        )


if __name__ == "__main__":
    main()

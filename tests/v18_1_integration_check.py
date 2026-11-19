#!/usr/bin/env python3
"""Offline acceptance checks for the V18.1 operational update."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from final_integration_check import FakeContext, FakeUser, install_dependency_stubs


def _chat_ids(context: FakeContext) -> list[int | str]:
    return [message[1].get("chat_id") for message in context.bot.messages]


def main() -> None:
    install_dependency_stubs()
    with tempfile.TemporaryDirectory(prefix="jaib-v18-1-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": "TEST_ONLY_TELEGRAM_TOKEN_NOT_VALID",
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "v18_1.db"),
                "LOG_FILE": str(root / "v18_1.log"),
                "BINANCE_WITHDRAW_ENABLED": "false",
                "PUBLIC_LIQUIDITY_ENABLED_DEFAULT": "false",
            }
        )

        from bot.app import runtime

        dbx = runtime.get("DBX")
        assert runtime.get("APP_VERSION") == "20.0.0"

        # Three separately managed destinations, with legacy settings mirrored safely.
        runtime.get("set_users_channel")("-100111", "قناة المستخدمين")
        runtime.get("set_admin_channel")("-100222", "قناة الأدمن")
        runtime.get("set_project_backup_channel")("-100333", "قناة النسخ")
        assert runtime.get("users_channel_id")() == -100111
        assert runtime.get("admin_channel_id")() == -100222
        assert runtime.get("unified_notification_channel_id")() == -100222
        assert runtime.get("project_backup_channel_id")() == -100333
        assert dbx.get_setting("notification_channel_id") == "-100111"
        assert dbx.get_setting("technical_channel_id") == "-100222"
        assert dbx.get_setting("account_verification_channel_id") == "-100222"
        assert dbx.get_setting("backup_channel_id") == "-100333"

        admin_context = FakeContext()
        asyncio.run(runtime.get("notify_admin")(admin_context, "اختبار حدث تشغيلي"))
        assert -100222 in _chat_ids(admin_context)
        assert -100111 not in _chat_ids(admin_context)

        # A completed withdrawal is the only financial event published publicly.
        telegram_id = 801
        customer_id = dbx.create_customer(
            "مستخدم اختبار الحذف",
            "777100801",
            telegram_id=telegram_id,
            telegram_username="delete_test",
        )
        runtime.get("register_bot_user")(FakeUser(telegram_id, "delete_test"))
        original_request = runtime.get("ensure_account_verification")(
            FakeUser(telegram_id, "delete_test")
        )
        runtime.get("wallet_ensure")(telegram_id, customer_id)
        runtime.get("wallet_admin_adjust")(
            telegram_id,
            Decimal("5"),
            admin_id=1,
            reason="اختبار حذف كامل مع رصيد",
        )
        withdrawal = runtime.get("wallet_create_withdrawal")(
            telegram_id,
            Decimal("2"),
            "BSC",
            "0x" + "b" * 40,
            "BINANCE",
            customer_fee=Decimal("0"),
        )
        completed = runtime.get("wallet_complete_withdrawal")(
            int(withdrawal["id"]),
            "0x" + "c" * 64,
        )
        dbx.audit(1, "Test Admin", "VIEW_CUSTOMER", "customer", customer_id, "must be purged")
        public_context = FakeContext()
        assert asyncio.run(
            runtime.get("wallet_publish_withdrawal_success")(public_context, completed)
        ) is True
        assert _chat_ids(public_context) == [-100111]

        # Permanent deletion works despite a non-zero balance and purges related data.
        summary = dbx.customer_deletion_summary(customer_id)
        assert summary is not None
        assert summary["available_balance"] == Decimal("3.00000000")
        assert summary["operation_count"] >= 1
        deleted = dbx.delete_customer(customer_id)
        assert deleted["customers"] == 1
        assert dbx.get_customer(customer_id) is None
        assert dbx.get_customer_by_telegram(telegram_id) is None
        with dbx.connect() as con:
            for table, column in (
                ("user_wallets", "telegram_user_id"),
                ("wallet_ledger", "telegram_user_id"),
                ("wallet_withdrawals", "telegram_user_id"),
                ("wallet_notification_outbox", "telegram_user_id"),
                ("account_verification_requests", "telegram_id"),
                ("bot_users", "telegram_id"),
            ):
                assert con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                    (telegram_id,),
                ).fetchone()[0] == 0, table
            assert con.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE entity_type='customer' AND entity_id=?",
                (str(customer_id),),
            ).fetchone()[0] == 0
        fresh_request = runtime.get("ensure_account_verification")(
            FakeUser(telegram_id, "delete_test")
        )
        assert fresh_request["status"] == "DRAFT"
        assert fresh_request["request_code"] != original_request["request_code"]

        # Unlinked admin-created customers are also purged by customer_id.
        unlinked_id = dbx.create_customer("عميل غير مربوط", "777100802")
        dbx.audit(1, "Test Admin", "VIEW_CUSTOMER", "customer", unlinked_id, "must be purged")
        assert dbx.customer_deletion_summary(unlinked_id)["operation_count"] == 0
        assert dbx.delete_customer(unlinked_id)["customers"] == 1
        assert dbx.get_customer(unlinked_id) is None

        # Health check is safe when secrets are absent: no payment/query is attempted.
        health = runtime.get("jaib_health_check")()
        assert health["ok"] is False
        assert health["status"] == "DISCONNECTED"
        assert "إعدادات الاتصال غير مكتملة" in health["reason"]

        webapp_html = (ROOT / "bot" / "webapp" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        webapp_server = (ROOT / "bot" / "webapp" / "server.py").read_text(
            encoding="utf-8"
        )
        withdrawal_ui = (ROOT / "bot" / "services" / "dynamic_withdrawals.py").read_text(
            encoding="utf-8"
        )
        pdf_source = (ROOT / "bot" / "services" / "pdf_reports.py").read_text(
            encoding="utf-8"
        )
        app_source = (ROOT / "bot" / "application.py").read_text(encoding="utf-8")

        for field in ("setChannelUsers", "setChannelAdmin", "setChannelBackup"):
            assert field in webapp_html
        assert "checkJaibHealth" in webapp_html
        assert webapp_html.count("confirm(") >= 2
        assert "تمت الموافقة على طلبك ✅" in webapp_server
        assert "تم رفض طلبك ❌" in webapp_server
        assert "wallet_enqueue_user_notification" in webapp_server
        assert "الحد الأدنى للسحب" in withdrawal_ui
        assert "الرسم" not in withdrawal_ui
        assert "jaib-health-monitor" in app_source
        assert "#CE1230" in pdf_source and "#26A17B" in pdf_source
        assert "#112E81" not in pdf_source and "#FFD400" not in pdf_source

        print(
            "PASS: V18.1 channels, Jaib health, permanent deletion, Mini App "
            "notifications, live withdrawal minimum, fee wording, and PDF palette"
        )


if __name__ == "__main__":
    main()

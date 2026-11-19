#!/usr/bin/env python3
"""فحوصات V17: Binance فقط، فحص 30 ثانية، وطابور السحب أثناء الانقطاع."""
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

from tests.final_integration_check import FakeBot, install_dependency_stubs


def main() -> None:
    install_dependency_stubs()
    assert not (ROOT / "bot/providers/aptos.py").exists()
    assert not (ROOT / "bot/admin/bybit.py").exists()
    app_source = (ROOT / "bot/app.py").read_text(encoding="utf-8").lower()
    assert "providers.aptos" not in app_source
    assert "admin.bybit" not in app_source
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8").lower()
    assert "aptos" not in env_source
    assert "bybit" not in env_source
    assert "binance_auth_cooldown" not in env_source

    with tempfile.TemporaryDirectory(prefix="jaib-v17-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": "TEST_ONLY_TOKEN",
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "v17.db"),
                "LOG_FILE": str(root / "v17.log"),
                "BINANCE_API_KEY": "V17_TEST_API_KEY",
                "BINANCE_API_SECRET": "V17_TEST_API_SECRET",
                "BINANCE_WITHDRAW_ENABLED": "true",
                "AUTO_ONCHAIN_WITHDRAW": "true",
                "BEP20_ENABLED": "true",
                "ALLOWED_NETWORKS": "BSC:BEP20",
                "BINANCE_EGRESS_IP_CHECK_ENABLED": "false",
            }
        )

        from bot.app import runtime
        import bot.providers.binance as binance_module
        import bot.providers.binance_wallet as wallet_provider_module

        dbx = runtime.get("DBX")
        health_check = runtime.get("binance_health_check")
        auth_state = runtime.get("binance_auth_state")
        clear_failure = runtime.get("clear_binance_auth_failure")
        mark_unavailable = runtime.get("mark_binance_unavailable")
        claim_failure = runtime.get("binance_claim_failure_notification")
        wallet_admin_adjust = runtime.get("wallet_admin_adjust")
        wallet_balances = runtime.get("wallet_balances")
        wallet_create = runtime.get("wallet_create_withdrawal")
        wallet_get = runtime.get("wallet_get_withdrawal")
        wallet_submit = runtime.get("wallet_submit_withdrawal")
        monitor_withdrawals = runtime.get("monitor_wallet_withdrawals")
        monitor_health = runtime.get("monitor_binance_health")
        BinanceIndividualError = runtime.get("BinanceIndividualError")
        BinanceTransportError = runtime.get("BinanceTransportError")

        # فحص المفتاح والصلاحيات والشبكة عند التشغيل.
        original_permissions = binance_module.BINANCE.get_api_key_permissions
        original_load_config = binance_module.BINANCE._load_coin_config
        original_ip = binance_module.binance_egress_ip
        binance_module.binance_egress_ip = lambda **_kwargs: "203.0.113.17"
        binance_module.BINANCE.get_api_key_permissions = lambda: {
            "enableReading": True,
            "enableWithdrawals": True,
        }
        binance_module.BINANCE._load_coin_config = lambda _coin: {
            "withdrawAllEnable": True,
            "networkList": [{"network": "BSC", "withdrawEnable": True, "busy": False}],
        }
        try:
            result = health_check(force_ip=True)
            assert result["ok"] is True
            assert auth_state()["blocked"] is False
            assert "blocked_until_epoch" not in auth_state()

            binance_module.BINANCE.get_api_key_permissions = lambda: {
                "enableReading": True,
                "enableWithdrawals": False,
            }
            result = health_check(force_ip=True)
            assert result["ok"] is False
            assert "صلاحية السحب" in result["reason"]
            assert auth_state()["blocked"] is True
            assert claim_failure() is True
            assert claim_failure() is False
        finally:
            binance_module.BINANCE.get_api_key_permissions = original_permissions
            binance_module.BINANCE._load_coin_config = original_load_config
            binance_module.binance_egress_ip = original_ip

        dbx.create_customer("V17 User", "777007017", telegram_id=7017)
        wallet_admin_adjust(7017, Decimal("20"), admin_id=1, reason="اختبار V17")
        context = SimpleNamespace(bot=FakeBot())

        class FakeQuote:
            fee = Decimal("0.1")
            raw_network = {"network": "BSC"}

        class SuccessProvider:
            def quote(self, *_args):
                return FakeQuote()

            def submit(self, _quote, _address, request_id):
                return {"id": f"BINANCE-{request_id}"}

            def status(self, _row):
                return {"completed": True, "failed": False, "txid": "V17-TX", "raw": {"status": 6}}

        original_provider = wallet_provider_module.BINANCE_PROVIDER
        original_health = wallet_provider_module.binance_health_check
        try:
            # الطلب يبقى محجوزًا أثناء الانقطاع، ثم ينفذ تلقائيًا فور نجاح الفحص.
            mark_unavailable(BinanceTransportError("انقطاع اختبار"))
            queued = wallet_create(
                7017, Decimal("5"), "BSC", "0x" + "1" * 40, "BINANCE", minimum_amount=Decimal("1")
            )
            asyncio.run(wallet_submit(int(queued["id"]), context, initiated_by_admin=False))
            assert wallet_get(int(queued["id"]))["status"] == "AUTO_PENDING"
            assert wallet_balances(7017) == (Decimal("15.00000000"), Decimal("5.00000000"))

            wallet_provider_module.BINANCE_PROVIDER = SuccessProvider()
            wallet_provider_module.binance_health_check = lambda **_kwargs: {
                **clear_failure(), "ok": True, "egress_ip": "203.0.113.17"
            }
            asyncio.run(monitor_health(context))
            assert wallet_get(int(queued["id"]))["status"] == "COMPLETED"
            assert wallet_balances(7017) == (Decimal("15.00000000"), Decimal("0E-8"))
            assert any("اكتمل السحب" in item[1].get("text", "") for item in context.bot.messages)

            # خطأ عنوان/طلب فردي يعيد الرصيد ولا يوقف بقية المستخدمين.
            class IndividualFailureProvider(SuccessProvider):
                def quote(self, *_args):
                    raise BinanceIndividualError("عنوان السحب غير صالح")

            wallet_provider_module.BINANCE_PROVIDER = IndividualFailureProvider()
            clear_failure()
            before = wallet_balances(7017)
            individual = wallet_create(
                7017, Decimal("3"), "BSC", "0x" + "2" * 40, "BINANCE", minimum_amount=Decimal("1")
            )
            asyncio.run(wallet_submit(int(individual["id"]), context, initiated_by_admin=False))
            assert wallet_get(int(individual["id"]))["status"] == "FAILED_RELEASED"
            assert wallet_balances(7017) == before
            assert auth_state()["blocked"] is False
        finally:
            wallet_provider_module.BINANCE_PROVIDER = original_provider
            wallet_provider_module.binance_health_check = original_health

    print("PASS: Binance-only provider, startup health check, no fixed cooldown, 30-second recovery queue, and isolated user errors")


if __name__ == "__main__":
    main()

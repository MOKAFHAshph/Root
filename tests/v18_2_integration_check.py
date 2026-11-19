#!/usr/bin/env python3
"""Offline acceptance checks for V18.2 customer pricing and withdrawal UX."""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from final_integration_check import install_dependency_stubs


def main() -> None:
    install_dependency_stubs()
    with tempfile.TemporaryDirectory(prefix="jaib-v18-2-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": "TEST_ONLY_TELEGRAM_TOKEN_NOT_VALID",
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "v18_2.db"),
                "LOG_FILE": str(root / "v18_2.log"),
                "BINANCE_WITHDRAW_ENABLED": "false",
                "PUBLIC_LIQUIDITY_ENABLED_DEFAULT": "false",
            }
        )

        from bot.app import runtime

        dbx = runtime.get("DBX")
        assert runtime.get("APP_VERSION") == "20.0.0"

        # The migration is additive and keeps all configuration in SQLite.
        with dbx.connect() as con:
            customer_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(customers)")
            }
            network_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(managed_networks)")
            }
            assert "custom_exchange_rate_yer" in customer_columns
            assert "minimum_floor" in network_columns
            assert con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tutorial_buttons'"
            ).fetchone()

        # A customer-specific rate overrides the general rate and is locked per deposit.
        telegram_id = 820
        customer_id = dbx.create_customer(
            "عميل السعر الخاص", "777100820", telegram_id=telegram_id
        )
        dbx.set_setting("usdt_rate_yer", "550.00")
        assert runtime.get("effective_customer_rate")(telegram_id) == Decimal("550.00")
        runtime.get("set_customer_custom_rate")(customer_id, "545")
        assert runtime.get("effective_customer_rate")(telegram_id) == Decimal("545.00")
        assert runtime.get("wallet_prepare_deposit")(telegram_id)["exchange_rate_yer"] == "545.00"
        deposit = runtime.get("wallet_create_deposit")(telegram_id, Decimal("545"))
        runtime.get("set_customer_custom_rate")(customer_id, "540")
        assert Decimal(deposit["exchange_rate_yer"]) == Decimal("545.00")
        assert Decimal(deposit["usdt_amount"]) == Decimal("1.00000000")
        assert runtime.get("wallet_prepare_deposit")(telegram_id)["exchange_rate_yer"] == "540.00"
        runtime.get("set_customer_custom_rate")(customer_id, None)
        assert runtime.get("effective_customer_rate")(telegram_id) == Decimal("550.00")

        # The effective withdrawal minimum is the higher provider/admin value.
        network = runtime.get("get_managed_network")("BSC")
        runtime.get("set_managed_network_minimum_floor")(int(network["id"]), "10")
        assert runtime.get("managed_network_minimum_floor")("BSC") == Decimal("10.00000000")
        assert runtime.get("effective_network_minimum")("BSC", "2.5") == Decimal("10.00000000")
        assert runtime.get("effective_network_minimum")("BSC", "12") == Decimal("12.00000000")
        runtime.get("set_managed_network_minimum_floor")(int(network["id"]), None)
        assert runtime.get("managed_network_minimum_floor")("BSC") == Decimal("0")

        # Tutorial buttons validate HTTPS, support optional custom emoji, and reorder safely.
        tutorial = runtime.get("create_tutorial_button")(
            "كيفية السحب",
            "https://example.com/withdraw",
            "📹",
            admin_id=1,
            custom_emoji_id="5368324170671202286",
        )
        assert tutorial["custom_emoji_id"] == "5368324170671202286"
        assert runtime.get("tutorial_custom_emoji_enabled")() is False
        runtime.get("set_tutorial_custom_emoji_enabled")(True)
        assert runtime.get("tutorial_custom_emoji_enabled")() is True
        runtime.get("set_tutorial_custom_emoji_enabled")(False)
        updated = runtime.get("update_tutorial_button")(
            int(tutorial["id"]), name="السحب عبر BSC", active=False, sort_order=5
        )
        assert updated["name"] == "السحب عبر BSC" and int(updated["active"]) == 0
        assert not runtime.get("list_tutorial_buttons")(active_only=True)
        try:
            runtime.get("create_tutorial_button")(
                "رابط غير آمن", "http://example.com", "📹", admin_id=1
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Non-HTTPS tutorial URL was accepted")
        runtime.get("delete_tutorial_button")(int(tutorial["id"]))

        # Mini App admin endpoints use the same services and permission gate.
        import bot.webapp.server as webapp

        endpoint_rate = webapp.admin_set_user_exchange_rate(
            telegram_id,
            webapp.CustomerExchangeRateRequest(rate=Decimal("543")),
            {"id": 1},
        )
        assert endpoint_rate["custom_exchange_rate_yer"] == "543.00"
        endpoint_network = webapp.admin_update_managed_network(
            int(network["id"]),
            webapp.ManagedNetworkUpdateRequest(minimum_floor=Decimal("11")),
            {"id": 1},
        )
        assert Decimal(endpoint_network["item"]["minimum_floor"]) == Decimal("11")
        endpoint_tutorial = webapp.admin_create_tutorial_button(
            webapp.TutorialButtonCreateRequest(
                name="تعليم Mini App",
                url="https://example.com/miniapp",
                icon="📖",
                custom_emoji_id=None,
            ),
            {"id": 1},
        )
        assert endpoint_tutorial["item"]["name"] == "تعليم Mini App"
        webapp.admin_delete_tutorial_button(int(endpoint_tutorial["item"]["id"]), {"id": 1})

        # Binance off-chain labels never leak into the user-facing/copy identifier.
        offchain_txid = "Off-chain transfer 400391218260"
        assert runtime.get("normalize_txid")(offchain_txid) == "400391218260"
        assert runtime.get("normalize_txid")("off-chain transfer: 400391218260") == "400391218260"

        # Future withdrawals persist the normalized value while retaining the raw response.
        runtime.get("wallet_ensure")(telegram_id)
        runtime.get("wallet_admin_adjust")(
            telegram_id,
            Decimal("20"),
            reason="اختبار V18.2.1",
            admin_id=1,
        )
        withdrawal = runtime.get("wallet_create_withdrawal")(
            telegram_id,
            Decimal("1"),
            "BSC",
            "0x1111111111111111111111111111111111111111",
            customer_fee=Decimal("0"),
        )
        completed = runtime.get("wallet_complete_withdrawal")(
            int(withdrawal["id"]), offchain_txid, {"txId": offchain_txid}
        )
        assert completed["txid"] == "400391218260"
        assert offchain_txid in str(completed["raw_json"])

        # Full on-chain TxID stays available while public masking is deterministic.
        txid = "0x7f3a" + "a" * 50 + "3b9c"
        assert runtime.get("normalize_txid")(txid) == txid
        assert runtime.get("mask_txid")(txid) == "0x7f3a••••••••••••3b9c"

        webapp = (ROOT / "bot" / "webapp" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        server = (ROOT / "bot" / "webapp" / "server.py").read_text(encoding="utf-8")
        wallet_ui = (ROOT / "bot" / "ui" / "wallet.py").read_text(encoding="utf-8")
        for marker in (
            "withdrawMinimum",
            "data-copy-value=\"${txid}\"",
            "tutorialButtonsSupport",
            "setCustomerRate",
            "manageTutorials",
            "data-network-minimum",
        ):
            assert marker in webapp, marker
        for marker in (
            "/api/admin/users/{telegram_id}/exchange-rate",
            "/api/admin/tutorial-buttons",
            "minimum_floor",
            '"txid": normalize_txid(operation.get("txid"))',
        ):
            assert marker in server, marker
        for marker in (
            "<b>⚠️ الحد الأدنى للسحب</b>",
            "يتم تحديثه تلقائيًا حسب إعداد الشبكة",
            "format_display_amount(min_withdraw)",
        ):
            assert marker in wallet_ui, marker

        print(
            "PASS: V18.2.1 per-customer locked pricing, highlighted withdrawal floor, "
            "numeric-only off-chain TxID storage/copy, managed tutorials, optional "
            "animated emoji, and Mini App controls"
        )


if __name__ == "__main__":
    main()

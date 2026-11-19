#!/usr/bin/env python3
"""Offline check for the portable Arabic ReportLab PDF engine."""
from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    source = (ROOT / "bot/services/pdf_reports.py").read_text(encoding="utf-8").lower()
    assert "weasyprint" not in requirements
    assert "from weasyprint" not in source
    assert "reportlab" in requirements
    assert "arabic-reshaper" in requirements
    assert "python-bidi" in requirements

    from bot import runtime

    class WalletUserError(Exception):
        pass

    class FakeDB:
        @staticmethod
        def get_customer_by_telegram(user_id: int):
            return {
                "full_name": "مستخدم اختبار",
                "status": "ACTIVE",
                "telegram_username": "pdf_test",
                "phone_primary": "777000111",
                "phone_alt": "",
            }

    with tempfile.TemporaryDirectory(prefix="bn-wallet-pdf-pdf-") as temp:
        base = Path(temp)
        export_dir = base / "exports"
        export_dir.mkdir()
        runtime.export_globals(
            {
                "env": lambda name, default=None, required=False: default or "",
                "APP_NAME": "بوت بيع USDT",
                "BASE_DIR": base,
                "EXPORT_DIR": export_dir,
                "DBX": FakeDB(),
                "WalletUserError": WalletUserError,
                "wallet_decimal": lambda value: Decimal(str(value or 0)),
                "q2": lambda value: f"{Decimal(str(value or 0)):.2f}",
                "format_display_amount": lambda value, places=None: f"{Decimal(str(value or 0)):.{4 if places is None else places}f}",
                "wallet_user_stats": lambda user_id: {
                    "first_used_at": "2026-07-01 10:00:00",
                    "last_at": "2026-07-22 11:00:00",
                    "purchase_count": 2,
                    "purchase_usdt": Decimal("20"),
                    "withdraw_count": 1,
                    "withdraw_usdt": Decimal("5"),
                    "purchase_yer": Decimal("11000"),
                    "available": Decimal("15"),
                    "referral_count": 3,
                    "referral_balance": Decimal("1.5"),
                    "operation_count": 3,
                    "successful_operations": 3,
                    "success_rate": 100.0,
                    "locked": Decimal("0"),
                },
                "wallet_report_operations": lambda user_id: [
                    {
                        "created_at": "2026-07-22 10:00:00",
                        "type_label": "شراء USDT",
                        "amount": Decimal("10"),
                        "currency": "USDT",
                        "status": "COMPLETED",
                        "reference": "REF-001",
                    },
                    {
                        "created_at": "2026-07-22 10:30:00",
                        "type_label": "سحب USDT",
                        "amount": Decimal("5"),
                        "currency": "USDT",
                        "status": "COMPLETED",
                        "reference": "TX-001",
                    },
                ],
                "log": logging.getLogger("v61-pdf-test"),
            }
        )

        spec = importlib.util.spec_from_file_location(
            "bot.services.pdf_reports_v61_test",
            ROOT / "bot/services/pdf_reports.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        # The modular compatibility registry exports PIL.Image before this module.
        # Ensure ReportLab aliases cannot be overwritten during runtime.sync_all().
        from PIL import Image as PILImage
        runtime.export_globals({"Image": PILImage})
        runtime.sync_all()
        assert module.RLImage.__module__.startswith("reportlab")

        report = module.generate_user_pdf_report(123456)
        assert report.is_file()
        assert report.read_bytes().startswith(b"%PDF")
        assert report.stat().st_size > 10_000

    print("PASS: portable Arabic ReportLab PDF engine")


if __name__ == "__main__":
    main()

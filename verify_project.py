#!/usr/bin/env python3
"""Run the project's offline structural and integration verification suite."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*command: str) -> None:
    printable = " ".join(command)
    print(f"$ {printable}")
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


run(sys.executable, "-m", "compileall", "-q", "bot", "run.py", "webapp_main.py", "run_all.py")
run(sys.executable, "tests/final_integration_check.py")
run(sys.executable, "tests/account_verification_check.py")
run(sys.executable, "tests/v18_integration_check.py")
run(sys.executable, "tests/v18_1_integration_check.py")
run(sys.executable, "tests/v18_2_integration_check.py")
run(sys.executable, "tests/v14_ui_check.py")
run(sys.executable, "tests/v17_binance_only_check.py")
run(sys.executable, "tests/pdf_engine_check.py")
run(sys.executable, "tests/backup_automation_check.py")

run(sys.executable, "-m", "pytest", "tests/test_upgrade.py", "tests/test_v20.py", "-q")

print("PASS: all offline project verification checks completed.")

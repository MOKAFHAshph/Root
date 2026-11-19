"""Base backup and CSV export utilities.

"""
from __future__ import annotations

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# نسخ احتياطي وتصدير
# ============================================================


def create_backup_file() -> Path:
    snapshot = DBX.export_snapshot()
    path = BACKUP_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_csv_exports() -> tuple[Path, Path]:
    snapshot = DBX.export_snapshot()
    customers_path = EXPORT_DIR / f"customers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    orders_path = EXPORT_DIR / f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with customers_path.open("w", newline="", encoding="utf-8-sig") as f:
        rows = snapshot["customers"]
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    with orders_path.open("w", newline="", encoding="utf-8-sig") as f:
        rows = snapshot["orders_v2"]
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return customers_path, orders_path




_export_globals(globals())

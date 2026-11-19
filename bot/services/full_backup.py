"""Full-system backup extensions for wallet, registration, and technical data.

The module extends the established snapshot implementation without duplicating
the original backup or restore workflow.
"""
from __future__ import annotations

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- نسخ احتياطي شامل ----------------


_wallet_original_export_snapshot = DB.export_snapshot
_wallet_original_import_snapshot = DB.import_snapshot


def _wallet_export_snapshot_impl(self: DB) -> dict[str, Any]:
    payload = _wallet_original_export_snapshot(self)
    with self.connect() as con:
        for table in ("user_wallets", "wallet_deposits", "wallet_withdrawals", "wallet_ledger", "technical_events", "user_registrations", "referrals", "account_verification_requests"):
            payload[table] = [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
    return payload


def _wallet_import_snapshot_impl(self: DB, payload: dict[str, Any]):
    table_keys = {
        "user_wallets": "telegram_user_id",
        "wallet_deposits": "id",
        "wallet_withdrawals": "id",
        "wallet_ledger": "id",
        "technical_events": "id",
        "user_registrations": "telegram_id",
        "referrals": "id",
        "account_verification_requests": "id",
    }

    # Remove wallet-layer rows before the base restore replaces customers and
    # orders. This prevents stale records from surviving a rollback to an older
    # backup and avoids foreign-key conflicts during customer replacement.
    delete_order = (
        "wallet_ledger",
        "wallet_deposits",
        "wallet_withdrawals",
        "user_wallets",
        "referrals",
        "user_registrations",
        "technical_events",
        "account_verification_requests",
    )
    with self.connect() as con:
        con.execute("PRAGMA foreign_keys = OFF")
        existing_tables = {
            row["name"]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for table in delete_order:
            if table in existing_tables:
                con.execute(f"DELETE FROM {table}")
        con.commit()
        con.execute("PRAGMA foreign_keys = ON")

    _wallet_original_import_snapshot(self, payload)

    with self.connect() as con:
        con.execute("PRAGMA foreign_keys = OFF")
        for table, primary in table_keys.items():
            rows = payload.get(table, [])
            if not isinstance(rows, list):
                raise ValueError(f"Invalid backup table payload: {table}")
            if not rows:
                continue
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()]
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"Invalid row in backup table: {table}")
                usable = {key: row.get(key) for key in columns if key in row}
                if not usable or primary not in usable:
                    raise ValueError(f"Incomplete row in backup table: {table}")
                names = list(usable)
                placeholders = ",".join("?" for _ in names)
                con.execute(
                    f"INSERT OR REPLACE INTO {table}({','.join(names)}) VALUES ({placeholders})",
                    [usable[name] for name in names],
                )
        con.commit()
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        con.execute("PRAGMA foreign_keys = ON")
        if violations:
            raise ValueError(f"Backup restore contains foreign-key violations: {len(violations)}")


DB.export_snapshot = _wallet_export_snapshot_impl
DB.import_snapshot = _wallet_import_snapshot_impl




_export_globals(globals())

"""Wallet history, user/system statistics, and CSV exports.

"""
from __future__ import annotations

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

def wallet_recent_ledger(telegram_user_id: int, limit: int = WALLET_RECENT_LIMIT) -> list[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM wallet_ledger WHERE telegram_user_id=? ORDER BY id DESC LIMIT ?",
            (int(telegram_user_id), int(limit)),
        ).fetchall()


def wallet_user_stats(telegram_user_id: int) -> dict[str, Any]:
    wallet = wallet_get(telegram_user_id)
    with DBX.connect() as con:
        deposits = con.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(CAST(fiat_amount_yer AS REAL)),0) AS yer,
                   COALESCE(SUM(CAST(usdt_amount AS REAL)),0) AS usdt,
                   MIN(completed_at) AS first_at, MAX(completed_at) AS last_at
            FROM wallet_deposits WHERE telegram_user_id=? AND status='COMPLETED'
            """,
            (int(telegram_user_id),),
        ).fetchone()
        withdrawals = con.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(CAST(amount_usdt AS REAL)),0) AS usdt,
                   MIN(completed_at) AS first_at, MAX(completed_at) AS last_at
            FROM wallet_withdrawals WHERE telegram_user_id=? AND status='COMPLETED'
            """,
            (int(telegram_user_id),),
        ).fetchone()
    dates = [x for x in (deposits["first_at"], withdrawals["first_at"]) if x]
    lasts = [x for x in (deposits["last_at"], withdrawals["last_at"]) if x]
    return {
        "available": wallet_decimal(wallet["available_balance"]),
        "locked": wallet_decimal(wallet["locked_balance"]),
        "total": wallet_decimal(wallet["available_balance"]) + wallet_decimal(wallet["locked_balance"]),
        "deposit_count": int(deposits["count"] or 0),
        "deposit_yer": decimal_or_zero(deposits["yer"]),
        "deposit_usdt": decimal_or_zero(deposits["usdt"]),
        "withdraw_count": int(withdrawals["count"] or 0),
        "withdraw_usdt": decimal_or_zero(withdrawals["usdt"]),
        "first_at": min(dates) if dates else None,
        "last_at": max(lasts) if lasts else None,
    }


def wallet_system_stats() -> dict[str, Any]:
    with DBX.connect() as con:
        balances = con.execute(
            """
            SELECT COALESCE(SUM(CAST(available_balance AS REAL)),0) AS available,
                   COALESCE(SUM(CAST(locked_balance AS REAL)),0) AS locked,
                   COUNT(*) AS wallets
            FROM user_wallets
            """
        ).fetchone()
        dep = con.execute("SELECT COUNT(*) c, COALESCE(SUM(CAST(usdt_amount AS REAL)),0) s FROM wallet_deposits WHERE status='COMPLETED'").fetchone()
        wd = con.execute("SELECT COUNT(*) c, COALESCE(SUM(CAST(amount_usdt AS REAL)),0) s FROM wallet_withdrawals WHERE status='COMPLETED'").fetchone()
        pending = con.execute("SELECT COUNT(*) c FROM wallet_withdrawals WHERE status IN ('AUTO_PENDING','PROCESSING','SUBMITTED','STATUS_UNKNOWN','MANUAL_REVIEW')").fetchone()
    return {
        "wallets": int(balances["wallets"] or 0),
        "available": decimal_or_zero(balances["available"]),
        "locked": decimal_or_zero(balances["locked"]),
        "liabilities": decimal_or_zero(balances["available"]) + decimal_or_zero(balances["locked"]),
        "deposit_count": int(dep["c"] or 0),
        "deposit_usdt": decimal_or_zero(dep["s"]),
        "withdraw_count": int(wd["c"] or 0),
        "withdraw_usdt": decimal_or_zero(wd["s"]),
        "pending": int(pending["c"] or 0),
    }


def wallet_export_csv() -> tuple[Path, Path, Path]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    files: list[tuple[str, str]] = [
        ("wallets", "SELECT * FROM user_wallets ORDER BY telegram_user_id"),
        ("wallet_ledger", "SELECT * FROM wallet_ledger ORDER BY id"),
        ("wallet_withdrawals", "SELECT * FROM wallet_withdrawals ORDER BY id"),
    ]
    paths: list[Path] = []
    with DBX.connect() as con:
        for name, sql in files:
            path = EXPORT_DIR / f"{name}_{timestamp}.csv"
            rows = con.execute(sql).fetchall()
            headers = rows[0].keys() if rows else []
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                if headers:
                    writer.writerow(headers)
                    for row in rows:
                        writer.writerow([row[key] for key in headers])
            paths.append(path)
    return paths[0], paths[1], paths[2]




_export_globals(globals())

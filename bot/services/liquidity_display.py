"""Read-only, source-neutral liquidity *presentation* for the Telegram main menu.

No withdrawal/deposit/ledger methods are called or modified here.  The Binance
provider yields a fungible Spot USDT amount; its BSC grouping denotes the
configured withdrawal route, NOT on-chain custody on BSC.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

_LOG = logging.getLogger(__name__)
_TTL = 25  # Independent menu cache; the existing provider cache may be longer.
_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "value": None}
_ZERO = Decimal("0")


def _amount(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("nonfinite/negative")
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid liquidity amount") from None


def _dollars(value):
    amount = _amount(value).to_integral_value(rounding=ROUND_DOWN)
    return "$" + format(amount, ",.0f")


def merge_entries(entries):
    """Sum BEFORE flooring, by asset + network; for unit and mock tests."""
    merged = {}
    for asset, network, amount in entries:
        key = (str(asset).upper(), str(network).upper())
        merged[key] = merged.get(key, _ZERO) + _amount(amount)
    return merged


def _output(entries, missing):
    merged = merge_entries(entries)
    order = [("TON", "TON"), ("USDT", "TON"), ("USDT", "BEP20")]
    rows = []
    for asset, network in order:
        if (asset, network) not in merged:
            continue
        usd = merged[(asset, network)]
        label = ("TON — TON network" if asset == "TON" else
                 "USDT — TON network" if network == "TON" else "USDT — BEP20")
        rows.append({"asset": asset, "network": network, "label": label,
                     "dollars": _dollars(usd), "usd": str(usd),
                     "style": "primary" if asset == "TON" else "success"})
    # This deployment exposes only the verified BSC and TON routes (max 3).
    return {"items": rows[:3], "incomplete": bool(missing),
            "available_known": bool(rows), "available_display": "0"}


def get_unified_liquidity(*, force_refresh=False, binance_reader=None,
                          ton_reader=None, price_reader=None,
                          reserves_reader=None, routes_reader=None):
    """Best-effort, independent read-only sources. Errors never create zero balances.

    Injected readers are for offline tests, not a deployment configuration.
    """
    injected = any(v is not None for v in
                   (binance_reader, ton_reader, price_reader, reserves_reader, routes_reader))
    now = time.monotonic()
    if not injected and not force_refresh and _CACHE["value"] is not None and now - _CACHE["at"] < _TTL:
        return copy.deepcopy(_CACHE["value"])
    if not injected and not _LOCK.acquire(blocking=False):
        old = copy.deepcopy(_CACHE["value"])
        return old if old is not None else _output([], ["in_progress"])
    try:
        if not injected and not force_refresh and _CACHE["value"] is not None and time.monotonic() - _CACHE["at"] < _TTL:
            return copy.deepcopy(_CACHE["value"])
        from bot import runtime
        from bot.providers.ton_hot import TonHotWallet

        if routes_reader is None:
            routes_reader = runtime.get("wallet_network_routes")
        if binance_reader is None:
            # Use the existing READ balance method, without the 60-second public
            # liquidity cache used by withdrawal checks. Do not alter that path.
            def binance_reader(*, force_refresh=False):
                provider = runtime.get("BINANCE")
                coin = str(runtime.get("BINANCE_COIN")).upper()
                if coin != "USDT" or not provider.enabled():
                    raise RuntimeError("USDT balance read not enabled")
                free = _amount(provider.get_spot_free_balance(coin))
                reserve = _amount(runtime.get("public_liquidity_reserve")())
                return {"available_known": True, "stale": False,
                        "available": str(max(_ZERO, free - reserve))}
        if ton_reader is None:
            ton_reader = lambda: TonHotWallet().balances()
        if price_reader is None:
            price_reader = runtime.get("ton_rate")
        if reserves_reader is None:
            reserves_reader = runtime.get("ton_reserved")

        items, missing = [], []
        try:
            routes = routes_reader()
            bsc_route = any(str(r.get("code", "")).upper() == "BSC" for r in routes)
            if bsc_route:
                snapshot = binance_reader(force_refresh=False)
                if not snapshot.get("available_known") or snapshot.get("stale"):
                    raise RuntimeError("unavailable or stale liquidity snapshot")
                # Existing Binance withdrawal reserve is ALREADY deducted here.
                items.append(("USDT", "BEP20", _amount(snapshot["available"])))
        except Exception as exc:
            missing.append("binance")
            _LOG.warning("liquidity display source=binance temporarily unavailable (%s)", type(exc).__name__)

        try:
            balances = ton_reader()  # Public TON address only; no seed is read.
            reserved_usdt, reserved_ton = reserves_reader()  # Read-only reservations.
            usdt = max(_ZERO, _amount(balances["usdt"]) - _amount(reserved_usdt))
            native_ton = max(_ZERO, _amount(balances["ton"]) - _amount(reserved_ton) - Decimal("0.05"))
            items.append(("USDT", "TON", usdt))
            if native_ton == 0:
                items.append(("TON", "TON", _ZERO))
            else:
                try:
                    quote = price_reader()
                    if not quote or quote.get("stale"):
                        raise ValueError("missing/frozen TON USD quote")
                    price = _amount(quote["usd"])
                    if price <= 0:
                        raise ValueError("invalid TON USD quote")
                    items.append(("TON", "TON", native_ton * price))
                except Exception as exc:
                    missing.append("ton_usd_price")
                    _LOG.warning("liquidity display source=ton_price temporarily unavailable (%s)", type(exc).__name__)
        except Exception as exc:
            missing.append("ton")
            _LOG.warning("liquidity display source=ton temporarily unavailable (%s)", type(exc).__name__)

        result = _output(items, missing)
        if not injected:
            _CACHE["at"] = time.monotonic()
            _CACHE["value"] = copy.deepcopy(result)
        return result
    finally:
        if not injected:
            _LOCK.release()

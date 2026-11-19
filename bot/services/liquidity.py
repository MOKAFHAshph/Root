"""Public liquidity display and configurable per-operation withdrawal limits.

The public label deliberately avoids exposing the payout provider name.  Live
provider balance is cached and reduced by an administrator-controlled reserve
before it is shown to customers.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals

_seed_globals(globals())

PUBLIC_LIQUIDITY_ENABLED_DEFAULT = env("PUBLIC_LIQUIDITY_ENABLED_DEFAULT", "true").lower() == "true"
PUBLIC_LIQUIDITY_LABEL = env("PUBLIC_LIQUIDITY_LABEL", "السيولة المتوفرة حالياً").strip() or "السيولة المتوفرة حالياً"
PUBLIC_LIQUIDITY_RESERVE_USDT_DEFAULT = max(
    Decimal("0"), Decimal(env("PUBLIC_LIQUIDITY_RESERVE_USDT", "1"))
)
PUBLIC_LIQUIDITY_CACHE_SECONDS = max(10, int(env("PUBLIC_LIQUIDITY_CACHE_SECONDS", "60")))
PUBLIC_LIQUIDITY_DECIMALS = min(8, max(0, int(env("PUBLIC_LIQUIDITY_DECIMALS", "2"))))
WITHDRAWAL_OPERATION_LIMIT_ENABLED_DEFAULT = env(
    "WITHDRAWAL_OPERATION_LIMIT_ENABLED_DEFAULT", "true"
).lower() == "true"
WITHDRAWAL_OPERATION_LIMIT_USDT_DEFAULT = max(
    Decimal("0"), Decimal(env("WITHDRAWAL_OPERATION_LIMIT_USDT", "100"))
)
JAIB_SECONDARY_BENEFICIARY_NAME = env(
    "JAIB_SECONDARY_BENEFICIARY_NAME", "عبدالرحمن علي"
).strip() or "عبدالرحمن علي"

_cache_lock = threading.Lock()
_liquidity_cache: dict[str, Any] = {
    "fetched_at": 0.0,
    "free": Decimal("0"),
    "available": Decimal("0"),
    "error": "",
}


def _bool_setting(key: str, default: bool) -> bool:
    raw = DBX.get_setting(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def public_liquidity_enabled() -> bool:
    return _bool_setting("public_liquidity_enabled", PUBLIC_LIQUIDITY_ENABLED_DEFAULT)


def set_public_liquidity_enabled(value: bool) -> None:
    DBX.set_setting("public_liquidity_enabled", "1" if value else "0")


def public_liquidity_reserve() -> Decimal:
    raw = DBX.get_setting("public_liquidity_reserve_usdt")
    try:
        return max(Decimal("0"), wallet_decimal(raw if raw is not None else PUBLIC_LIQUIDITY_RESERVE_USDT_DEFAULT))
    except Exception:
        return wallet_decimal(PUBLIC_LIQUIDITY_RESERVE_USDT_DEFAULT)


def set_public_liquidity_reserve(value: Any) -> None:
    amount = wallet_decimal(value)
    if amount < 0:
        raise WalletUserError("احتياطي السيولة لا يمكن أن يكون سالبًا.")
    DBX.set_setting("public_liquidity_reserve_usdt", q8(amount))


def liquidity_alert_enabled() -> bool:
    return _bool_setting(
        "liquidity_alert_enabled",
        bool(globals().get("LIQUIDITY_ALERT_ENABLED_DEFAULT", True)),
    )


def set_liquidity_alert_enabled(value: bool) -> None:
    DBX.set_setting("liquidity_alert_enabled", "1" if value else "0")


def liquidity_alert_threshold() -> Decimal:
    default = globals().get("LIQUIDITY_ALERT_THRESHOLD_USDT_DEFAULT", Decimal("20"))
    try:
        return max(Decimal("0"), wallet_decimal(DBX.get_setting("liquidity_alert_threshold_usdt") or default))
    except Exception:
        return wallet_decimal(default)


def set_liquidity_alert_threshold(value: Any) -> None:
    amount = wallet_decimal(value)
    if amount < 0:
        raise WalletUserError("حد تنبيه السيولة لا يمكن أن يكون سالبًا.")
    DBX.set_setting("liquidity_alert_threshold_usdt", q8(amount))
    if liquidity_alert_recovery() < amount:
        DBX.set_setting("liquidity_alert_recovery_usdt", q8(amount))


def liquidity_alert_recovery() -> Decimal:
    default = globals().get("LIQUIDITY_ALERT_RECOVERY_USDT_DEFAULT", Decimal("25"))
    try:
        return max(liquidity_alert_threshold(), wallet_decimal(DBX.get_setting("liquidity_alert_recovery_usdt") or default))
    except Exception:
        return max(liquidity_alert_threshold(), wallet_decimal(default))


def set_liquidity_alert_recovery(value: Any) -> None:
    amount = wallet_decimal(value)
    if amount < liquidity_alert_threshold():
        raise WalletUserError("حد استعادة السيولة يجب أن يساوي حد التنبيه أو يزيد عليه.")
    DBX.set_setting("liquidity_alert_recovery_usdt", q8(amount))


def liquidity_alert_cooldown_minutes() -> int:
    default = int(globals().get("LIQUIDITY_ALERT_COOLDOWN_MINUTES_DEFAULT", 360))
    try:
        return max(5, int(DBX.get_setting("liquidity_alert_cooldown_minutes") or default))
    except Exception:
        return default


def set_liquidity_alert_cooldown_minutes(value: Any) -> None:
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise WalletUserError("مدة تكرار تنبيه السيولة غير صالحة.") from exc
    if minutes < 5 or minutes > 10080:
        raise WalletUserError("مدة التذكير يجب أن تكون بين 5 و10080 دقيقة.")
    DBX.set_setting("liquidity_alert_cooldown_minutes", str(minutes))


def evaluate_liquidity_alert(*, force_refresh: bool = False) -> Optional[dict[str, Any]]:
    """إرجاع حدث تنبيه واحد فقط عند تغير الحالة أو انتهاء فترة التهدئة."""
    if not liquidity_alert_enabled():
        return None
    info = get_public_liquidity(force_refresh=force_refresh)
    if not info.get("available_known") or info.get("stale"):
        return None
    available = wallet_decimal(info.get("available") or 0)
    threshold = liquidity_alert_threshold()
    recovery = liquidity_alert_recovery()
    state = str(DBX.get_setting("liquidity_alert_state") or "NORMAL").upper()
    try:
        last_sent = float(DBX.get_setting("liquidity_alert_last_sent_epoch") or 0)
    except Exception:
        last_sent = 0.0
    now_epoch = time.time()
    cooldown_seconds = liquidity_alert_cooldown_minutes() * 60

    if available <= threshold:
        should_send = state != "LOW" or now_epoch - last_sent >= cooldown_seconds
        DBX.set_setting("liquidity_alert_state", "LOW")
        DBX.set_setting("liquidity_alert_last_available", q8(available))
        if not should_send:
            return None
        DBX.set_setting("liquidity_alert_last_sent_epoch", str(now_epoch))
        return {
            "kind": "LOW",
            "available": available,
            "threshold": threshold,
            "recovery": recovery,
            "reserve": wallet_decimal(info.get("reserve") or 0),
            "reminder": state == "LOW",
        }

    if state == "LOW" and available >= recovery:
        DBX.set_setting("liquidity_alert_state", "NORMAL")
        DBX.set_setting("liquidity_alert_last_available", q8(available))
        DBX.set_setting("liquidity_alert_last_sent_epoch", str(now_epoch))
        return {
            "kind": "RECOVERED",
            "available": available,
            "threshold": threshold,
            "recovery": recovery,
            "reserve": wallet_decimal(info.get("reserve") or 0),
            "reminder": False,
        }

    if state != "NORMAL":
        DBX.set_setting("liquidity_alert_state", "NORMAL")
    DBX.set_setting("liquidity_alert_last_available", q8(available))
    return None


async def monitor_liquidity_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        event = await asyncio.to_thread(evaluate_liquidity_alert, force_refresh=True)
    except Exception:
        log.exception("Liquidity alert monitor failed")
        return
    if not event:
        return
    if event["kind"] == "LOW":
        heading = "⚠️ تنبيه انخفاض سيولة السحب"
        note = "تذكير: ما زالت السيولة تحت الحد المحدد." if event.get("reminder") else "يرجى تغذية محفظة الصرف لتجنب تعطل سحوبات المستخدمين."
    else:
        heading = "✅ عادت سيولة السحب إلى المستوى الآمن"
        note = "أصبح رصيد الصرف أعلى من حد الاستعادة المحدد."
    await notify_admin(
        context,
        render_bot_text('notice.bot.services.liquidity.py:204','<b>{v0}</b>\n\nالسيولة المتاحة: <b>{v1} USDT</b>\nحد التنبيه: <b>{v2} USDT</b>\nحد الاستعادة: <b>{v3} USDT</b>\nالاحتياطي المحجوز: <b>{v4} USDT</b>\n\n{v5}',[f'{heading}',f"{html(format_display_amount(event['available']))}",f"{html(format_display_amount(event['threshold']))}",f"{html(format_display_amount(event['recovery']))}",f"{html(format_display_amount(event['reserve']))}",f'{html(note)}']),
    )


def withdrawal_operation_limit_enabled() -> bool:
    return _bool_setting(
        "withdrawal_operation_limit_enabled",
        WITHDRAWAL_OPERATION_LIMIT_ENABLED_DEFAULT,
    )


def set_withdrawal_operation_limit_enabled(value: bool) -> None:
    DBX.set_setting("withdrawal_operation_limit_enabled", "1" if value else "0")


def withdrawal_operation_limit() -> Decimal:
    raw = DBX.get_setting("withdrawal_operation_limit_usdt")
    try:
        amount = wallet_decimal(
            raw if raw is not None else WITHDRAWAL_OPERATION_LIMIT_USDT_DEFAULT
        )
    except Exception:
        amount = wallet_decimal(WITHDRAWAL_OPERATION_LIMIT_USDT_DEFAULT)
    return max(Decimal("0"), amount)


def set_withdrawal_operation_limit(value: Any) -> None:
    amount = wallet_decimal(value)
    if amount <= 0:
        raise WalletUserError("الحد الأقصى للعملية يجب أن يكون أكبر من صفر.")
    DBX.set_setting("withdrawal_operation_limit_usdt", q8(amount))


def effective_withdrawal_ceiling() -> Decimal:
    """Return the configured hard ceiling, excluding the user's own balance."""
    ceiling = wallet_decimal(WALLET_MAX_WITHDRAW_USDT)
    if withdrawal_operation_limit_enabled():
        configured = withdrawal_operation_limit()
        if configured > 0:
            ceiling = min(ceiling, configured)
    return ceiling


def validate_operation_limit(amount: Any) -> Decimal:
    parsed = wallet_decimal(amount)
    ceiling = effective_withdrawal_ceiling()
    if parsed > ceiling:
        if withdrawal_operation_limit_enabled():
            raise WalletUserError(
                f"الحد الأقصى المسموح لكل عملية هو {format_display_amount(ceiling)} USDT."
            )
        raise WalletUserError(f"الحد الأعلى للعملية هو {format_display_amount(ceiling)} USDT.")
    return parsed


def _display_quantum() -> Decimal:
    if PUBLIC_LIQUIDITY_DECIMALS <= 0:
        return Decimal("1")
    return Decimal("1").scaleb(-PUBLIC_LIQUIDITY_DECIMALS)


def get_public_liquidity(*, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch and cache live withdrawable USDT after the configured reserve.

    On a temporary provider error, the last successful cached value is returned
    as stale when available. The provider name is intentionally not returned in
    the public payload.
    """
    if not public_liquidity_enabled():
        return {
            "enabled": False,
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available": "0",
            "free": "0",
            "reserve": q8(public_liquidity_reserve()),
            "available_display": "0",
            "stale": False,
            "available_known": False,
            "error": "",
            "fetched_at": 0,
        }

    now = time.time()
    shared_cache = DBX.get_provider_cache(f"binance:liquidity:{BINANCE_COIN.upper()}")
    if shared_cache:
        shared_payload = shared_cache.get("payload") or {}
        shared_at = float(shared_cache.get("fetched_at_epoch") or 0)
        with _cache_lock:
            if shared_at > float(_liquidity_cache.get("fetched_at") or 0):
                _liquidity_cache.update(
                    fetched_at=shared_at,
                    free=wallet_decimal(shared_payload.get("free") or 0),
                    available=wallet_decimal(shared_payload.get("available") or 0),
                    error="",
                )
    with _cache_lock:
        cached_at = float(_liquidity_cache.get("fetched_at") or 0)
        if (
            not force_refresh
            and cached_at > 0
            and now - cached_at <= PUBLIC_LIQUIDITY_CACHE_SECONDS
        ):
            return {
                "enabled": public_liquidity_enabled(),
                "label": PUBLIC_LIQUIDITY_LABEL,
                "available": q8(_liquidity_cache["available"]),
                "free": q8(_liquidity_cache["free"]),
                "reserve": q8(public_liquidity_reserve()),
                "available_display": format(
                    _liquidity_cache["available"].quantize(_display_quantum(), rounding=ROUND_DOWN),
                    "f",
                ),
                "stale": False,
                "available_known": True,
                "error": "",
                "fetched_at": cached_at,
            }

    if not BINANCE.enabled():
        return {
            "enabled": public_liquidity_enabled(),
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available": "0",
            "free": "0",
            "reserve": q8(public_liquidity_reserve()),
            "available_display": "0",
            "stale": False,
            "available_known": False,
            "error": "محفظة الصرف غير مهيأة",
            "fetched_at": 0,
        }

    try:
        free = wallet_decimal(BINANCE.get_spot_free_balance(BINANCE_COIN))
        reserve = public_liquidity_reserve()
        available = max(Decimal("0"), free - reserve)
        with _cache_lock:
            _liquidity_cache.update(
                fetched_at=now,
                free=free,
                available=available,
                error="",
            )
        DBX.set_provider_cache(
            f"binance:liquidity:{BINANCE_COIN.upper()}",
            {"free": q8(free), "available": q8(available)},
            now,
        )
        return {
            "enabled": public_liquidity_enabled(),
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available": q8(available),
            "free": q8(free),
            "reserve": q8(reserve),
            "available_display": format(
                available.quantize(_display_quantum(), rounding=ROUND_DOWN), "f"
            ),
            "stale": False,
            "available_known": True,
            "error": "",
            "fetched_at": now,
        }
    except Exception as exc:
        with _cache_lock:
            cached_at = float(_liquidity_cache.get("fetched_at") or 0)
            cached_available = wallet_decimal(_liquidity_cache.get("available") or 0)
            cached_free = wallet_decimal(_liquidity_cache.get("free") or 0)
            _liquidity_cache["error"] = str(exc)
        if cached_at > 0:
            return {
                "enabled": public_liquidity_enabled(),
                "label": PUBLIC_LIQUIDITY_LABEL,
                "available": q8(cached_available),
                "free": q8(cached_free),
                "reserve": q8(public_liquidity_reserve()),
                "available_display": format(
                    cached_available.quantize(_display_quantum(), rounding=ROUND_DOWN),
                    "f",
                ),
                "stale": True,
                "available_known": True,
                "error": "تعذر تحديث السيولة مؤقتًا",
                "fetched_at": cached_at,
            }
        return {
            "enabled": public_liquidity_enabled(),
            "label": PUBLIC_LIQUIDITY_LABEL,
            "available": "0",
            "free": "0",
            "reserve": q8(public_liquidity_reserve()),
            "available_display": "0",
            "stale": False,
            "available_known": False,
            "error": "تعذر قراءة السيولة مؤقتًا",
            "fetched_at": 0,
        }


def validate_live_liquidity(amount: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    parsed = wallet_decimal(amount)
    info = get_public_liquidity(force_refresh=force_refresh)
    if force_refresh and (not info.get("available_known") or info.get("stale")):
        raise WalletTechnicalError("تعذر التحقق المباشر من سيولة مزود السحب؛ لم يتم حجز الرصيد.")
    if info["available_known"] and parsed > wallet_decimal(info["available"]):
        raise WalletUserError(
            f"المبلغ يتجاوز {PUBLIC_LIQUIDITY_LABEL}: {info['available_display']} USDT."
        )
    return info


for _key, _value in {
    "public_liquidity_enabled": "1" if PUBLIC_LIQUIDITY_ENABLED_DEFAULT else "0",
    "public_liquidity_reserve_usdt": q8(PUBLIC_LIQUIDITY_RESERVE_USDT_DEFAULT),
    "withdrawal_operation_limit_enabled": "1" if WITHDRAWAL_OPERATION_LIMIT_ENABLED_DEFAULT else "0",
    "withdrawal_operation_limit_usdt": q8(WITHDRAWAL_OPERATION_LIMIT_USDT_DEFAULT),
    "liquidity_alert_enabled": "1" if globals().get("LIQUIDITY_ALERT_ENABLED_DEFAULT", True) else "0",
    "liquidity_alert_threshold_usdt": q8(globals().get("LIQUIDITY_ALERT_THRESHOLD_USDT_DEFAULT", Decimal("20"))),
    "liquidity_alert_recovery_usdt": q8(globals().get("LIQUIDITY_ALERT_RECOVERY_USDT_DEFAULT", Decimal("25"))),
    "liquidity_alert_cooldown_minutes": str(int(globals().get("LIQUIDITY_ALERT_COOLDOWN_MINUTES_DEFAULT", 360))),
    "liquidity_alert_state": "NORMAL",
}.items():
    if DBX.get_setting(_key) is None:
        DBX.set_setting(_key, _value)


_export_globals(globals())

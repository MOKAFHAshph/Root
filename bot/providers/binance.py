"""عميل Binance الوحيد للسحب مع فحص فوري للمفتاح وعنوان الخروج."""
from __future__ import annotations

import os
import socket

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals

_seed_globals(globals())


class BinanceAPIError(RuntimeError):
    """خطأ API يحتفظ بالكود والمسار لتصنيف الخطأ بدقة."""

    def __init__(self, message: str, *, code: Any = None, http_status: int = 0, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.http_status = int(http_status or 0)
        self.path = str(path or "")

    @property
    def permission_error(self) -> bool:
        text = str(self).lower()
        return self.code == -2015 or "invalid api-key, ip, or permissions" in text

    @property
    def service_error(self) -> bool:
        text = str(self).lower()
        if self.permission_error:
            return True
        if self.http_status >= 500:
            return True
        if self.code in {-1000, -1001, -1003, -1006, -1007, -1015, -1021, -1022}:
            return True
        return any(
            token in text
            for token in (
                "internal error",
                "service unavailable",
                "system busy",
                "server busy",
                "timed out",
                "timeout",
                "disconnected",
            )
        )


class BinanceTransportError(RuntimeError):
    """تعذر الاتصال بخدمة Binance."""


class BinanceServiceUnavailableError(RuntimeError):
    """حالة عامة توقف السحب لكل المستخدمين حتى ينجح الفحص التالي."""


class BinanceIndividualError(RuntimeError):
    """خطأ يخص طلب مستخدم واحد ولا يوقف السحب لبقية المستخدمين."""


def is_binance_service_error(error: BaseException) -> bool:
    if isinstance(error, (BinanceTransportError, BinanceServiceUnavailableError)):
        return True
    if isinstance(error, BinanceAPIError):
        return error.service_error
    text = str(error).lower()
    return any(
        token in text
        for token in (
            "صلاحيات binance",
            "مفتاح binance",
            "اتصال binance",
            "خدمة binance",
            "الشبكة مشغولة حاليًا",
            "السحب متوقف على هذه الشبكة",
            "السحب غير متاح حاليًا",
            "رصيد binance غير كاف",
        )
    )


def is_binance_individual_error(error: BaseException) -> bool:
    if isinstance(error, BinanceIndividualError):
        return True
    if isinstance(error, BinanceAPIError) and not error.service_error:
        text = str(error).lower()
        return any(
            token in text
            for token in (
                "address",
                "withdraw min",
                "minimum",
                "memo",
                "tag",
                "invalid parameter",
                "illegal characters",
            )
        )
    return False


def binance_key_fingerprint() -> str:
    value = str(BINANCE_API_KEY or "")
    if not value:
        return "UNSET"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()


_EGRESS_CACHE: dict[str, Any] = {"ip": "", "fetched_at": 0.0}


def binance_egress_ip(*, force_refresh: bool = False) -> str:
    """قراءة IPv4 العام الذي تخرج منه الخدمة باتجاه Binance."""
    if not BINANCE_EGRESS_IP_CHECK_ENABLED:
        return "DISABLED"
    now = time.time()
    if (
        not force_refresh
        and _EGRESS_CACHE.get("ip")
        and now - float(_EGRESS_CACHE.get("fetched_at") or 0) <= BINANCE_EGRESS_IP_CACHE_SECONDS
    ):
        return str(_EGRESS_CACHE["ip"])
    cached = DBX.get_setting("binance_last_egress_ip") or ""
    try:
        cached_epoch = float(DBX.get_setting("binance_last_egress_ip_epoch") or 0)
    except Exception:
        cached_epoch = 0.0
    if not force_refresh and cached and now - cached_epoch <= BINANCE_EGRESS_IP_CACHE_SECONDS:
        _EGRESS_CACHE.update(ip=cached, fetched_at=cached_epoch)
        return cached

    for url in (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ifconfig.me/ip",
    ):
        try:
            response = requests.get(url, timeout=BINANCE_EGRESS_IP_TIMEOUT_SECONDS)
            response.raise_for_status()
            value = str(response.text or "").strip()
            if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value):
                _EGRESS_CACHE.update(ip=value, fetched_at=now)
                DBX.set_setting("binance_last_egress_ip", value)
                DBX.set_setting("binance_last_egress_ip_epoch", str(now))
                return value
        except Exception:
            continue
    return cached or "UNKNOWN"


def binance_auth_state() -> dict[str, Any]:
    available = (DBX.get_setting("binance_withdraw_available") or "0") == "1"
    return {
        "blocked": not available,
        "available": available,
        "last_error": DBX.get_setting("binance_auth_last_error") or "",
        "last_error_code": DBX.get_setting("binance_auth_last_error_code") or "",
        "last_check_at": DBX.get_setting("binance_auth_last_check_at") or "",
        "last_success_at": DBX.get_setting("binance_auth_last_success_at") or "",
        "egress_ip": DBX.get_setting("binance_auth_egress_ip") or DBX.get_setting("binance_last_egress_ip") or "",
        "key_fingerprint": DBX.get_setting("binance_auth_key_fingerprint") or binance_key_fingerprint(),
    }


def mark_binance_unavailable(error: BaseException, *, code: Any = None, refresh_ip: bool = False) -> dict[str, Any]:
    """إيقاف السحب فقط، بدون مهلة زمنية ثابتة."""
    previous = binance_auth_state()
    ip = binance_egress_ip(force_refresh=refresh_ip)
    error_code = code if code not in (None, "") else getattr(error, "code", "")
    DBX.set_setting("binance_withdraw_available", "0")
    DBX.set_setting("binance_auth_last_error", str(error))
    DBX.set_setting("binance_auth_last_error_code", str(error_code or ""))
    DBX.set_setting("binance_auth_egress_ip", ip)
    DBX.set_setting("binance_auth_key_fingerprint", binance_key_fingerprint())
    DBX.set_setting("binance_auth_last_check_at", now_str())
    DBX.set_setting("binance_auth_last_failure_epoch", str(time.time()))
    if previous["available"]:
        DBX.set_setting("binance_failure_alert_pending", "1")
    state = binance_auth_state()
    state["became_unavailable"] = bool(previous["available"])
    return state


def mark_binance_auth_failure(error: BinanceAPIError) -> dict[str, Any]:
    """اسم توافق قديم؛ لم يعد يطبّق فترة عشر دقائق."""
    return mark_binance_unavailable(error, code=error.code)


def clear_binance_auth_failure() -> dict[str, Any]:
    previous = binance_auth_state()
    DBX.set_setting("binance_withdraw_available", "1")
    DBX.set_setting("binance_auth_last_error", "")
    DBX.set_setting("binance_auth_last_error_code", "")
    DBX.set_setting("binance_auth_last_check_at", now_str())
    DBX.set_setting("binance_auth_last_success_at", now_str())
    DBX.set_setting("binance_auth_key_fingerprint", binance_key_fingerprint())
    DBX.set_setting("binance_failure_alert_pending", "0")
    state = binance_auth_state()
    state["recovered"] = bool(not previous["available"])
    return state


def binance_claim_failure_notification() -> bool:
    """حجز إشعار الفشل فورًا مرة واحدة عند انتقال الخدمة إلى التوقف."""
    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT value FROM settings_v2 WHERE key='binance_failure_alert_pending'"
        ).fetchone()
        if not row or str(row["value"] or "0") != "1":
            con.commit()
            return False
        con.execute(
            "UPDATE settings_v2 SET value='0', updated_at=? WHERE key='binance_failure_alert_pending'",
            (now_str(),),
        )
        con.commit()
        return True


def binance_require_available() -> None:
    state = binance_auth_state()
    if state["available"]:
        return
    reason = state["last_error"] or "لم ينجح فحص اتصال Binance بعد."
    raise BinanceServiceUnavailableError(
        f"خدمة السحب متوقفة مؤقتًا لحين عودة اتصال Binance. السبب: {reason}"
    )


def record_runtime_identity(service_name: str, *, refresh_ip: bool = False, stopped: bool = False) -> dict[str, Any]:
    service = str(service_name or os.getenv("JAIB_SERVICE_NAME") or "UNKNOWN").upper()
    pid = os.getpid()
    instance_id = f"{service}:{socket.gethostname()}:{pid}"
    ip = binance_egress_ip(force_refresh=refresh_ip)
    parent_pid = int(os.getenv("JAIB_PARENT_PID") or os.getppid() or 0)
    DBX.upsert_runtime_instance(
        instance_id=instance_id,
        service_name=service,
        pid=pid,
        hostname=socket.gethostname(),
        cwd=str(Path.cwd().resolve()),
        env_path=str((BASE_DIR / ".env").resolve()),
        db_path=str(DBX.path),
        provider_key_fingerprint=binance_key_fingerprint(),
        egress_ip=ip,
        parent_pid=parent_pid,
        stopped=stopped,
    )
    return {
        "instance_id": instance_id,
        "service": service,
        "pid": pid,
        "parent_pid": parent_pid,
        "cwd": str(Path.cwd().resolve()),
        "env_path": str((BASE_DIR / ".env").resolve()),
        "db_path": str(DBX.path),
        "key_fingerprint": binance_key_fingerprint(),
        "egress_ip": ip,
    }


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BINANCE_API_BASE):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.time_offset_ms = 0
        self._config_cache: dict[str, Any] = {}
        self._config_cache_at = 0.0

    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def sync_time(self) -> int:
        try:
            response = self.session.get(f"{self.base_url}/api/v3/time", timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            error = BinanceTransportError(f"تعذر مزامنة وقت Binance: {type(exc).__name__}")
            mark_binance_unavailable(error)
            raise error from exc
        server_time = int(response.json()["serverTime"])
        self.time_offset_ms = server_time - int(time.time() * 1000)
        return self.time_offset_ms

    def _timestamp(self) -> int:
        if self.time_offset_ms == 0:
            self.sync_time()
        return int(time.time() * 1000) + self.time_offset_ms

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in dict(params).items() if v is not None and v != "" and k != "signature"}
        query = urlencode(clean)
        clean["signature"] = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
        return clean

    def _unknown_submit_error(self, message: str, exc: Exception | None = None) -> Exception:
        error_class = globals().get("WalletUnknownResultError")
        if error_class is not None:
            return error_class(message)
        return BinanceTransportError(message)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        retry_on_time: bool = True,
    ) -> Any:
        method_upper = method.upper()
        payload = dict(params if params is not None else data if data is not None else {})
        payload["timestamp"] = self._timestamp()
        payload.setdefault("recvWindow", BINANCE_RECV_WINDOW)
        signed = self._sign(payload)
        is_withdraw_submit = method_upper == "POST" and path == "/sapi/v1/capital/withdraw/apply"
        try:
            response = self.session.request(
                method_upper,
                f"{self.base_url}{path}",
                params=signed if params is not None else None,
                data=signed if data is not None or params is None else None,
                timeout=30,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            error = BinanceTransportError(f"انقطع الاتصال مع Binance أثناء {path}: {type(exc).__name__}")
            mark_binance_unavailable(error)
            if is_withdraw_submit:
                raise self._unknown_submit_error(str(error), exc) from exc
            raise error from exc
        except requests.RequestException as exc:
            error = BinanceTransportError(f"فشل اتصال Binance أثناء {path}: {type(exc).__name__}")
            mark_binance_unavailable(error)
            if is_withdraw_submit:
                raise self._unknown_submit_error(str(error), exc) from exc
            raise error from exc

        try:
            body = response.json()
        except Exception as exc:
            error = BinanceAPIError(
                f"Binance أعاد استجابة غير مفهومة HTTP {response.status_code} أثناء {path}",
                http_status=response.status_code,
                path=path,
            )
            if error.service_error:
                mark_binance_unavailable(error)
            if is_withdraw_submit and response.status_code >= 500:
                raise self._unknown_submit_error(str(error), exc) from exc
            raise error from exc

        if response.status_code == 200:
            return body

        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg") if isinstance(body, dict) else str(body)
        if retry_on_time and code == -1021:
            self.sync_time()
            return self._request(method, path, params=params, data=data, retry_on_time=False)

        error = BinanceAPIError(msg or str(body), code=code, http_status=response.status_code, path=path)
        if error.service_error:
            mark_binance_unavailable(error, code=error.code)
        if is_withdraw_submit and response.status_code >= 500:
            raise self._unknown_submit_error(f"نتيجة إرسال السحب غير مؤكدة: {error}", error) from error
        raise error

    def get_api_key_permissions(self) -> dict[str, Any]:
        data = self._request("GET", "/sapi/v1/account/apiRestrictions", params={})
        return data if isinstance(data, dict) else {}

    def _load_coin_config(self, coin: str) -> dict[str, Any]:
        coin_key = str(coin or "").upper()
        data = self._request("GET", "/sapi/v1/capital/config/getall", params={})
        cache: dict[str, Any] = {}
        now = time.time()
        if isinstance(data, list):
            for item in data:
                key = str(item.get("coin", "")).upper()
                if key:
                    cache[key] = item
                    DBX.set_provider_cache(f"binance:coin-config:{key}", item, now)
        self._config_cache = cache
        self._config_cache_at = now
        return cache.get(coin_key, {})

    def get_coin_config(self, coin: str, force_refresh: bool = False) -> dict[str, Any]:
        coin_key = str(coin or "").upper()
        now = time.time()
        if not force_refresh and self._config_cache and now - self._config_cache_at <= BINANCE_SHARED_CONFIG_CACHE_SECONDS:
            return self._config_cache.get(coin_key, {})

        shared_key = f"binance:coin-config:{coin_key}"
        shared = DBX.get_provider_cache(shared_key)
        if (
            not force_refresh
            and shared
            and now - float(shared.get("fetched_at_epoch") or 0) <= BINANCE_SHARED_CONFIG_CACHE_SECONDS
        ):
            info = shared.get("payload") or {}
            self._config_cache[coin_key] = info
            self._config_cache_at = float(shared.get("fetched_at_epoch") or now)
            return info

        binance_require_available()
        return self._load_coin_config(coin_key)

    def get_network_config(self, coin: str, network: str, force_refresh: bool = False) -> dict[str, Any]:
        info = self.get_coin_config(coin, force_refresh=force_refresh)
        for item in info.get("networkList", []) or []:
            if str(item.get("network", "")).upper() == network.upper():
                return item
        return {}

    def get_spot_free_balance(self, asset: str) -> Decimal:
        binance_require_available()
        data = self._request("POST", "/sapi/v3/asset/getUserAsset", data={})
        if isinstance(data, list):
            for item in data:
                if str(item.get("asset", "")).upper() == asset.upper():
                    return decimal_or_zero(item.get("free"))
        return Decimal("0")

    def get_withdraw_quote(self, coin: str, network: str, amount: Decimal) -> dict[str, Any]:
        binance_require_available()
        coin_info = self.get_coin_config(coin)
        if not coin_info:
            error = BinanceServiceUnavailableError(f"عملة {coin} غير موجودة في إعدادات Binance")
            mark_binance_unavailable(error)
            raise error
        if not coin_info.get("withdrawAllEnable", False):
            error = BinanceServiceUnavailableError(f"السحب غير متاح حاليًا لعملة {coin}")
            mark_binance_unavailable(error)
            raise error
        net = self.get_network_config(coin, network)
        if not net:
            error = BinanceServiceUnavailableError(f"الشبكة {network} غير متاحة لعملة {coin}")
            mark_binance_unavailable(error)
            raise error
        if not net.get("withdrawEnable", False):
            error = BinanceServiceUnavailableError(net.get("withdrawDesc") or "السحب متوقف على هذه الشبكة")
            mark_binance_unavailable(error)
            raise error
        if net.get("busy", False):
            error = BinanceServiceUnavailableError("الشبكة مشغولة حاليًا")
            mark_binance_unavailable(error)
            raise error
        min_amount = decimal_or_zero(net.get("withdrawMin"))
        fee = decimal_or_zero(net.get("withdrawFee"))
        return {
            "coin_info": coin_info,
            "net": net,
            "min_amount": min_amount,
            "fee": fee,
            "total_required": amount + fee,
        }

    def validate_withdraw_request(self, *, coin: str, network: str, address: str, amount: Decimal) -> dict[str, Any]:
        info = self.get_withdraw_quote(coin, network, amount)
        if amount < info["min_amount"]:
            raise BinanceIndividualError(
                f"الحد الأدنى للسحب على {network} هو {format_display_amount(info['min_amount'])} {coin}"
            )
        if not validate_address_for_network(network, address):
            raise BinanceIndividualError(f"العنوان لا يبدو صالحًا لشبكة {network}")
        if info["net"].get("withdrawTag"):
            raise BinanceIndividualError(f"شبكة {network} تحتاج MEMO/TAG وهذه النسخة لا تدعمه")
        free = self.get_spot_free_balance(coin)
        if free < info["total_required"]:
            error = BinanceServiceUnavailableError(
                f"رصيد Binance غير كافٍ. المتاح {format_display_amount(free)} {coin}، "
                f"المطلوب {format_display_amount(info['total_required'])} {coin}."
            )
            mark_binance_unavailable(error)
            raise error
        return info

    def withdraw(
        self,
        *,
        coin: str,
        network: str,
        address: str,
        amount: Decimal,
        withdraw_order_id: str,
        wallet_type: int = BINANCE_WALLET_TYPE,
        validate: bool = True,
    ) -> dict[str, Any]:
        if validate:
            self.validate_withdraw_request(coin=coin, network=network, address=address, amount=amount)
        response = self._request(
            "POST",
            "/sapi/v1/capital/withdraw/apply",
            data={
                "coin": coin,
                "network": network,
                "address": address,
                "amount": q8(amount),
                "walletType": wallet_type,
                "withdrawOrderId": withdraw_order_id,
            },
        )
        return response if isinstance(response, dict) else {}

    def withdraw_history(self, *, withdraw_order_id: str, coin: Optional[str] = None) -> list[dict[str, Any]]:
        binance_require_available()
        data = self._request(
            "GET",
            "/sapi/v1/capital/withdraw/history",
            params={"withdrawOrderId": withdraw_order_id, "coin": coin or BINANCE_COIN},
        )
        return data if isinstance(data, list) else []


BINANCE = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)


def binance_health_check(*, force_ip: bool = True) -> dict[str, Any]:
    """فحص المفتاح، الـIP، صلاحية القراءة والسحب، وجاهزية شبكة BSC."""
    ip = binance_egress_ip(force_refresh=force_ip)
    try:
        if not BINANCE.enabled():
            raise BinanceServiceUnavailableError("مفتاح Binance API أو المفتاح السري غير مضبوط.")
        if not BINANCE_WITHDRAW_ENABLED:
            raise BinanceServiceUnavailableError("BINANCE_WITHDRAW_ENABLED غير مفعّل.")
        permissions = BINANCE.get_api_key_permissions()
        if not bool(permissions.get("enableReading")):
            raise BinanceServiceUnavailableError("صلاحية القراءة غير مفعّلة في مفتاح Binance API.")
        if not bool(permissions.get("enableWithdrawals")):
            raise BinanceServiceUnavailableError("صلاحية السحب غير مفعّلة في مفتاح Binance API.")
        coin_info = BINANCE._load_coin_config(BINANCE_COIN)
        if not coin_info or not coin_info.get("withdrawAllEnable", False):
            raise BinanceServiceUnavailableError(f"السحب غير متاح حاليًا لعملة {BINANCE_COIN}.")
        network = {}
        for item in coin_info.get("networkList", []) or []:
            if str(item.get("network", "")).upper() == str(BEP20_NETWORK_CODE).upper():
                network = item
                break
        if not network:
            raise BinanceServiceUnavailableError(f"شبكة {BEP20_NETWORK_CODE} غير موجودة في Binance.")
        if not network.get("withdrawEnable", False):
            raise BinanceServiceUnavailableError(network.get("withdrawDesc") or "السحب متوقف على شبكة BSC.")
        if network.get("busy", False):
            raise BinanceServiceUnavailableError("شبكة BSC مشغولة حاليًا.")
        state = clear_binance_auth_failure()
        state.update(ok=True, reason="", permissions=permissions, egress_ip=ip)
        return state
    except Exception as exc:
        state = mark_binance_unavailable(exc, code=getattr(exc, "code", ""), refresh_ip=False)
        state.update(ok=False, reason=str(exc), egress_ip=ip)
        return state


_export_globals(globals())

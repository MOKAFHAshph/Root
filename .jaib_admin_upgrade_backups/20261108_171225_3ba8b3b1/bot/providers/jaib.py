"""Jaib wallet client and payment-query integration.

"""
from __future__ import annotations

import asyncio
import threading
from urllib.parse import urlsplit

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ============================================================
# Jaib
# ============================================================


def _pad(data: bytes) -> bytes:
    pad_len = 16 - len(data) % 16
    return data + bytes([pad_len]) * pad_len


def _unpad(data: bytes) -> bytes:
    return data[:-data[-1]]


def encrypt_payload(data: dict[str, Any], key: bytes, iv: bytes) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    zipped = gzip.compress(raw, 6)
    base = base64.b64encode(zipped)
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(_pad(base))
    return base64.b64encode(enc).decode()


def decrypt_payload(payload: str, key: bytes, iv: bytes) -> dict[str, Any]:
    enc = base64.b64decode(payload)
    data = AES.new(key, AES.MODE_CBC, iv).decrypt(enc)
    data = _unpad(data)
    zipped = base64.b64decode(data.decode())
    raw = gzip.decompress(zipped)
    return json.loads(raw.decode("utf-8-sig").strip())


@dataclass
class JaibTx:
    amount: Decimal
    currency: str
    reference_id: str
    sender_name: Optional[str]
    sender_phone: Optional[str]
    tx_date: Optional[str]
    tx_type: Optional[str]
    note: Optional[str]
    raw: dict[str, Any]


class JaibClient:
    def __init__(self):
        self.diagnostics=[]
        self.request_timeout=JAIB_REQUEST_TIMEOUT_SECONDS
        self.init_timeout=JAIB_INIT_TIMEOUT_SECONDS
        self.session = requests.Session()
        jaib_proxy = os.getenv("JAIB_SOCKS_PROXY", "").strip()
        if jaib_proxy:
            self.session.proxies.update({
                "http": jaib_proxy,
                "https": jaib_proxy,
            })
            self.session.trust_env = False

        # Jaib's older API uses a TLS configuration that differs from the other
        # providers. Only this session may opt out, to reproduce jaib_bot_test.
        # Prefer JAIB_TLS_VERIFY=true and a trusted JAIB_CA_BUNDLE in production.
        tls_verify = os.getenv('JAIB_TLS_VERIFY', 'true').strip().lower()
        self.session.verify = (os.environ.get('JAIB_CA_BUNDLE') or True) if tls_verify not in ('false', '0', 'no') else False
        self.session.headers.update(
            {
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "okhttp/5.1.0",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Authorization": JAIB_AUTH_HEADER,
            }
        )
        self.client_key: Optional[bytes] = None
        self.session_key: Optional[bytes] = None
        self.session_iv: Optional[bytes] = None
        self.access_token: Optional[str] = None
        self.servers: list[str] = []

    def record(self,stage,code,server='',http_status=None,endpoint=''):
        try:
            url=urlsplit(server);host=url.hostname or ''
            if url.port:host+=':'+str(url.port)
        except Exception:host=''
        self.diagnostics.append({'stage':stage,'code':code,'host':host,'http_status':http_status,'endpoint':endpoint,'at':now_str()})
        # Log metadata only; HTTP request bodies, credentials and responses may
        # contain tokens or private wallet information and must never be logged.
        log.info('Jaib transport stage=%s code=%s host=%s endpoint=%s http_status=%s', stage, code, host, endpoint, http_status)
        self.diagnostics=self.diagnostics[-30:]

    def request_failure(self,stage,server,exc,endpoint=''):
        code='TLS_CERTIFICATE' if isinstance(exc,requests.exceptions.SSLError) else ('TIMEOUT' if isinstance(exc,requests.exceptions.Timeout) else ('CONNECTION' if isinstance(exc,requests.exceptions.ConnectionError) else type(exc).__name__))
        self.record(stage,code,server,endpoint=endpoint)

    def _login_transport_failure(self) -> tuple[str, str]:
        """Summarize *observed* HTTP metadata; never infer an SMS challenge."""
        events = [event for event in self.diagnostics if event.get('stage') == 'LOGIN_HTTP']
        statuses = [int(event['http_status']) for event in events if event.get('http_status') is not None]
        codes = {str(event.get('code') or '') for event in events}
        if statuses and all(status in (401, 403) for status in statuses) and not (codes & {'TIMEOUT', 'CONNECTION', 'TLS_CERTIFICATE'}):
            return 'AUTH_HTTP_REJECTED', 'رفضت خوادم جيب تسجيل الدخول (HTTP 401/403). لا يمكن تحديد الحاجة لرمز SMS من هذا الرد.'
        if 429 in statuses:
            return 'RATE_LIMIT', 'تقييد مؤقت للطلبات من خوادم جيب (HTTP 429).'
        if any(status in (401, 403) for status in statuses) and (any(status >= 500 for status in statuses) or codes & {'TIMEOUT', 'CONNECTION'}):
            return 'MIXED_LOGIN_FAILURE', 'ردود متباينة من خوادم جيب (رفض مصادقة/عطل أو اتصال). السبب غير محسوم.'
        if any(status >= 500 for status in statuses):
            return 'SERVER_HTTP_FAILURE', 'فشل خادم جيب أثناء تسجيل الدخول (HTTP 5xx).'
        if 'TLS_CERTIFICATE' in codes:
            return 'TLS_CERTIFICATE', 'فشل التحقق من شهادة الاتصال بجيب.'
        if 'TIMEOUT' in codes or 'CONNECTION' in codes:
            return 'NETWORK_FAILURE', 'تعذر الوصول إلى خوادم جيب أو انتهت مهلة الاتصال.'
        if statuses:
            return 'HTTP_FAILURE', 'فشل تسجيل الدخول مع رد HTTP غير ناجح من خوادم جيب.'
        return 'NO_RESPONSE', 'لم يصل رد تسجيل دخول قابل للاستخدام من جيب.'

    @staticmethod
    def enabled() -> bool:
        required_values = [
            JAIB_PHONE, JAIB_PASSWORD, JAIB_FCM_KEY_B64, JAIB_FCM_IV_B64, JAIB_CLIENT_IV_B64,
            JAIB_DEVICE_ID, JAIB_SMS_CODE, JAIB_TKN_NOT, JAIB_INIT_VALUE, JAIB_AUTH_HEADER,
        ]
        return all(bool(x) for x in required_values) and all(os.getenv(k) for k in ("JAIB_CLIENT_KEY","JAIB_CLIENT_SECRET","JAIB_RETURN_KEY"))

    def _request(self, path: str, payload: str, headers: Optional[dict[str, str]] = None, servers: Optional[list[str]] = None):
        targets = servers or self.servers or JAIB_BOOTSTRAP_SERVERS
        for base in targets:
            host = base.replace("https://", "")
            hdrs = dict(headers or {})
            hdrs["Host"] = host
            try:
                response = self.session.post(
                    base + path,
                    json={"value": payload},
                    headers=hdrs,
                    timeout=self.request_timeout,
                )
                stage='LOGIN_HTTP' if 'LogE2' in path else 'QUERY_HTTP'
                self.record(stage,'HTTP_OK' if response.status_code==200 else 'HTTP_ERROR',base,response.status_code,endpoint=path)
                if response.status_code == 200:
                    return response.json()
            except Exception as exc:
                self.request_failure('LOGIN_HTTP' if 'LogE2' in path else 'QUERY_HTTP',base,exc,endpoint=path)
                continue
        return None

    def init(self) -> bool:
        fcm_key = safe_b64decode(JAIB_FCM_KEY_B64)
        fcm_iv = safe_b64decode(JAIB_FCM_IV_B64)
        data = {"DtTime": int(time.time() * 1000), "envir": 1, "value": JAIB_INIT_VALUE, "VersNum": 446}
        payload = encrypt_payload(data, fcm_key, fcm_iv)
        for base in JAIB_BOOTSTRAP_SERVERS:
            host = base.replace("https://", "")
            try:
                response = self.session.post(
                    base + "/api/HzHelp/GetInitWallet",
                    json={"value": payload},
                    headers={"Host": host},
                    timeout=self.init_timeout,
                )
                self.record('BOOTSTRAP_HTTP','HTTP_OK' if response.status_code==200 else 'HTTP_ERROR',base,response.status_code,endpoint='/api/HzHelp/GetInitWallet')
                if response.status_code != 200:
                    continue
                body = response.json()
                if not body.get("success") or not body.get("result"):
                    self.record('BOOTSTRAP_API','API_REJECTED',base)
                    continue
                dec = decrypt_payload(body["result"], fcm_key, fcm_iv)
                self.client_key = safe_b64decode(dec["Kl"])
                self.servers = [url for url in dict.fromkeys(dec.get("lsIP", [])) if urlsplit(url).scheme=='https' and urlsplit(url).hostname and not urlsplit(url).username and not urlsplit(url).password]
                for extra in JAIB_BOOTSTRAP_SERVERS:
                    if extra not in self.servers:
                        self.servers.append(extra)
                self.record('BOOTSTRAP_API','READY',base)
                return True
            except Exception as exc:
                self.request_failure('BOOTSTRAP_API',base,exc,endpoint='/api/HzHelp/GetInitWallet')
                continue
        return False

    def login(self) -> tuple[bool, str]:
        if not self.client_key:
            return False, "client_key غير موجود"
        client_iv = safe_b64decode(JAIB_CLIENT_IV_B64)
        data = {
            "clientInfo": {"AppVersion": os.getenv("JAIB_APP_VERSION", "446"), "clientKey": os.getenv("JAIB_CLIENT_KEY", ""), "clientSecret": os.getenv("JAIB_CLIENT_SECRET", ""), "returnKey": os.getenv("JAIB_RETURN_KEY", "")},
            "deviceInfo": {
                "ConfirmCode": "",
                "deviceId": JAIB_DEVICE_ID,
                "deviceInfo": "samsung SM-A716U",
                "tknNot": JAIB_TKN_NOT,
                "langId": 1,
                "otherInfo": json.dumps({"brand": "samsung", "deviceName": "samsung", "ip": "", "loginMethodType": 2, "model": "SM-A716U", "os": "33"}),
                "sourceType": 1,
            },
            "password": JAIB_PASSWORD,
            "smsCode": JAIB_SMS_CODE,
            "UpdateData": "20220329171",
            "userName": JAIB_PHONE,
        }
        payload = encrypt_payload(data, self.client_key, client_iv)
        response = self._request("/api/TokenAuth/LogE2", payload)
        if not isinstance(response, dict):
            failure_code, failure_text = self._login_transport_failure()
            self.record('LOGIN_API', failure_code)
            return False, failure_text
        if not response.get("success"):
            self.record('LOGIN_API', 'API_REJECTED')
            error = response.get("error")
            err = error.get("message") if isinstance(error, dict) else None
            return False, _jaib_safe_reason(err) if err else "رفضت واجهة جيب تسجيل الدخول. لم يُثبت سبب الرفض أو طلب SMS."
        raw = response.get("result", "")
        if isinstance(raw, dict):
            raw = raw.get("value", "")
        if not raw:
            self.record('LOGIN_API','EMPTY_RESPONSE')
            return False, "رد فارغ من Jaib"
        try:
            dec = decrypt_payload(raw, self.client_key, client_iv)
        except Exception as exc:
            self.record('LOGIN_API','DECRYPTION')
            log.warning("Failed to decrypt Jaib login response error=%s",type(exc).__name__)
            return False, f"فشل فك رد Jaib: {exc}"
        self.access_token = dec.get("accessToken")
        if not self.access_token:
            self.record('SESSION','TOKEN_MISSING')
            return False,'رد تسجيل الدخول لا يحتوي جلسة صالحة' 
        self.session_key = safe_b64decode(dec["k"])
        self.session_iv = safe_b64decode(dec["mobileScon"])
        self.record('SESSION','READY')
        return True, "ok"

    def _execute(self, body: dict[str, Any]) -> tuple[bool, Any]:
        if not self.session_key or not self.session_iv:
            return False, "لم يتم تسجيل الدخول"
        payload = encrypt_payload(body, self.session_key, self.session_iv)
        headers = {"auth": self.access_token} if self.access_token else {}
        response = self._request("/api/v1/Wallet/ExecuteE2", payload, headers)
        if not response:
            self.record('QUERY_API','NO_RESPONSE')
            return False, "فشل الاتصال بخوادم Jaib"
        if not response.get("success"):
            self.record('QUERY_API','API_REJECTED')
            err = response.get("error", {})
            return False, err.get("message", "خطأ غير معروف")
        raw = response.get("result", "")
        if isinstance(raw, dict):
            raw = raw.get("value", "")
        if not raw:
            return False, "رد فارغ"
        try:
            dec = decrypt_payload(raw, self.session_key, self.session_iv)
        except Exception as exc:
            log.exception("Failed to decrypt Jaib operation response")
            return False, f"فشل فك التشفير: {exc}"
        self.record('QUERY_API','READY')
        return True, dec

    def query_ref(self, ref_id: str) -> tuple[bool, Any]:
        ok, dec = self._execute({"code": "", "opType": JAIB_OP_QUERY_REF, "ReferenceID": ref_id})
        if not ok:
            return False, dec
        amount = decimal_or_zero(dec.get("amount"))
        currency = dec.get("currencyName") or dec.get("currency") or "ريال يمني"
        reference_id = dec.get("referenceID") or ref_id
        tx_type = dec.get("typeName") or dec.get("operationType")
        note = dec.get("note") or dec.get("description")
        sender_name = dec.get("senderName") or dec.get("customerName") or dec.get("payerName") or dec.get("name")
        sender_phone = (
            dec.get("senderPhone") or dec.get("payerPhone") or dec.get("fromPhone")
            or dec.get("alternateNumber") or dec.get("phone") or dec.get("mobile")
        )
        tx_date = None
        for field in dec.get("fields") or []:
            fname = str(field.get("name", ""))
            fvalue = str(field.get("value", ""))
            if "تاريخ" in fname and not tx_date:
                tx_date = fvalue
            if not sender_name and ("اسم" in fname or "مرسل" in fname):
                sender_name = fvalue
            # لا نستخدم كلمة "رقم" وحدها، لأنها قد تكون رقم العملية لا رقم المرسل.
            if not sender_phone and _sender_key(fname):
                sender_phone = fvalue
            if not tx_type and "نوع" in fname:
                tx_type = fvalue
            if not note and "ملاحظ" in fname:
                note = fvalue
        tx = JaibTx(amount=amount, currency=currency, reference_id=reference_id, sender_name=sender_name, sender_phone=sender_phone, tx_date=tx_date, tx_type=tx_type, note=note, raw=dec)
        return True, tx


# ============================================================
# فحص اتصال Jaib الآمن — لا ينفذ أي عملية مالية
# ============================================================

_JAIB_HEALTH_LOCK = threading.Lock()
_JAIB_REQUIRED_SETTINGS = {
    "JAIB_PHONE": lambda: JAIB_PHONE,
    "JAIB_PASSWORD": lambda: JAIB_PASSWORD,
    "JAIB_FCM_KEY_B64": lambda: JAIB_FCM_KEY_B64,
    "JAIB_FCM_IV_B64": lambda: JAIB_FCM_IV_B64,
    "JAIB_CLIENT_IV_B64": lambda: JAIB_CLIENT_IV_B64,
    "JAIB_DEVICE_ID": lambda: JAIB_DEVICE_ID,
    "JAIB_SMS_CODE": lambda: JAIB_SMS_CODE,
    "JAIB_TKN_NOT": lambda: JAIB_TKN_NOT,
    "JAIB_INIT_VALUE": lambda: JAIB_INIT_VALUE,
    "JAIB_AUTH_HEADER": lambda: JAIB_AUTH_HEADER,
    "JAIB_CLIENT_KEY": lambda: os.getenv("JAIB_CLIENT_KEY"),
    "JAIB_CLIENT_SECRET": lambda: os.getenv("JAIB_CLIENT_SECRET"),
    "JAIB_RETURN_KEY": lambda: os.getenv("JAIB_RETURN_KEY"),
}


def _jaib_safe_reason(value: Any) -> str:
    text = redact_sensitive_text(str(value or "سبب غير معروف")).strip()
    return text[:500] or "سبب غير معروف"


def jaib_health_state() -> dict[str, Any]:
    status = str(DBX.get_setting("jaib_health_status") or "UNKNOWN").upper()
    return {
        "ok": status == "CONNECTED",
        "status": status,
        "configured": JaibClient.enabled(),
        "reason": str(DBX.get_setting("jaib_health_reason") or "لم يُجرَ الفحص بعد"),
        "checked_at": str(DBX.get_setting("jaib_health_checked_at") or "—"),
        "response_ms": int(DBX.get_setting("jaib_health_response_ms") or 0),
    }


def _store_jaib_health(*, ok: bool, reason: str, response_ms: int) -> None:
    DBX.set_setting("jaib_health_status", "CONNECTED" if ok else "DISCONNECTED")
    DBX.set_setting("jaib_health_reason", _jaib_safe_reason(reason))
    DBX.set_setting("jaib_health_checked_at", now_str())
    DBX.set_setting("jaib_health_response_ms", str(max(0, int(response_ms))))


def jaib_health_check() -> dict[str, Any]:
    """Bootstrap and log in to Jaib without querying or moving money."""
    if not _JAIB_HEALTH_LOCK.acquire(blocking=False):
        current = jaib_health_state()
        current["previous_status"] = current["status"]
        current["check_in_progress"] = True
        return current
    try:
        previous = jaib_health_state()
        started = time.monotonic()
        missing = [name for name, getter in _JAIB_REQUIRED_SETTINGS.items() if not getter()]
        if missing:
            reason = "إعدادات الاتصال غير مكتملة: " + ", ".join(missing)
            elapsed = int((time.monotonic() - started) * 1000)
            _store_jaib_health(ok=False, reason=reason, response_ms=elapsed)
            current = jaib_health_state()
            current["previous_status"] = previous["status"]
            return current

        client = JaibClient()
        # Health checks use the same connection timeouts as real Jaib calls.
        # The former 8s cap produced false 'disconnected' reports.
        client.request_timeout=JAIB_REQUEST_TIMEOUT_SECONDS
        client.init_timeout=JAIB_INIT_TIMEOUT_SECONDS
        ok = False
        reason = "فشل تهيئة اتصال Jaib"
        try:
            if client.init():
                ok, login_reason = client.login()
                reason = "الاتصال وتسجيل الدخول يعملان" if ok else _jaib_safe_reason(login_reason)
        except Exception as exc:
            if previous["status"] != "DISCONNECTED":
                log.warning("Jaib health check failed error=%s", type(exc).__name__, exc_info=True)
            reason = _jaib_safe_reason(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                events=getattr(client,'diagnostics',[])
                DBX.set_setting('jaib_transport_diagnostics',json.dumps(events,ensure_ascii=False))
                if not ok and events:
                    event=events[-1]
                    reason=f"{event['stage']}: {event['code']}"+(f" HTTP {event['http_status']}" if event.get('http_status') else '')
                client.session.close()
            except Exception:
                pass

        elapsed = int((time.monotonic() - started) * 1000)
        _store_jaib_health(ok=ok, reason=reason, response_ms=elapsed)
        current = jaib_health_state()
        current["previous_status"] = previous["status"]
        return current
    finally:
        _JAIB_HEALTH_LOCK.release()


async def monitor_jaib_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await asyncio.to_thread(jaib_health_check)
    previous = str(result.get("previous_status") or "UNKNOWN")
    current = str(result.get("status") or "UNKNOWN")
    if previous == current or previous == "UNKNOWN":
        return
    if result.get("ok"):
        text = (
            "<b>✅ عاد اتصال Jaib للعمل</b>\n\n"
            f"وقت الفحص: <code>{html(result['checked_at'])}</code>\n"
            f"زمن الاستجابة: <code>{int(result['response_ms'])} ms</code>"
        )
    else:
        text = (
            "<b>🚨 انقطع اتصال Jaib</b>\n\n"
            f"السبب: <code>{html(result['reason'])}</code>\n"
            f"وقت الفحص: <code>{html(result['checked_at'])}</code>\n\n"
            "سيستمر الفحص تلقائيًا وسيصل إشعار عند عودة الاتصال."
        )
    await notify_admin(context, render_bot_text('notice.bot.providers.jaib.py:375','{v0}',[str(text)]))




_export_globals(globals())

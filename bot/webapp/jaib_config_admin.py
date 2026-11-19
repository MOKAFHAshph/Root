"""Primary-admin-only Telegram Mini App credential/receiver configuration editor.

Secrets are accepted only via a signed initData HTTPS request; none are reflected,
logged, saved to a database, or stored in the web process between preview/commit.
The Mini App keeps submitted text in its WebView memory until confirmation.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from bot.config import ADMIN_ID, BASE_DIR, BOT_TOKEN

# Only these known fields can be imported; raw AES/session blobs are never keys.
SCHEMA = {
    "JAIB_APP_VERSION": ("clientInfo", "AppVersion", 20),
    "JAIB_CLIENT_KEY": ("clientInfo", "clientKey", 120),
    "JAIB_CLIENT_SECRET": ("clientInfo", "clientSecret", 250),
    "JAIB_RETURN_KEY": ("clientInfo", "returnKey", 120),
    "JAIB_DEVICE_ID": ("deviceInfo", "deviceId", 250),
    "JAIB_TKN_NOT": ("deviceInfo", "tknNot", 1500),
    "JAIB_DEVICE_INFO": ("deviceInfo", "deviceInfo", 250),
    "JAIB_OTHER_INFO": ("deviceInfo", "otherInfo", 1200),
    "JAIB_LANG_ID": ("deviceInfo", "langId", 4),
    "JAIB_SOURCE_TYPE": ("deviceInfo", "sourceType", 4),
    "JAIB_UPDATE_DATA": (None, "UpdateData", 100),
    "JAIB_PHONE": (None, "userName", 40),
    "JAIB_PASSWORD": (None, "password", 250),
    "JAIB_SMS_CODE": (None, "smsCode", 250),
}
RECEIVERS = {
    "JAIB_DISPLAY_NAME": 100,
    "JAIB_PRIMARY_PHONE": 40,
    "JAIB_SECONDARY_PHONE": 40,
    "JAIB_SECONDARY_BENEFICIARY_NAME": 100,
}
ALLOWED = frozenset(SCHEMA) | frozenset(RECEIVERS)
LOCK = threading.Lock()
PENDING: dict[str, tuple[int, float, str, str]] = {}
EXPIRE_S = 180


def _reject(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


def _admin(request: Request) -> int:
    # Never accept Telegram initData in URL/query parameters.
    signed = request.headers.get("X-Telegram-Init-Data", "")
    if not signed or len(signed) > 12000 or not BOT_TOKEN:
        _reject(401, "افتح هذه الصفحة من زر البوت الخاص.")
    pairs_raw = parse_qsl(signed, keep_blank_values=True)
    pairs = dict(pairs_raw)
    if len(pairs) != len(pairs_raw):
        _reject(401, "معرف تيليجرام غير صالح.")
    received = pairs.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
        _reject(401, "توقيع تيليجرام غير موجود.")
    canonical = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    correct = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(correct, received.lower()):
        _reject(401, "فشل التحقق من هوية تيليجرام.")
    try:
        issued = int(pairs.get("auth_date", "0"))
        uid = int(json.loads(pairs["user"])["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _reject(401, "هوية تيليجرام غير صالحة.")
    age = time.time() - issued
    if age < -30 or age > 300:
        _reject(401, "انتهت صلاحية جلسة التحرير؛ أعد فتح الصفحة من البوت.")
    if uid != int(ADMIN_ID):
        _reject(403, "التعديل للأدمن الرئيسي فقط.")
    if pairs.get("chat_type") not in (None, "private"):
        _reject(403, "افتحها من المحادثة الخاصة.")
    return uid


def _report(raw: str) -> dict:
    """Recover the first complete decrypted login JSON from arbitrary Reqable text."""
    if not isinstance(raw, str) or len(raw) > 30000:
        raise ValueError("النص طويل جداً أو غير صالح.")
    if not raw.strip():
        return {}
    decoder = json.JSONDecoder()
    # A VAL ciphertext alone cannot be parsed without decrypting the payload.
    for match in re.finditer(r"\{", raw):
        try:
            candidate, _ = decoder.raw_decode(raw[match.start():])
        except ValueError:
            continue
        if (isinstance(candidate, dict) and isinstance(candidate.get("clientInfo"), dict)
                and isinstance(candidate.get("deviceInfo"), dict)):
            return candidate
    raise ValueError("لم أجد JSON مفكوكاً يحتوي clientInfo وdeviceInfo. VAL المشفر وحده لا يكفي.")


def _value(v: object, max_len: int) -> str:
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise ValueError("البيانات المستخرجة تحتوي على قيمة غير صالحة.")
    val = str(v).strip()
    if not val or len(val) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in val):
        raise ValueError("البيانات المستخرجة ناقصة أو غير صالحة.")
    return val


def parse_update(body: dict) -> dict[str, str]:
    if not isinstance(body, dict):
        raise ValueError("تنسيق البيانات غير صالح.")
    raw = body.get("raw", "")
    parsed = _report(raw)
    proposed = {}
    if parsed:
        for dest, (section, source, limit) in SCHEMA.items():
            src = parsed.get(section) if section else parsed
            if isinstance(src, dict) and source in src:
                proposed[dest] = _value(src[source], limit)
        if len(proposed) < 7:
            raise ValueError("تقرير جلسة جيب غير مكتمل؛ لم نغيّر أي شيء.")
        if not re.fullmatch(r"[0-9+]{6,20}", proposed.get("JAIB_PHONE", "")):
            raise ValueError("رقم الحساب في تقرير جيب غير صالح.")
        for key in ("JAIB_APP_VERSION", "JAIB_LANG_ID", "JAIB_SOURCE_TYPE"):
            if key in proposed and not re.fullmatch(r"[0-9]{1,6}", proposed[key]):
                raise ValueError("تقرير جيب يحتوي على رقم إصدار أو مصدر غير صالح.")
        if "JAIB_OTHER_INFO" in proposed:
            try:
                info = json.loads(proposed["JAIB_OTHER_INFO"])
            except ValueError as exc:
                raise ValueError("otherInfo غير صالح في تقرير جيب.") from exc
            if not isinstance(info, dict) or len(info) > 20:
                raise ValueError("otherInfo يحتوي على بنية غير متوقعة.")
    display = body.get("receiver", {})
    if not isinstance(display, dict) or set(display) - set(RECEIVERS):
        raise ValueError("حقول المستلم غير معروفة.")
    for key, limit in RECEIVERS.items():
        v = display.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > limit or "\n" in v or "\r" in v:
            raise ValueError("بيانات المستلم غير صالحة.")
        v = v.strip()
        if v:
            if key.endswith("PHONE") and not re.fullmatch(r"\+?[0-9 -]{5,40}", v):
                raise ValueError("رقم التحويل غير صالح.")
            proposed[key] = v
        elif key == "JAIB_SECONDARY_PHONE" and body.get("clear_secondary") is True:
            proposed[key] = ""
    if not proposed:
        raise ValueError("ألصق تقريراً مفكوكاً أو اكتب بيانات مستلم جديدة.")
    return proposed


def _env_lines_and_values() -> tuple[list[str], dict[str, str], bytes]:
    env = BASE_DIR / ".env"
    data = env.read_bytes()
    if len(data) > 1_000_000:
        raise ValueError("حجم ملف .env غير متوقع.")
    content = data.decode("utf-8")
    lines = content.splitlines(keepends=True)
    # Avoid import of bot.config for current values: it was loaded before edits.
    from dotenv import dotenv_values
    values = dotenv_values(env)
    return lines, {k: str(v or "") for k, v in values.items()}, data


def _delta(proposed: dict[str, str], existing: dict[str, str]) -> list[str]:
    return sorted(k for k, v in proposed.items() if v != existing.get(k, ""))


def _digest(proposed: dict[str, str], original: bytes) -> str:
    return hashlib.sha256(original + b"\0" + json.dumps(
        proposed, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()


def _render(lines: list[str], updates: dict[str, str]) -> bytes:
    result = []
    seen: set[str] = set()
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            if key in seen:
                raise ValueError("ملف .env يحتوي على تكرار لأحد إعدادات جيب.")
            seen.add(key)
            # json quoting is also understood by python-dotenv; protects newlines.
            result.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}\n")
        else:
            result.append(line)
    for key in sorted(updates.keys() - seen):
        if result and not result[-1].endswith("\n"):
            result.append("\n")
        result.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}\n")
    return "".join(result).encode("utf-8")


def _save_env(updates: dict[str, str], expected_sha: str) -> Path:
    env = BASE_DIR / ".env"
    if env.is_symlink():
        raise ValueError("ملف .env مرتبط برابط رمزي؛ توقف للحماية.")
    lock = BASE_DIR / ".jaib_admin_config.lock"
    with lock.open("a+b") as lockfile:
        os.chmod(lock, 0o600)
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        lines, current, original = _env_lines_and_values()
        if _digest(updates, original) != expected_sha:
            raise ValueError("تغيّرت الإعدادات منذ المعاينة. أعد المعاينة قبل الحفظ.")
        changed = {k: v for k, v in updates.items() if current.get(k, "") != v}
        if not changed:
            raise ValueError("لا توجد تغييرات جديدة.")
        content = _render(lines, changed)
        from dotenv import dotenv_values
        from io import StringIO
        parsed = dotenv_values(stream=StringIO(content.decode()))
        if any(parsed.get(k) != v for k, v in changed.items()):
            raise ValueError("تعذر التحقق من ملف الإعدادات الجديد.")

        backup_dir = BASE_DIR / ".jaib_admin_config_backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        backup = backup_dir / f"{stamp}_{secrets.token_hex(4)}.env.bak"
        with backup.open("xb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(original)
            f.flush()
            os.fsync(f.fileno())
        tempname = None
        try:
            fd, tempname = tempfile.mkstemp(prefix=".jaib_env_", dir=BASE_DIR)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tempname, env)
            dirfd = os.open(BASE_DIR, os.O_RDONLY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
        finally:
            if tempname and os.path.exists(tempname):
                os.unlink(tempname)
        return backup


def _restart_service() -> bool:
    # No shell, no user-controlled command fragments, delayed to finish HTTP reply.
    exe = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not exe or not systemctl:
        return False
    unit = f"v20-jaib-admin-reload-{int(time.time())}-{os.getpid()}"
    try:
        result = subprocess.run(
            [exe, "--quiet", "--on-active=12s", f"--unit={unit}", systemctl, "restart", "v20.service"],
            capture_output=True, timeout=8, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _headers():
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


async def _body(request: Request) -> dict:
    try:
        length = int(request.headers.get("Content-Length", "0") or "0")
    except ValueError:
        _reject(400, "حجم الطلب غير صالح.")
    if length > 65000:
        _reject(413, "حجم الطلب كبير.")
    content = await request.body()
    if len(content) > 65000:
        _reject(413, "حجم الطلب كبير.")
    try:
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        _reject(400, "تنسيق الطلب غير صالح.")


def install(app: FastAPI) -> None:
    ui = Path(__file__).with_name("jaib_config_admin.html")

    @app.get("/api/admin/jaib-config/ui", include_in_schema=False)
    async def jaib_config_ui():
        nonce = secrets.token_urlsafe(18)
        html = ui.read_text(encoding="utf-8")
        html = html.replace('<style>', f'<style nonce="{nonce}">').replace('<script>\n', f'<script nonce="{nonce}">\n')
        headers = _headers()
        headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; form-action 'none'; "
            f"style-src 'nonce-{nonce}'; script-src https://telegram.org 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' data:"
        )
        return HTMLResponse(html, headers=headers)

    @app.get("/api/admin/jaib-config/current", include_in_schema=False)
    async def jaib_config_current(request: Request):
        _admin(request)
        try:
            _, values, _ = _env_lines_and_values()
        except (OSError, ValueError):
            _reject(503, "لا يمكن قراءة ملف الإعدادات.")
        return JSONResponse({"receiver": {k: values.get(k, "") for k in RECEIVERS}}, headers=_headers())

    @app.post("/api/admin/jaib-config/preview", include_in_schema=False)
    async def jaib_config_preview(request: Request):
        uid = _admin(request)
        body = await _body(request)
        try:
            proposed = parse_update(body)
            _, existing, original = _env_lines_and_values()
        except (OSError, UnicodeDecodeError, ValueError):
            _reject(400, "تعذرت قراءة البيانات؛ تأكد من وجود JSON مفكوك صالح أو بيانات المستلم.")
        changed = _delta(proposed, existing)
        if not changed:
            return JSONResponse({"changed": [], "already_current": True}, headers=_headers())
        nonce = secrets.token_urlsafe(32)
        valuehash = _digest(proposed, original)
        now = time.monotonic()
        with LOCK:
            expired = [k for k, v in PENDING.items() if v[1] < now]
            for key in expired:
                PENDING.pop(key, None)
            PENDING[nonce] = (uid, now + EXPIRE_S, valuehash, hashlib.sha256(original).hexdigest())
        warnings = []
        if ("JAIB_PHONE" in proposed and proposed["JAIB_PHONE"] != existing.get("JAIB_PRIMARY_PHONE", "")
                and "JAIB_PRIMARY_PHONE" not in proposed):
            warnings.append("رقم تسجيل جيب يختلف عن رقم استقبال التحويلات؛ راجع رقم التحويل قبل التأكيد.")
        if "JAIB_PASSWORD" in changed:
            warnings.append("تغيير كلمة مرور جيب يستلزم إعادة اختبار الدخول. لا تُرسل كلمة المرور في الرسائل.")
        return JSONResponse({
            "nonce": nonce,
            "changed": changed,
            "warnings": warnings,
            "receiver": {k: proposed.get(k, existing.get(k, "")) for k in RECEIVERS},
            "valid_for_seconds": EXPIRE_S,
        }, headers=_headers())

    @app.post("/api/admin/jaib-config/commit", include_in_schema=False)
    async def jaib_config_commit(request: Request):
        uid = _admin(request)
        body = await _body(request)
        nonce = body.get("nonce", "")
        if not isinstance(nonce, str) or len(nonce) > 100:
            _reject(400, "معاينة غير صالحة.")
        with LOCK:
            pending = PENDING.pop(nonce, None)  # One-time: cannot replay.
        if not pending or pending[0] != uid or pending[1] < time.monotonic():
            _reject(409, "انتهت المعاينة؛ أعد المحاولة.")
        if body.get("confirm") != "تحديث جيب":
            _reject(400, "يلزم تأكيد التغيير كتابةً.")
        try:
            proposed = parse_update(body)
            _, _, original = _env_lines_and_values()
            if not hmac.compare_digest(_digest(proposed, original), pending[2]):
                _reject(409, "البيانات تغيّرت بعد المعاينة؛ أعد المحاولة.")
            backup = _save_env(proposed, pending[2])
        except HTTPException:
            raise
        except (OSError, UnicodeDecodeError, ValueError):
            _reject(409, "تعذر حفظ البيانات بأمان؛ لم يُنفذ تحديث جديد.")
        scheduled = _restart_service()
        return JSONResponse({
            "saved": True,
            "restart_scheduled": scheduled,
            "restart_after_seconds": 12 if scheduled else None,
            "backup_created": True,
            "message": "حُفظت الإعدادات. تحقق من اتصال جيب بعد إعادة التشغيل.",
        }, headers=_headers())

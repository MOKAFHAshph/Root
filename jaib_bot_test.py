#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║       بوت جيب — النظام المتكامل للمدفوعات           ║
║       الإصدار 3.0 — ملف واحد كامل                   ║
║  ✅ التحقق من الدفع                                  ║
║  💸 إرسال / سحب للمحافظ                             ║
║  💰 عرض الرصيد بجميع العملات                        ║
║  👥 إدارة المستخدمين                                ║
╚══════════════════════════════════════════════════════╝
"""

# ══════════════════════════════════════════════════════
#  الخطوة 1 — تثبيت المكتبات تلقائياً
# ══════════════════════════════════════════════════════
import subprocess, sys, importlib

def _auto_install():
    needed = {
        'requests':      'requests',
        'Crypto.Cipher': 'pycryptodome',
        'urllib3':       'urllib3',
        'telegram':      'python-telegram-bot==20.7',
    }
    print("📦 التحقق من المكتبات...")
    for mod, pkg in needed.items():
        try:
            importlib.import_module(mod)
            print(f"   ✅ {mod}")
        except ImportError:
            print(f"   ⚙️  تثبيت {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])
            print(f"   ✅ {pkg} — تم التثبيت")
    print("✅ جميع المكتبات جاهزة\n")

_auto_install()

# ══════════════════════════════════════════════════════
#  الخطوة 2 — الاستيرادات
# ══════════════════════════════════════════════════════
import os, time, json, gzip, base64, urllib3, sqlite3, logging
from datetime import datetime
from Crypto.Cipher import AES
import requests as req_lib

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

urllib3.disable_warnings()

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("jaib_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  الخطوة 3 — الإعدادات  ← غيّر ما يلزم
# ══════════════════════════════════════════════════════

BOT_TOKEN   = "7190116639:AAFcX_AmFBOqv5ySJCDMAgxWrxlCHfrAb94"
ADMIN_ID    = 1654759285

# بيانات محفظة جيب الخاصة بك
PHONE       = "781886458"
PASSWORD    = "999777"

# مفاتيح API جيب — لا تغيّرها
FCM_KEY     = base64.b64decode('cKaO7jFQ7JgS2G97QovObIlIK4MHMUcrXJzAxOj8WNI=')
FCM_IV      = base64.b64decode('dbCnTfkJJPMYk09L5qMEgA==')
CLIENT_IV   = base64.b64decode('1T9yGplidi4rj0NAFdd3Gg==')
DEVICE_ID   = "ffffffff-c5ba-f543-0000-000000000020@93f87315-a545-4665-81d3-3a0a73bf456a"
SMS_CODE    = "0qDUPl/hH8g"
TKN_NOT     = ("eGU7rLqLTFqE7tslpjBase:APA91bEtH1D1Uh7FGH_EcvvejHzZp2WI0I8ijef7MicKAt"
               "bKIfZhTPlN9hv9JAGV2UVBS3r4X_wP0a2zVtZPRXSY5YebGXFvDUVCPN6Zg3jcYpy11oHyd4g")
INIT_VALUE  = "Cn9dxD4IEqmmTOu4+0VeX5hdY3c7m2WRPXHy93"
AUTH_HEADER = "Basic SGF6bWlBZ2VudFNlcnZpY2U6QWRtaW4hI0AyNDU"

BOOTSTRAP_SERVERS = [
    'https://www.w-jaib.com:2074',
    'https://api.jaib.com.ye:1074',
    'https://api3.jaib.com.ye:9974',
    'https://api.e-jaib.com:2174',
    'https://api.ahd.com.ye:6073',
    'https://api.jaib.com.ye:6072',
]

# opType للعمليات المختلفة في جيب
OP_QUERY_REF  = 567   # استعلام برقم مرجع
OP_SEND_PHONE = 100   # إرسال برقم هاتف (جرّب 100 أو 101)
OP_BALANCE    = 200   # رصيد الحساب

DB_FILE = "jaib_bot.db"

# ══════════════════════════════════════════════════════
#  حالات ConversationHandler
# ══════════════════════════════════════════════════════
(
    WAIT_REF,          # انتظار رقم مرجع العملية
    WAIT_PHONE,        # انتظار رقم هاتف المستلم
    WAIT_AMOUNT,       # انتظار المبلغ
    WAIT_CONFIRM_SEND, # انتظار تأكيد الإرسال
    WAIT_USER_ID,      # انتظار ID مستخدم (إضافة/حذف)
) = range(5)

# ══════════════════════════════════════════════════════
#  الخطوة 4 — قاعدة البيانات
# ══════════════════════════════════════════════════════

def db_init():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS used_tx (
            ref_id       TEXT PRIMARY KEY,
            user_id      INTEGER,
            username     TEXT,
            full_name    TEXT,
            amount       REAL,
            currency     TEXT,
            sender_name  TEXT,
            sender_phone TEXT,
            tx_date      TEXT,
            tx_type      TEXT,
            note         TEXT,
            verified_at  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_tx (
            ref_id       TEXT PRIMARY KEY,
            to_phone     TEXT,
            amount       REAL,
            currency     TEXT,
            note         TEXT,
            status       TEXT,
            sent_at      TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS allowed_users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            added_by   INTEGER,
            added_at   TEXT
        )
    """)
    con.commit()
    con.close()
    log.info("✅ قاعدة البيانات جاهزة")

# — العمليات المحقّقة —

def db_is_used(ref_id):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT * FROM used_tx WHERE ref_id=?", (ref_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    cols = ["ref_id","user_id","username","full_name","amount","currency",
            "sender_name","sender_phone","tx_date","tx_type","note","verified_at"]
    return dict(zip(cols, row))

def db_mark_used(ref_id, user_id, username, full_name,
                 amount, currency, sender_name, sender_phone,
                 tx_date, tx_type, note):
    con = sqlite3.connect(DB_FILE)
    con.execute("""INSERT OR IGNORE INTO used_tx VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ref_id, user_id, username, full_name, amount, currency,
         sender_name, sender_phone, tx_date, tx_type, note,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    con.commit()
    con.close()

def db_save_sent(ref_id, to_phone, amount, currency, note, status):
    con = sqlite3.connect(DB_FILE)
    con.execute("""INSERT OR IGNORE INTO sent_tx VALUES(?,?,?,?,?,?,?)""",
        (ref_id, to_phone, amount, currency, note, status,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    con.commit()
    con.close()

def db_recent_tx(limit=10):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""SELECT ref_id,full_name,username,amount,currency,sender_name,verified_at
                   FROM used_tx ORDER BY verified_at DESC LIMIT ?""", (limit,))
    rows = cur.fetchall()
    con.close()
    return rows

def db_user_tx(user_id, limit=5):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""SELECT ref_id,amount,currency,sender_name,verified_at
                   FROM used_tx WHERE user_id=? ORDER BY verified_at DESC LIMIT ?""",
                (user_id, limit))
    rows = cur.fetchall()
    con.close()
    return rows

def db_stats():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM used_tx")
    tc, ts = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM allowed_users")
    uc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM sent_tx WHERE status='success'")
    sc, ss = cur.fetchone()
    con.close()
    return {"tx_count":tc,"tx_sum":ts,"u_count":uc,"sent_count":sc,"sent_sum":ss}

# — المستخدمون —

def db_is_allowed(uid):
    if uid == ADMIN_ID:
        return True
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (uid,))
    res = cur.fetchone()
    con.close()
    return res is not None

def db_add_user(uid, username, full_name, added_by):
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT OR REPLACE INTO allowed_users VALUES(?,?,?,?,?)",
        (uid, username, full_name, added_by,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    con.commit()
    con.close()

def db_del_user(uid):
    con = sqlite3.connect(DB_FILE)
    con.execute("DELETE FROM allowed_users WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def db_list_users():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id,username,full_name,added_at FROM allowed_users ORDER BY added_at DESC")
    rows = cur.fetchall()
    con.close()
    return rows

# ══════════════════════════════════════════════════════
#  الخطوة 5 — التشفير / فك التشفير
# ══════════════════════════════════════════════════════

def _pad(d):
    p = 16 - len(d) % 16
    return d + bytes([p]) * p

def _unpad(d):
    return d[:-d[-1]]

def encrypt(data, key, iv):
    j = json.dumps(data, ensure_ascii=False, separators=(',',':')).encode()
    c = gzip.compress(j, 6)
    b = base64.b64encode(c)
    e = AES.new(key, AES.MODE_CBC, iv).encrypt(_pad(b))
    return base64.b64encode(e).decode()

def decrypt(payload, key, iv):
    e = base64.b64decode(payload)
    d = AES.new(key, AES.MODE_CBC, iv).decrypt(e)
    d = _unpad(d)
    b = base64.b64decode(d.decode())
    c = gzip.decompress(b)
    return json.loads(c.decode('utf-8-sig').strip())

# ══════════════════════════════════════════════════════
#  الخطوة 6 — عميل جيب الكامل
# ══════════════════════════════════════════════════════

class JaibClient:
    def __init__(self):
        self.http = req_lib.Session()
        self.http.verify = False
        self.http.headers.update({
            'Content-Type':   'application/json; charset=utf-8',
            'User-Agent':     'okhttp/5.1.0',
            'Accept':         'application/json',
            'Accept-Encoding':'gzip',
            'Authorization':  AUTH_HEADER,
        })
        self.client_key   = None
        self.session_key  = None
        self.session_iv   = None
        self.access_token = None
        self.servers      = []

    # — إرسال طلب HTTP —
    def _req(self, path, payload, hdrs=None, servers=None):
        svrs = servers or self.servers or BOOTSTRAP_SERVERS
        for s in svrs:
            host = s.replace('https://', '')
            h = dict(hdrs or {})
            h['Host'] = host
            try:
                r = self.http.post(s + path, json={"value": payload},
                                   headers=h, timeout=15)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
        return None

    # — تهيئة الاتصال —
    def init(self):
        data = {"DtTime": int(time.time()*1000), "envir":1,
                "value": INIT_VALUE, "VersNum":446}
        payload = encrypt(data, FCM_KEY, FCM_IV)
        for s in BOOTSTRAP_SERVERS:
            host = s.replace('https://','')
            try:
                r = self.http.post(s+"/api/HzHelp/GetInitWallet",
                    json={"value":payload}, headers={'Host':host}, timeout=10)
                if r.status_code == 200:
                    resp = r.json()
                    if resp.get("success") and resp.get("result"):
                        dec = decrypt(resp["result"], FCM_KEY, FCM_IV)
                        self.client_key = base64.b64decode(dec["Kl"])
                        self.servers = list(dict.fromkeys(dec.get("lsIP",[])))
                        for s2 in BOOTSTRAP_SERVERS:
                            if s2 not in self.servers:
                                self.servers.append(s2)
                        return True
            except Exception:
                pass
        return False

    # — تسجيل الدخول —
    def login(self):
        data = {
            "clientInfo": {"AppVersion":"446","clientKey":"4XEPFXX0qO",
                           "clientSecret":"mangmasng","returnKey":"425978"},
            "deviceInfo": {
                "ConfirmCode":"", "deviceId":DEVICE_ID,
                "deviceInfo":"samsung SM-A716U", "tknNot":TKN_NOT,
                "langId":1,
                "otherInfo": json.dumps({"brand":"samsung","deviceName":"samsung",
                    "ip":"","loginMethodType":2,"model":"SM-A716U","os":"33"}),
                "sourceType":1
            },
            "password":PASSWORD, "smsCode":SMS_CODE,
            "UpdateData":"20220329171", "userName":PHONE
        }
        payload = encrypt(data, self.client_key, CLIENT_IV)
        resp = self._req("/api/TokenAuth/LogE2", payload)
        if not resp or not resp.get("success"):
            return False, "فشل تسجيل الدخول"
        raw = resp.get("result","")
        if isinstance(raw, dict): raw = raw.get("value","")
        if not raw: return False, "رد فارغ"
        try:
            dec = decrypt(raw, self.client_key, CLIENT_IV)
        except Exception as e:
            return False, f"فشل فك التشفير: {e}"
        self.access_token = dec.get("accessToken")
        self.session_key  = base64.b64decode(dec["k"])
        self.session_iv   = base64.b64decode(dec["mobileScon"])
        return True, dec

    # — helper: إرسال طلب Execute —
    def _execute(self, body: dict):
        payload = encrypt(body, self.session_key, self.session_iv)
        hdrs = {}
        if self.access_token:
            hdrs["auth"] = self.access_token
        resp = self._req("/api/v1/Wallet/ExecuteE2", payload, hdrs)
        if not resp:
            return False, "فشل الاتصال بخوادم جيب"
        if not resp.get("success"):
            err = resp.get("error", {})
            return False, err.get("message","خطأ غير معروف")
        raw = resp.get("result","")
        if isinstance(raw, dict): raw = raw.get("value","")
        if not raw: return False, "رد فارغ"
        try:
            dec = decrypt(raw, self.session_key, self.session_iv)
        except Exception as e:
            return False, f"فشل فك التشفير: {e}"
        return True, dec

    # ── ① الاستعلام برقم المرجع ──
    def query_ref(self, ref_id: str):
        ok, dec = self._execute({"code":"","opType":OP_QUERY_REF,"ReferenceID":ref_id})
        if not ok:
            return False, dec
        amount   = dec.get("amount")
        currency = dec.get("currencyName") or dec.get("currency","ريال يمني")
        ref      = dec.get("referenceID") or ref_id
        tx_type  = dec.get("typeName") or dec.get("operationType")
        note     = dec.get("note") or dec.get("description")
        sname    = dec.get("senderName") or dec.get("customerName") or dec.get("name")
        sphone   = dec.get("senderPhone") or dec.get("phone") or dec.get("mobile")
        tx_date  = None
        for f in (dec.get("fields") or []):
            fn = str(f.get("name",""))
            fv = f.get("value","")
            if "تاريخ" in fn and not tx_date:    tx_date  = fv
            if not sname  and ("اسم" in fn or "مرسل" in fn): sname   = fv
            if not sphone and ("هاتف" in fn or "جوال" in fn or "رقم" in fn): sphone  = fv
            if not tx_type and "نوع" in fn:       tx_type  = fv
            if not note and ("ملاحظ" in fn):      note     = fv
        return True, {"amount":amount,"currency":currency,"reference_id":ref,
                      "sender_name":sname,"sender_phone":sphone,
                      "tx_date":tx_date,"tx_type":tx_type,"note":note,"raw":dec}

    # ── ② الرصيد ──
    def get_balance(self):
        """
        يجرّب عدة opType للحصول على الرصيد.
        يُرجع قائمة [{"currency":..., "amount":...}, ...]
        """
        BALANCE_OPS = [200, 201, 202, 300, 400]
        for op in BALANCE_OPS:
            ok, dec = self._execute({"code":"","opType":op})
            if not ok:
                continue
            # المحاولة 1: حقل balances قائمة
            if dec.get("balances"):
                return True, dec["balances"]
            # المحاولة 2: حقل accounts
            if dec.get("accounts"):
                return True, dec["accounts"]
            # المحاولة 3: الحقل مباشر amount + currencyName
            if dec.get("amount") is not None:
                return True, [{"currency": dec.get("currencyName","ريال"), "amount":dec["amount"]}]
            # المحاولة 4: البحث في fields
            if dec.get("fields"):
                items = []
                for f in dec["fields"]:
                    if f.get("amount") is not None:
                        items.append({"currency": f.get("name","—"), "amount": f["amount"]})
                if items:
                    return True, items
        return False, "تعذّر جلب الرصيد — قد يكون opType مختلفاً لهذا الحساب"

    # ── ③ إرسال / سحب ──
    def send_money(self, to_phone: str, amount: float, currency_id: int = 1, note: str = ""):
        """
        إرسال مبلغ لرقم هاتف أو رقم بديل.
        currency_id: 1=ريال يمني, 2=دولار, 3=ريال سعودي (قد يختلف حسب API جيب)
        يُجرّب opType = 100 أولاً ثم 101
        """
        SEND_OPS = [100, 101, 102]
        body_base = {
            "toPhone":    to_phone,
            "amount":     amount,
            "currencyId": currency_id,
            "note":       note,
            "code":       "",
        }
        for op in SEND_OPS:
            body = dict(body_base)
            body["opType"] = op
            ok, dec = self._execute(body)
            if ok:
                ref_id   = (dec.get("referenceID") or dec.get("ref_id")
                             or dec.get("transactionId") or "—")
                tx_date  = None
                for f in (dec.get("fields") or []):
                    if "تاريخ" in str(f.get("name","")):
                        tx_date = f.get("value")
                        break
                return True, {
                    "ref_id":     ref_id,
                    "to_phone":   to_phone,
                    "amount":     amount,
                    "currency":   dec.get("currencyName","ريال يمني"),
                    "tx_date":    tx_date or datetime.now().strftime('%Y-%m-%d %H:%M'),
                    "raw":        dec,
                }
            # إذا الخطأ "رقم خاطئ" لا فائدة من تجربة opType آخر
            if "هاتف" in str(dec) or "مستخدم" in str(dec) or "غير موجود" in str(dec):
                return False, dec
        return False, f"فشلت جميع المحاولات. آخر خطأ: {dec}"

# ══════════════════════════════════════════════════════
#  الخطوة 7 — helper: بناء عميل جاهز (init+login)
# ══════════════════════════════════════════════════════

async def _build_client():
    """يُنشئ JaibClient ويُسجّل الدخول. يُرجع (client, None) أو (None, رسالة_خطأ)"""
    c = JaibClient()
    if not c.init():
        return None, "❌ فشل الاتصال بخوادم جيب"
    ok, res = c.login()
    if not ok:
        return None, f"❌ فشل تسجيل الدخول: {res}"
    return c, None

# ══════════════════════════════════════════════════════
#  الخطوة 8 — لوحات الأزرار الشفافة
# ══════════════════════════════════════════════════════

def kb_user_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍  التحقق من عملية دفع",     callback_data="check_payment")],
        [InlineKeyboardButton("💰  عرض الرصيد الحالي",       callback_data="balance_menu")],
        [InlineKeyboardButton("📋  عملياتي السابقة",          callback_data="my_history")],
        [InlineKeyboardButton("ℹ️   كيفية الاستخدام",         callback_data="help")],
    ])

def kb_admin_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍  التحقق من عملية",          callback_data="check_payment")],
        [InlineKeyboardButton("💸  إرسال / سحب",              callback_data="send_money")],
        [InlineKeyboardButton("💰  عرض الرصيد الحالي",       callback_data="balance_menu")],
        [InlineKeyboardButton("👥  إدارة المستخدمين",         callback_data="manage_users")],
        [InlineKeyboardButton("📊  الإحصائيات",               callback_data="stats")],
        [InlineKeyboardButton("📋  آخر العمليات",             callback_data="recent_tx")],
    ])

def kb_balance_currencies():
    """اختيار العملة لعرض الرصيد"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇾🇪  الريال اليمني",            callback_data="bal_1")],
        [InlineKeyboardButton("🇺🇸  الدولار الأمريكي",         callback_data="bal_2")],
        [InlineKeyboardButton("🇸🇦  الريال السعودي",           callback_data="bal_3")],
        [InlineKeyboardButton("📊  جميع العملات",              callback_data="bal_all")],
        [InlineKeyboardButton("🔙  رجوع",                     callback_data="main_menu")],
    ])

def kb_manage_users():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  إضافة مستخدم",             callback_data="add_user")],
        [InlineKeyboardButton("🗑️   حذف مستخدم",              callback_data="del_user")],
        [InlineKeyboardButton("📋  قائمة المستخدمين",         callback_data="list_users")],
        [InlineKeyboardButton("🔙  رجوع",                     callback_data="main_menu")],
    ])

def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌  إلغاء",                    callback_data="main_menu")],
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙  القائمة الرئيسية",         callback_data="main_menu")],
    ])

def kb_confirm_send(to_phone, amount, currency_label):
    """أزرار تأكيد / إلغاء الإرسال"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅  تأكيد الإرسال",
                callback_data=f"do_send|{to_phone}|{amount}|{currency_label}"
            ),
            InlineKeyboardButton("❌  إلغاء", callback_data="main_menu"),
        ],
    ])

def kb_after_verify(ref_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  تأكيد الاستلام",           callback_data=f"confirm_{ref_id}")],
        [InlineKeyboardButton("🔍  تحقق من عملية أخرى",       callback_data="check_payment")],
        [InlineKeyboardButton("🏠  القائمة الرئيسية",         callback_data="main_menu")],
    ])

# ══════════════════════════════════════════════════════
#  الخطوة 9 — /start
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    if not db_is_allowed(uid):
        await update.message.reply_text(
            "⛔ *ليس لديك صلاحية*\n\nتواصل مع المسؤول.",
            parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_message(ADMIN_ID,
                f"⚠️ *محاولة وصول غير مصرّح*\n"
                f"• {user.full_name} | @{user.username or '—'} | `{uid}`",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        return
    is_admin = (uid == ADMIN_ID)
    kb = kb_admin_main() if is_admin else kb_user_main()
    await update.message.reply_text(
        f"👋 *أهلاً {user.first_name}!*\n\n"
        f"{'🔑 أدمن' if is_admin else '👤 مستخدم'}\n\n"
        "اختر من القائمة:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ══════════════════════════════════════════════════════
#  الخطوة 10 — معالج الأزرار الرئيسي
# ══════════════════════════════════════════════════════

async def btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    # ═══ القائمة الرئيسية ═══
    if data == "main_menu":
        context.user_data.clear()
        is_admin = (uid == ADMIN_ID)
        kb = kb_admin_main() if is_admin else kb_user_main()
        await q.edit_message_text("🏠 *القائمة الرئيسية*\nاختر ما تريد:",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return ConversationHandler.END

    # ═══ التحقق من عملية ═══
    if data == "check_payment":
        if not db_is_allowed(uid):
            await q.answer("⛔ ليس لديك صلاحية!", show_alert=True); return
        context.user_data['action'] = 'check_payment'
        await q.edit_message_text(
            "🔍 *التحقق من عملية دفع*\n\n"
            "أرسل رقم العملية (Reference ID):\n"
            "_مثال: 17771469776285_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_REF

    # ═══ إرسال / سحب — للأدمن فقط ═══
    if data == "send_money":
        if uid != ADMIN_ID:
            await q.answer("⛔ للأدمن فقط!", show_alert=True); return
        context.user_data['action'] = 'send_phone'
        await q.edit_message_text(
            "💸 *إرسال / سحب من المحفظة*\n\n"
            "أرسل رقم هاتف المستلم أو الرقم البديل لمحفظة جيب:\n\n"
            "📌 _مثال: 781234567_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_PHONE

    # ═══ قائمة الرصيد ═══
    if data == "balance_menu":
        if not db_is_allowed(uid):
            await q.answer("⛔ ليس لديك صلاحية!", show_alert=True); return
        await q.edit_message_text(
            "💰 *عرض الرصيد*\n\nاختر العملة:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_balance_currencies())
        return

    # ═══ عرض الرصيد حسب العملة ═══
    if data.startswith("bal_"):
        if not db_is_allowed(uid):
            await q.answer("⛔ ليس لديك صلاحية!", show_alert=True); return
        cur_map = {"bal_1":"ريال يمني","bal_2":"دولار","bal_3":"ريال سعودي","bal_all":"all"}
        chosen = cur_map.get(data, "all")

        await q.edit_message_text("⏳ *جاري جلب الرصيد...*",
                                  parse_mode=ParseMode.MARKDOWN)
        client, err = await _build_client()
        if err:
            await q.edit_message_text(err, reply_markup=kb_back()); return

        ok, balances = client.get_balance()

        if not ok:
            await q.edit_message_text(
                f"❌ *تعذّر جلب الرصيد*\n\n{balances}\n\n"
                "💡 _ملاحظة: قد يكون مسار API الرصيد مختلفاً، "
                "نحتاج تتبع حزم الشبكة من تطبيق جيب لمعرفة opType الصحيح._",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_balance_currencies())
            return

        # تصفية حسب العملة المختارة
        CURRENCY_KEYWORDS = {
            "ريال يمني": ["يمن","yer","ريال يمني"],
            "دولار":      ["دولار","usd","dollar"],
            "ريال سعودي": ["سعودي","sar","saudi"],
        }

        lines = ["💰 *رصيد المحفظة*\n"]
        if chosen == "all":
            for b in (balances if isinstance(balances,list) else [balances]):
                cur  = b.get("currency") or b.get("currencyName") or b.get("name","—")
                amt  = b.get("amount") or b.get("balance") or 0
                lines.append(f"• {cur}: *{float(amt):,.2f}*")
        else:
            kws = CURRENCY_KEYWORDS.get(chosen, [])
            found = False
            for b in (balances if isinstance(balances,list) else [balances]):
                cur = str(b.get("currency") or b.get("currencyName") or b.get("name","")).lower()
                if any(k in cur for k in kws):
                    amt = b.get("amount") or b.get("balance") or 0
                    lines.append(f"• {chosen}: *{float(amt):,.2f}*")
                    found = True
            if not found:
                lines.append(f"• {chosen}: _لا توجد بيانات_")

        lines.append(f"\n🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M')}`")
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_balance_currencies())
        return

    # ═══ تأكيد استلام (بعد تحقق) ═══
    if data.startswith("confirm_"):
        ref_id = data[len("confirm_"):]
        await q.edit_message_text(
            f"☑️ *تم تأكيد الاستلام*\n\n`{ref_id}`\n\nشكراً!",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        try:
            u = q.from_user
            await context.bot.send_message(ADMIN_ID,
                f"☑️ *تأكيد استلام*\n"
                f"• {u.full_name} | @{u.username or '—'}\n"
                f"• رقم: `{ref_id}`",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        return

    # ═══ تأكيد الإرسال الفعلي ═══
    if data.startswith("do_send|"):
        if uid != ADMIN_ID:
            await q.answer("⛔ للأدمن فقط!", show_alert=True); return
        _, to_phone, amount_str, cur_label = data.split("|", 3)
        amount = float(amount_str)
        # تحديد currency_id
        CUR_ID = {"ريال يمني":1, "دولار":2, "ريال سعودي":3}
        cur_id = CUR_ID.get(cur_label, 1)

        await q.edit_message_text("⏳ *جاري تنفيذ عملية الإرسال...*",
                                  parse_mode=ParseMode.MARKDOWN)
        client, err = await _build_client()
        if err:
            await q.edit_message_text(err, reply_markup=kb_back()); return

        ok, result = client.send_money(to_phone, amount, cur_id)

        if not ok:
            await q.edit_message_text(
                f"❌ *فشلت عملية الإرسال*\n\nالسبب: {result}\n\n"
                "💡 _ملاحظة: opType الإرسال يحتاج تأكيداً من حزم الشبكة "
                "الخاصة بتطبيق جيب لضمان الدقة._",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
            return

        ref   = result.get("ref_id","—")
        cur   = result.get("currency","ريال يمني")
        tdate = result.get("tx_date","—")
        amt   = result.get("amount", amount)

        db_save_sent(ref, to_phone, amt, cur, "", "success")

        success_text = (
            f"✅ *تم الإرسال بنجاح!*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 *تفاصيل العملية:*\n\n"
            f"🔢 رقم العملية:\n`{ref}`\n\n"
            f"📱 المستلم: `{to_phone}`\n"
            f"💰 المبلغ: *{amt:,.0f} {cur}*\n"
            f"📅 التاريخ: `{tdate}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M')}`"
        )
        await q.edit_message_text(success_text,
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        # إشعار الأدمن (إشعار لنفسه)
        try:
            await context.bot.send_message(ADMIN_ID,
                f"💸 *تم تنفيذ إرسال*\n"
                f"• إلى: `{to_phone}`\n"
                f"• المبلغ: *{amt:,.0f} {cur}*\n"
                f"• رقم العملية: `{ref}`\n"
                f"• الوقت: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        return

    # ═══ الإحصائيات ═══
    if data == "stats":
        if uid != ADMIN_ID:
            await q.answer("⛔ للأدمن فقط!", show_alert=True); return
        s = db_stats()
        await q.edit_message_text(
            f"📊 *الإحصائيات*\n\n"
            f"🔍 عمليات محقّقة: `{s['tx_count']}`\n"
            f"💰 مجموع المحقّق: `{s['tx_sum']:,.0f}` ريال\n"
            f"💸 إرساليات ناجحة: `{s['sent_count']}`\n"
            f"💸 مجموع المُرسَل: `{s['sent_sum']:,.0f}` ريال\n"
            f"👥 المستخدمون: `{s['u_count']}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        return

    # ═══ آخر العمليات ═══
    if data == "recent_tx":
        if uid != ADMIN_ID:
            await q.answer("⛔ للأدمن فقط!", show_alert=True); return
        rows = db_recent_tx(10)
        if not rows:
            text = "📋 لا توجد عمليات بعد."
        else:
            lines = ["📋 *آخر 10 عمليات:*\n"]
            for ref,fn,un,amt,cur,sname,vat in rows:
                amt_s = f"{amt:,.0f} {cur}" if amt else "—"
                lines.append(
                    f"• `{ref}`\n"
                    f"  💰 {amt_s} | 👤 {sname or '—'}\n"
                    f"  🙋 {fn or un or '—'} | 🕐 {(vat or '')[:16]}"
                )
            text = "\n\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        return

    # ═══ إدارة المستخدمين ═══
    if data == "manage_users":
        if uid != ADMIN_ID:
            await q.answer("⛔ للأدمن فقط!", show_alert=True); return
        await q.edit_message_text("👥 *إدارة المستخدمين*\nاختر:",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb_manage_users())
        return

    if data == "add_user":
        if uid != ADMIN_ID: return
        context.user_data['action'] = 'add_user'
        await q.edit_message_text(
            "➕ *إضافة مستخدم*\n\nأرسل الـ ID:\n_من @userinfobot_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_USER_ID

    if data == "del_user":
        if uid != ADMIN_ID: return
        context.user_data['action'] = 'del_user'
        await q.edit_message_text(
            "🗑️ *حذف مستخدم*\n\nأرسل الـ ID:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_USER_ID

    if data == "list_users":
        if uid != ADMIN_ID: return
        rows = db_list_users()
        if not rows:
            text = "📋 لا يوجد مستخدمون مضافون."
        else:
            lines = [f"📋 *المستخدمون ({len(rows)}):*\n"]
            for i,(u_id,un,fn,at) in enumerate(rows,1):
                lines.append(
                    f"{i}. {fn or 'بدون اسم'}\n"
                    f"   ID: `{u_id}` | @{un or '—'}\n"
                    f"   أُضيف: {(at or '')[:10]}"
                )
            text = "\n\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_manage_users())
        return

    # ═══ كيفية الاستخدام ═══
    if data == "help":
        await q.edit_message_text(
            "📖 *كيفية الاستخدام*\n\n"
            "🔍 *التحقق من دفع:*\n"
            "1️⃣ حوّل المبلغ لمحفظة جيب\n"
            "2️⃣ احفظ رقم العملية\n"
            "3️⃣ أرسله للبوت للتحقق\n\n"
            "💸 *الإرسال (أدمن):*\n"
            "1️⃣ اضغط إرسال/سحب\n"
            "2️⃣ أدخل رقم هاتف المستلم\n"
            "3️⃣ أدخل المبلغ\n"
            "4️⃣ اختر العملة وأكّد\n\n"
            "⚠️ *ملاحظة:*\n"
            "كل رقم عملية يُقبل مرة واحدة فقط",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        return

    # ═══ عملياتي السابقة ═══
    if data == "my_history":
        rows = db_user_tx(uid)
        if not rows:
            text = "📋 لم تُجرِ أي عملية بعد."
        else:
            lines = ["📋 *عملياتك الأخيرة:*\n"]
            for ref,amt,cur,sname,vat in rows:
                amt_s = f"{amt:,.0f} {cur}" if amt else "—"
                lines.append(f"• `{ref}` | {amt_s} | {sname or '—'}\n  🕐 {(vat or '')[:16]}")
            text = "\n\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        return

# ══════════════════════════════════════════════════════
#  الخطوة 11 — معالجات النص حسب الحالة
# ══════════════════════════════════════════════════════

# ── حالة انتظار رقم المرجع (تحقق من عملية) ──
async def on_wait_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    text   = update.message.text.strip()
    action = context.user_data.get('action','')

    if action != 'check_payment':
        return ConversationHandler.END

    if not db_is_allowed(uid):
        await update.message.reply_text("⛔ ليس لديك صلاحية.")
        return ConversationHandler.END

    ref_id = text

    # التحقق من الصيغة
    if not ref_id.isdigit() or not (8 <= len(ref_id) <= 20):
        await update.message.reply_text(
            "❌ رقم العملية غير صالح.\n"
            "يجب أن يكون أرقاماً فقط (8-20 خانة).\nأعد الإرسال:",
            reply_markup=kb_cancel())
        return WAIT_REF

    # هل مستخدمة؟
    used = db_is_used(ref_id)
    if used:
        amt_s = f"{used['amount']:,.0f} {used['currency']}" if used.get('amount') else "—"
        await update.message.reply_text(
            f"⚠️ *هذه العملية مستخدمة من قبل!*\n\n"
            f"• رقم: `{ref_id}`\n• المبلغ: `{amt_s}`\n"
            f"• تاريخ الاستخدام: `{(used.get('verified_at') or '')[:16]}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        u = update.effective_user
        try:
            await context.bot.send_message(ADMIN_ID,
                f"🚨 *محاولة إعادة استخدام عملية!*\n"
                f"• {u.full_name} | @{u.username or '—'} | `{uid}`\n"
                f"• رقم: `{ref_id}`\n"
                f"• سبق استخدامها: `{(used.get('verified_at') or '')[:16]}`",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    # الاستعلام من جيب
    msg = await update.message.reply_text("⏳ *جاري الاستعلام...*",
                                          parse_mode=ParseMode.MARKDOWN)
    client, err = await _build_client()
    if err:
        await msg.edit_text(err, reply_markup=kb_back())
        return ConversationHandler.END

    ok, tx = client.query_ref(ref_id)
    if not ok:
        await msg.edit_text(
            f"❌ *لم يُعثر على العملية*\n\nالسبب: {tx}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_back())
        context.user_data.clear()
        return ConversationHandler.END

    # تسجيل العملية
    u = update.effective_user
    db_mark_used(ref_id, uid, u.username, u.full_name,
                 tx.get('amount'), tx.get('currency','ريال يمني'),
                 tx.get('sender_name'), tx.get('sender_phone'),
                 tx.get('tx_date'), tx.get('tx_type'), tx.get('note'))

    amt_s = f"{tx['amount']:,.0f} {tx['currency']}" if tx.get('amount') else "—"

    result_text = (
        f"✅ *تم التحقق بنجاح!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 رقم العملية:\n`{ref_id}`\n\n"
        f"💰 المبلغ: *{amt_s}*\n"
    )
    if tx.get('sender_name'):  result_text += f"👤 المُرسِل: `{tx['sender_name']}`\n"
    if tx.get('sender_phone'): result_text += f"📞 الهاتف: `{tx['sender_phone']}`\n"
    if tx.get('tx_date'):      result_text += f"📅 التاريخ: `{tx['tx_date']}`\n"
    if tx.get('tx_type'):      result_text += f"🏷️ النوع: `{tx['tx_type']}`\n"
    if tx.get('note'):         result_text += f"📝 ملاحظة: `{tx['note']}`\n"
    result_text += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M')}`"
    )
    await msg.edit_text(result_text, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb_after_verify(ref_id))

    # إشعار الأدمن
    try:
        await context.bot.send_message(ADMIN_ID,
            f"💰 *عملية دفع تم التحقق منها!*\n\n"
            f"👤 {u.full_name} | @{u.username or '—'} | `{uid}`\n\n"
            f"• رقم: `{ref_id}`\n• المبلغ: *{amt_s}*\n"
            + (f"• المُرسِل: `{tx['sender_name']}`\n" if tx.get('sender_name') else "")
            + (f"• الهاتف: `{tx['sender_phone']}`\n" if tx.get('sender_phone') else "")
            + f"• 🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN)
    except Exception: pass

    context.user_data.clear()
    return ConversationHandler.END

# ── حالة انتظار رقم الهاتف (إرسال) ──
async def on_wait_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    if uid != ADMIN_ID:
        return ConversationHandler.END

    # قبول أرقام هاتف يمنية (7-15 خانة)
    phone = text.replace(" ","").replace("-","").replace("+","")
    if not phone.isdigit() or not (7 <= len(phone) <= 15):
        await update.message.reply_text(
            "❌ رقم الهاتف غير صالح.\n"
            "أرسل رقماً صحيحاً (7-15 خانة):",
            reply_markup=kb_cancel())
        return WAIT_PHONE

    context.user_data['send_phone'] = phone
    context.user_data['action']     = 'send_amount'
    await update.message.reply_text(
        f"📱 *المستلم:* `{phone}`\n\n"
        "أرسل المبلغ المراد إرساله:\n"
        "_مثال: 5000_",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
    return WAIT_AMOUNT

# ── حالة انتظار المبلغ ──
async def on_wait_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip().replace(",","")
    if uid != ADMIN_ID:
        return ConversationHandler.END

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ المبلغ غير صالح.\nأرسل رقماً موجباً:",
            reply_markup=kb_cancel())
        return WAIT_AMOUNT

    context.user_data['send_amount'] = amount
    to_phone = context.user_data.get('send_phone','—')

    # اختيار العملة ثم تأكيد
    await update.message.reply_text(
        f"💸 *تأكيد الإرسال*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 إلى: `{to_phone}`\n"
        f"💰 المبلغ: *{amount:,.0f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        "اختر العملة وأكّد:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇾🇪 ريال يمني",    callback_data=f"do_send|{to_phone}|{amount}|ريال يمني")],
            [InlineKeyboardButton("🇺🇸 دولار",         callback_data=f"do_send|{to_phone}|{amount}|دولار")],
            [InlineKeyboardButton("🇸🇦 ريال سعودي",    callback_data=f"do_send|{to_phone}|{amount}|ريال سعودي")],
            [InlineKeyboardButton("❌ إلغاء",           callback_data="main_menu")],
        ])
    )
    return WAIT_CONFIRM_SEND

# ── حالة انتظار ID مستخدم (إضافة/حذف) ──
async def on_wait_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    text   = update.message.text.strip()
    action = context.user_data.get('action','')
    if uid != ADMIN_ID:
        return ConversationHandler.END

    if not text.isdigit():
        await update.message.reply_text(
            "❌ الـ ID يجب أن يكون أرقاماً فقط:", reply_markup=kb_cancel())
        return WAIT_USER_ID

    target = int(text)

    if action == 'add_user':
        db_add_user(target, None, f"مستخدم_{target}", uid)
        await update.message.reply_text(
            f"✅ تمت إضافة `{target}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_main())
        try:
            await context.bot.send_message(target,
                "✅ *تمت إضافتك في نظام الدفع!*\nاضغط /start للبدء.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    elif action == 'del_user':
        db_del_user(target)
        await update.message.reply_text(
            f"🗑️ تم حذف `{target}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_main())

    context.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════════════════
#  الخطوة 12 — أوامر مباشرة للأدمن
# ══════════════════════════════════════════════════════

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("الاستخدام: /adduser 123456789 الاسم"); return
    new_uid = int(args[0])
    name    = " ".join(args[1:]) if len(args)>1 else f"مستخدم_{new_uid}"
    db_add_user(new_uid, None, name, ADMIN_ID)
    await update.message.reply_text(f"✅ تمت إضافة `{name}` | `{new_uid}`",
                                    parse_mode=ParseMode.MARKDOWN)
    try:
        await context.bot.send_message(new_uid,
            "✅ *تمت إضافتك!* اضغط /start", parse_mode=ParseMode.MARKDOWN)
    except Exception: pass

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("الاستخدام: /removeuser 123456789"); return
    db_del_user(int(args[0]))
    await update.message.reply_text(f"🗑️ تم حذف `{args[0]}`",
                                    parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    s = db_stats()
    await update.message.reply_text(
        f"📊 *الإحصائيات*\n\n"
        f"🔍 محقّقة: `{s['tx_count']}` | مجموع: `{s['tx_sum']:,.0f}`\n"
        f"💸 مُرسَلة: `{s['sent_count']}` | مجموع: `{s['sent_sum']:,.0f}`\n"
        f"👥 مستخدمون: `{s['u_count']}`",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════
#  الخطوة 13 — معالج الأخطاء
# ══════════════════════════════════════════════════════

async def err_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("خطأ:", exc_info=context.error)

# ══════════════════════════════════════════════════════
#  الخطوة 14 — main()
# ══════════════════════════════════════════════════════

def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler — يربط كل مسارات الحوار
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(btn)],
        states={
            WAIT_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_wait_ref),
                CallbackQueryHandler(btn),
            ],
            WAIT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_wait_phone),
                CallbackQueryHandler(btn),
            ],
            WAIT_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_wait_amount),
                CallbackQueryHandler(btn),
            ],
            WAIT_CONFIRM_SEND: [
                CallbackQueryHandler(btn),
            ],
            WAIT_USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_wait_user_id),
                CallbackQueryHandler(btn),
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        per_message=False,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("adduser",    cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(conv)
    app.add_error_handler(err_handler)

    print("═" * 54)
    print("  🤖  بوت جيب — النظام المتكامل للمدفوعات  ")
    print("═" * 54)
    print(f"  ✅  Admin ID   : {ADMIN_ID}")
    print(f"  ✅  Phone      : {PHONE}")
    print(f"  ✅  DB         : {DB_FILE}")
    print(f"  ✅  Log        : jaib_bot.log")
    print("═" * 54)
    print("  💸  السحب/الإرسال        : ✅")
    print("  💰  عرض الرصيد           : ✅")
    print("  🔍  التحقق من الدفع      : ✅")
    print("  👥  إدارة المستخدمين     : ✅")
    print("  🚫  منع تكرار العمليات   : ✅")
    print("  🔔  إشعارات الأدمن       : ✅")
    print("═" * 54)
    print("  🟢  البوت يعمل...  Ctrl+C للإيقاف")
    print("═" * 54)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

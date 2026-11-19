# نشر المشروع على VPS

## متطلبات V18.1 قبل النشر

- استخدم Python 3.11 أو أحدث وثبّت `requirements.txt` كما هو.
- اضبط `JAIB_API_UTC_OFFSET_HOURS=3` ما دامت أوقات Jaib بتوقيت اليمن.
- اترك شبكات USDT الجديدة متوقفة حتى اختبار العنوان والحد الأدنى والرسم على حساب Binance محدود الصلاحية.
- عيّن قناة المستخدمين وقناة الأدمن وقناة النسخ الاحتياطية كلًا على حدة، واجعل البوت مشرفًا بصلاحية نشر الرسائل في كل قناة مستخدمة.
- تأكد أن `JAIB_HEALTH_CHECK_INTERVAL_SECONDS` مناسب للخادم؛ القيمة الافتراضية 60 ثانية والحد الأدنى 30 ثانية.
- استخدم نطاق HTTPS ثابتًا لـMini App؛ لا تستخدم وسيطًا يغير Origin أو `initData`.
- شغّل `python verify_project.py` قبل مفاتيح الإنتاج.

## 1. تجهيز المستخدم والمجلد

```bash
sudo adduser --system --group bnwallet
sudo mkdir -p /opt/bn-wallet
sudo chown -R bnwallet:bnwallet /opt/bn-wallet
```

انسخ المشروع إلى `/opt/bn-wallet` ثم:

```bash
sudo -u bnwallet python3 -m venv /opt/bn-wallet/.venv
sudo -u bnwallet /opt/bn-wallet/.venv/bin/python -m pip install --upgrade pip
sudo -u bnwallet /opt/bn-wallet/.venv/bin/python -m pip install -r /opt/bn-wallet/requirements.txt
sudo -u bnwallet cp /opt/bn-wallet/.env.example /opt/bn-wallet/.env
sudo chmod 600 /opt/bn-wallet/.env
```

## 2. خدمة البوت

أنشئ `/etc/systemd/system/bn-wallet-bot.service`:

```ini
[Unit]
Description=BN Wallet Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bnwallet
Group=bnwallet
WorkingDirectory=/opt/bn-wallet
ExecStart=/opt/bn-wallet/.venv/bin/python /opt/bn-wallet/run.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

ثم:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bn-wallet-bot
sudo systemctl status bn-wallet-bot
```

## 3. خدمة Mini App

أنشئ `/etc/systemd/system/bn-wallet-webapp.service`:

```ini
[Unit]
Description=BN Wallet Telegram Mini App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bnwallet
Group=bnwallet
WorkingDirectory=/opt/bn-wallet
ExecStart=/opt/bn-wallet/.venv/bin/python /opt/bn-wallet/webapp_main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

استخدم Nginx وHTTPS أمام `127.0.0.1:8080`، واضبط `MINI_APP_URL` على الرابط العام.

## 4. التحديث الآمن

1. عطّل البوت مؤقتًا.
2. أنشئ نسخة احتياطية واختبر وجودها.
3. احتفظ بـ `.env` وقاعدة البيانات.
4. استبدل ملفات المصدر فقط.
5. نفّذ `python -m pip install -r requirements.txt`.
6. نفّذ `python verify_project.py`.
7. شغّل الخدمة وراجع السجل من لوحة الأدمن ومن `journalctl` أول مرة.

```bash
sudo systemctl stop bn-wallet-bot
sudo -u bnwallet /opt/bn-wallet/.venv/bin/python /opt/bn-wallet/verify_project.py
sudo systemctl start bn-wallet-bot
sudo journalctl -u bn-wallet-bot -n 100 --no-pager
```

## 5. الحماية

- اجعل `.env` بصلاحية `600`.
- لا تفتح منفذ SQLite أو تنسخ قاعدة البيانات إلى مسار عام.
- اسمح بالوصول إلى Mini App عبر HTTPS فقط.
- قيّد SSH واستخدم Firewall.
- لا تمنح قناة النسخ إلا للأدمن والمستخدمين الموثوقين.
- استخدم مفاتيح Binance بصلاحية السحب المطلوبة فقط وقيود IP عندما تكون متاحة.

## 8. تشغيل الخدمتين بوحدة واحدة

يمكن استخدام Supervisor أو systemd لتشغيل `run_all.py` بدل خدمتين منفصلتين:

```ini
[Service]
WorkingDirectory=/opt/bn-wallet
ExecStart=/opt/bn-wallet/.venv/bin/python /opt/bn-wallet/run_all.py
Restart=always
RestartSec=5
```

إذا خرج البوت أو خادم الويب، يوقف المشغل الخدمة الأخرى حتى يعيد systemd تشغيل المجموعة بحالة متناسقة.

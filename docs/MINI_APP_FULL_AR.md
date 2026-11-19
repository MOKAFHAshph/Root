# Mini App الكاملة

## المبدأ

البوت والـMini App واجهتان لخدمات واحدة. لا يُسمح بوضع حساب الرصيد أو التحقق من الدفع أو السحب داخل JavaScript؛ الواجهة تستدعي FastAPI، والخادم يستدعي خدمات المحفظة نفسها التي يستخدمها البوت.

## المصادقة

كل طلب يرسل `X-Telegram-Init-Data`. يتحقق الخادم من:

- توقيع HMAC وفق Bot Token.
- عمر `auth_date`.
- هوية Telegram.
- حالة المستخدم أو صلاحية الأدمن.

لا يعتمد الخادم على `telegram_id` يرسله المتصفح.

## منع التكرار

تُصدر مرحلتا تجهيز الإيداع وتسعير السحب Token موقعًا يحتوي على:

- نوع الإجراء.
- Telegram ID.
- وقت الإصدار.
- Nonce.
- البيانات التي تم تسعيرها.

عند التنفيذ يُسجل الـNonce في `webapp_action_tokens`. محاولة إعادة استخدامه تعيد HTTP 409.

## الإيداع

```text
ضغط زر الإيداع
    ↓
مسودة بلا سجل مالي
    ↓
إرسال رقم العملية
    ↓
إنشاء سجل WAITING_REFERENCE
    ↓
التحقق من جيب
    ├─ COMPLETED
    ├─ REJECTED
    ├─ PAYMENT_REVIEW
    └─ RETRY_REQUIRED
```

المسودة لا تظهر في التقارير. `PAYMENT_REVIEW` و`RETRY_REQUIRED` لا يدخلان إحصائيات النجاح أو الفشل النهائي.

## السحب

```text
Quote موقّع
    ↓
تأكيد أحادي الاستخدام
    ↓
RESERVED
    ↓
PROCESSING
    ├─ SUBMITTED → COMPLETED
    ├─ MANUAL_REVIEW
    └─ FAILED_RELEASED
```

## API المستخدم

- `GET /api/bootstrap`
- `POST /api/access/request`
- `GET /api/me/stats`
- `GET /api/me/operations`
- `GET /api/me/referrals`
- `GET /api/me/deposits/prepare`
- `POST /api/me/deposits/verify`
- `GET /api/me/withdrawals/networks`
- `POST /api/me/withdrawals/quote`
- `POST /api/me/withdrawals`
- `GET /api/me/withdrawals/{id}`
- `GET /api/me/report`

## API الإدارة

- `GET /api/admin/dashboard`
- `GET /api/admin/users`
- `GET /api/admin/users/{telegram_id}`
- `PATCH /api/admin/users/{telegram_id}/status`
- `POST /api/admin/users/{telegram_id}/balance-adjustment`
- `GET /api/admin/deposits`
- `POST /api/admin/deposits/{id}/approve`
- `POST /api/admin/deposits/{id}/reject`
- `GET /api/admin/withdrawals`
- `POST /api/admin/withdrawals/{id}/retry`
- `POST /api/admin/withdrawals/{id}/release`
- `GET/PATCH /api/admin/settings`
- `POST /api/admin/broadcasts`
- `POST /api/admin/backups`
- `GET /api/admin/audit-log`

## API إدارة النظام الإضافية

- `POST/PATCH/DELETE /api/admin/customers...`
- `GET/POST/PATCH/DELETE /api/admin/admins...`
- `GET/POST/DELETE /api/admin/required-channels...`
- `GET /api/admin/security-log`
- `GET /api/admin/export/customers.csv`
- `POST /api/admin/backups/restore`
- `GET/PATCH /api/admin/logs`
- `GET /api/admin/logs/download`
- `POST /api/admin/logs/truncate`

## الوصول للحسابات الجديدة

`GET /api/bootstrap` متاح لكل مستخدم يملك `initData` صحيحًا. إذا لم يكن الحساب مفعلاً يعيد بيانات Landing Page فقط، ولا يعيد خطأ صلاحيات. يمكن للمستخدم إرسال طلب عبر:

- `POST /api/access/request`

تُنشأ/تُحدّث حالة العميل كحساب معلق، ويصل إشعار للإدارة. لا تفتح الخدمات المالية إلا بعد التفعيل.

## السيولة وحد العملية

- تعرض الواجهة `liquidity.label` و`liquidity.available` دون اسم المزود.
- تخصم الخدمة احتياطي الأمان قبل العرض.
- عند Quote السحب يُضاف رسم الشبكة الداخلي إلى المبلغ المطلوب عند فحص السيولة.
- `withdrawal_policy.operation_limit_enabled` يحدد هل يظهر ويطبق حد خاص لكل عملية.
- كل الإعدادات الحساسة تتحقق في الخادم، ولا يعتمد التنفيذ على قيمة JavaScript.

## تجربة الإيداع والنتائج

تعرض شاشة الإيداع أرقام المستفيد واسم المستفيد البديل وسياسة الطرف الثالث. تتوفر أزرار نسخ للمعلومات والمرجع. بعد التحقق تظهر شاشة نتيجة مستقلة للنجاح أو الفشل أو المراجعة بدل رسالة صغيرة فقط.

## الإحالة عند التفعيل

تسجيل الإحالة يبقى `PENDING`. عند انتقال العميل من حالة غير نشطة إلى `ACTIVE`، سواء من البوت أو Mini App، تُنفذ معاملة SQLite واحدة تشمل تحديث الإحالة وإضافة الرصيد وLedger. إعادة التفعيل لا تصرف مكافأة إضافية.

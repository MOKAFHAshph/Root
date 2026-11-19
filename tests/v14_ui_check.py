#!/usr/bin/env python3
"""فحوصات ثابتة لتحديثات واجهة V14 في التطبيق المصغر."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / "bot/webapp/static/index.html").read_text(encoding="utf-8")

required = [
    'class="money-ltr"',
    'function showLoading(',
    'result-icon loading',
    '$("resultClose").hidden = String(primaryText).trim() === "إغلاق"',
    'حسابك غير مفعّل. يرجى التسجيل وتقديم طلب.',
    'حسابك غير مفعّل. يرجى استكمال بقية البيانات لتفعيل حسابك.',
    'حسابك لا يزال قيد المراجعة. يرجى التحلي بالصبر، سيتم تفعيله قريباً.',
    'تم تقديم طلبك. حسابك الآن قيد المراجعة.',
    'المبلغ الذي ستقوم بتحويله للتحقق من حسابك سيتم توريده إلى رصيدك فور تفعيل الحساب.',
    'moneyHtml(recipient.amount_yer || 0, "YER")',
    'rateHtml(recipient.exchange_rate_yer || state.bootstrap?.rate || 0)',
]
for item in required:
    assert item in html, f"العنصر المطلوب غير موجود: {item}"

assert "قيمة تحويل التحقق ثابتة حاليًا: 550.00 YER" not in html
# الهوية الحالية تستخدم اسم «جيب/Jaib» صراحةً في واجهة الإيداع.
assert "إيداع جيب" in html
assert "Jeeb" not in html

print("PASS: V14 loading, one-close-button flow, account messages, Jaib terminology, and LTR money blocks")

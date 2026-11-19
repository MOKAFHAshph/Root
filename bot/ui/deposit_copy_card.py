"""Compact Jaib deposit presentation and native Telegram clipboard button.

Presentation only: never creates, approves, or changes any payment/ledger entries.
"""
from __future__ import annotations

from html import escape
import re

_DEFAULT_TITLE = 'إيداع عبر محفظة جيب'
_DEFAULT_INSTRUCTIONS = 'حوّل من محفظتك الشخصية ثم أرسل رقم العملية الصحيح فقط.'
_DEFAULT_POLICY = ('التحويل من طرف ثالث غير مقبول. يجب أن يكون المرسل صاحب الحساب '
                   'الشخصي المطابق لبيانات العميل.')


def _str(value: object) -> str:
    return str(value or '').strip()


def phone_to_copy(secondary: object, primary: object) -> str:
    """Give preference to the explicitly supplied alternate receiving account."""
    return _str(secondary) or _str(primary)


def compact_html(*, title: object, instructions: object, policy: object,
                 rate: object, minimum: object, beneficiary: object,
                 phone: object, title_icon_html: str = '💳') -> str:
    """The phone belongs in the copy button below, not duplicated in message text."""
    title = escape(_str(title) or _DEFAULT_TITLE)
    instructions = escape(_str(instructions) or _DEFAULT_INSTRUCTIONS)
    policy = escape(_str(policy) or _DEFAULT_POLICY)
    rate = escape(_str(rate))
    minimum = escape(_str(minimum))
    beneficiary = escape(_str(beneficiary))
    lines = [
        f'{title_icon_html} <b>{title}</b>',
        instructions,
        '',
    ]
    if _str(phone):
        if beneficiary:
            lines.append(f'👤 المستفيد: <b>{beneficiary}</b>')
        lines.append('📋 <b>رقم الاستقبال على زر النسخ أدناه</b>')
    else:
        lines.append('⚠️ رقم الاستقبال غير مهيأ؛ لا تحوّل قبل التواصل مع الإدارة.')
    lines.extend([
        '',
        f'💱 <b>1 USDT = {rate} ريال يمني</b>',
        f'📌 أقل إيداع: <b>{minimum} ريالات يمنية</b> — دون كسور.',
        '',
        '<b>⚠️ سياسة منع الطرف الثالث</b>',
        policy,
        '',
        '🧾 <b>بعد التحويل: أرسل رقم العملية فقط.</b>',
    ])
    return '\n'.join(lines)


def copy_button(phone: object, *, emoji_id: object = '5897909375028172264'):
    """Native Telegram copy button, styled red, Jaib custom emoji if allowed.

    Import lazily; this module can be tested without Telegram installed. The
    app's existing presentation fallback strips unsupported styling/emoji while
    leaving the copy_text action intact.
    """
    from telegram import InlineKeyboardButton, CopyTextButton

    value = _str(phone)
    if not value:
        raise ValueError('RECEIVER_PHONE_MISSING')
    # Display only trusted configured digits; no accidental control characters.
    if not re.fullmatch(r'[+]?[0-9]{5,20}', value):
        raise ValueError('RECEIVER_PHONE_INVALID')
    kwargs = {
        'text': f'📋 {value}  |  نسخ رقم جيب',
        'copy_text': CopyTextButton(text=value),
        'style': 'danger',
    }
    emoji_id = _str(emoji_id)
    if re.fullmatch(r'[0-9]{10,25}', emoji_id):
        kwargs['icon_custom_emoji_id'] = emoji_id
    return InlineKeyboardButton(**kwargs)

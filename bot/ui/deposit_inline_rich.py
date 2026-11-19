"""Compact Jaib deposit screen with an IN-MESSAGE Telegram rich copy button.

Presentation only. NEVER changes ledger, validation, claims, financial state,
external wallet requests, payment ownership or transaction verification.
"""
from __future__ import annotations

from html import escape
import re

TITLE = 'إيداع عبر محفظة جيب'
INSTRUCTIONS = 'حوّل المبلغ من محفظتك الشخصية المسجلة ثم أرسل رقم العملية الصحيح فقط.'
POLICY = 'يُمنع التحويل من طرف ثالث. يجب أن يكون المرسل صاحب الحساب الشخصي المطابق لبيانات العميل.'
ICON_ID = '5897909375028172264'


def clean(value):
    return str(value or '').strip()


def phone_to_copy(secondary, primary):
    return clean(secondary) or clean(primary)


def valid_phone(value):
    return bool(re.fullmatch(r'\+?[0-9]{5,20}', clean(value)))


def valid_icon(value):
    return bool(re.fullmatch(r'[0-9]{10,25}', clean(value)))


def icon_text(icon_id, fallback='💳'):
    if valid_icon(icon_id):
        return {'type': 'custom_emoji', 'custom_emoji_id': clean(icon_id),
                'alternative_text': fallback}
    return fallback


def rich_card(*, title, instructions, policy, rate, minimum, beneficiary,
              phone, icon_id=ICON_ID, use_custom_emoji=True):
    """Structured rich message; Telegram renders native centered copy button in BODY."""
    value = clean(phone)
    if not valid_phone(value):
        raise ValueError('RECEIVER_PHONE_INVALID')
    icon = icon_text(icon_id) if use_custom_emoji else '💳'
    name = clean(beneficiary)
    head = [icon, ' ' + (clean(title) or TITLE)]
    body = [
        {'type': 'paragraph', 'text': head},
        {'type': 'paragraph', 'text': clean(instructions) or INSTRUCTIONS},
    ]
    if name:
        body.append({'type': 'paragraph', 'text': '👤 المستفيد: ' + name})
    body.extend([
        {'type': 'buttons', 'align': 'center', 'buttons': [{
            'text': [icon, '  ' + value],
            'style': 'danger',
            'copy_text': {'text': value},
        }]},
        {'type': 'paragraph', 'text':
            '💱 1 USDT = ' + clean(rate) + ' ريال يمني\n'
            '📌 أقل إيداع: ' + clean(minimum) + ' ريالات (دون كسور)'},
        {'type': 'blockquote', 'blocks': [
            {'type': 'paragraph', 'text': '⚠️ سياسة منع الطرف الثالث\n' + (clean(policy) or POLICY)}
        ]},
        {'type': 'paragraph', 'text': '🧾 بعد التحويل أرسل رقم العملية فقط.'},
    ])
    return {'blocks': body, 'is_rtl': True}


def fallback_html(*, title, instructions, policy, rate, minimum,
                  beneficiary, phone, icon_html='💳'):
    """Compact HTML + regular copy keyboard, used only if Rich API rejects."""
    name = clean(beneficiary)
    lines = [
        icon_html + ' <b>' + escape(clean(title) or TITLE) + '</b>',
        escape(clean(instructions) or INSTRUCTIONS),
    ]
    if name:
        lines.append('👤 المستفيد: <b>' + escape(name) + '</b>')
    lines.extend([
        '📋 اضغط الزر الأحمر لنسخ رقم جيب:',
        '💱 <b>1 USDT = ' + escape(clean(rate)) + ' ريال يمني</b>',
        '📌 أقل إيداع: <b>' + escape(clean(minimum)) + ' ريالات</b> (دون كسور)',
        '<b>⚠️ سياسة منع الطرف الثالث</b>',
        escape(clean(policy) or POLICY),
        '🧾 أرسل رقم العملية الصحيح فقط.',
    ])
    return '\n'.join(lines)


def keyboard_copy_button(phone, icon_id=ICON_ID):
    """Copy button for classic fallback only (not used on rich success)."""
    from telegram import InlineKeyboardButton, CopyTextButton
    value = clean(phone)
    if not valid_phone(value):
        raise ValueError('RECEIVER_PHONE_INVALID')
    kwargs = {'text': '📋 ' + value + ' | نسخ',
              'copy_text': CopyTextButton(text=value), 'style': 'danger'}
    if valid_icon(icon_id):
        kwargs['icon_custom_emoji_id'] = clean(icon_id)
    return InlineKeyboardButton(**kwargs)

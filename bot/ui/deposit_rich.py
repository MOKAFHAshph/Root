"""Rich Jaib deposit presentation, using the same Telegram rich_message blocks as the main menu.

Display-only module. No transactions, keys, persistence, network calls, or ledgers here.
"""
from __future__ import annotations
from html import escape
import re


def _s(value: object) -> str:
    return str(value or '').strip()


def _rich_icon(emoji_id: str, alternative: str):
    """Telegram-rich text custom emoji; fallback used if the rich API rejects it."""
    if re.fullmatch(r"[0-9]{10,25}", _s(emoji_id)):
        return {'type': 'custom_emoji', 'custom_emoji_id': _s(emoji_id),
                'alternative_text': (_s(alternative) or '💳')}
    return _s(alternative) or '💳'


def build_deposit_rich(*, title, instructions, policy, rate, minimum,
                       primary, secondary, secondary_name, emoji_id='', fallback='💳',
                       custom_emoji=True):
    """Return the heading/table/blockquote schema used by menu_view.render_rich."""
    title = _s(title) or 'إيداع عبر محفظة جيب'
    instructions = _s(instructions) or 'حوّل من محفظتك الشخصية ثم أرسل رقم العملية الصحيح فقط.'
    policy = _s(policy) or 'لا تقبل التحويلات من طرف ثالث.'
    primary, secondary, secondary_name = _s(primary), _s(secondary), _s(secondary_name)
    rate, minimum = _s(rate), _s(minimum)
    main_number = primary or secondary
    main_label = 'الرقم الأساسي' if primary else 'الرقم البديل'
    main_name = secondary_name if secondary and main_number == secondary else ''
    headline = ([(_rich_icon(emoji_id, fallback) if custom_emoji else (_s(fallback) or '💳')),
                '  ' + title])
    blocks = [
        {'type': 'heading', 'text': headline, 'size': 1},
        {'type': 'paragraph', 'text': instructions},
        {'type': 'divider'},
        {'type': 'heading', 'text': '📥 بيانات محفظة الاستقبال', 'size': 3},
    ]
    if main_number:
        blocks += [
            {'type': 'paragraph', 'text': main_label + ' — انسخ الرقم وحوّل إليه'},
            # Big heading as in the bot's rich main-menu section headings.
            {'type': 'heading', 'text': main_number, 'size': 1},
        ]
        if main_name:
            blocks.append({'type': 'paragraph', 'text': '👤 اسم المستفيد: ' + main_name})
        if primary and secondary and primary != secondary:
            blocks += [
                {'type': 'paragraph', 'text': 'رقم استقبال إضافي:'},
                {'type': 'table', 'is_compact': True, 'cells': [
                    [{'text': 'الحساب', 'is_header': True}, {'text': 'الرقم', 'is_header': True}],
                    [{'text': 'البديل'}, {'text': secondary}],
                ]},
            ]
            if secondary_name:
                blocks.append({'type': 'paragraph', 'text': '👤 مستفيد الرقم البديل: ' + secondary_name})
    else:
        blocks.append({'type': 'paragraph', 'text': '⚠️ رقم الاستقبال غير مهيأ، تواصل مع الإدارة قبل التحويل.'})
    blocks += [
        {'type': 'divider'},
        {'type': 'heading', 'text': '💱 سعر الصرف وحد الإيداع', 'size': 3},
        {'type': 'table', 'is_compact': True, 'cells': [
            [{'text': 'البيان', 'is_header': True}, {'text': 'القيمة', 'is_header': True}],
            [{'text': 'سعر 1 USDT'}, {'text': rate + ' ريال يمني'}],
            [{'text': 'أقل إيداع'}, {'text': minimum + ' ريالات يمنية'}],
        ]},
        {'type': 'paragraph', 'text': 'تُقبل مبالغ الريال الصحيحة فقط، دون كسور عشرية.'},
        {'type': 'divider'},
        {'type': 'heading', 'text': '⚠️ سياسة منع التحويل من طرف ثالث', 'size': 3},
        {'type': 'blockquote', 'blocks': [{'type': 'paragraph', 'text': policy}]},
        {'type': 'divider'},
        {'type': 'heading', 'text': '🧾 بعد التحويل: أرسل رقم العملية فقط', 'size': 3},
        {'type': 'paragraph', 'text': 'سيبدأ التحقق من العملية فور استلام رقمها. لا تكرر الرقم أثناء الفحص.'},
    ]
    return {'blocks': blocks, 'is_rtl': True}


def build_deposit_html(*, title, instructions, policy, rate, minimum,
                       primary, secondary, secondary_name, emoji_id='', fallback='💳'):
    """Fallback for Telegram clients/bots not supporting editMessageText.rich_message."""
    esc = escape
    title, instructions, policy = esc(_s(title)), esc(_s(instructions)), esc(_s(policy))
    primary, secondary = _s(primary), _s(secondary)
    main_number = primary or secondary
    main_label = 'الرقم الأساسي' if primary else 'الرقم البديل'
    icon = esc(_s(fallback) or '💳')
    if re.fullmatch(r'[0-9]{10,25}', _s(emoji_id)):
        icon = '<tg-emoji emoji-id="' + _s(emoji_id) + '">' + icon + '</tg-emoji>'
    lines = [icon+' <b>'+title+'</b>', '', instructions, '',
             '<b>📥 بيانات محفظة الاستقبال</b>']
    if main_number:
        lines.append('<blockquote>📱 <b>'+main_label+'</b>\n<code>'+esc(main_number)+'</code>'+(
            '\n👤 <b>اسم المستفيد:</b> '+esc(_s(secondary_name)) if not primary and secondary_name else '')+'</blockquote>')
        if primary and secondary and primary != secondary:
            lines.append('<blockquote>📱 <b>الرقم البديل</b>\n<code>'+esc(secondary)+'</code>'+(
                '\n👤 <b>اسم المستفيد:</b> '+esc(_s(secondary_name)) if secondary_name else '')+'</blockquote>')
    else:
        lines.append('⚠️ رقم الاستقبال غير مهيأ. تواصل مع الإدارة قبل التحويل.')
    lines += ['', '<b>💱 سعر الصرف والحد الأدنى</b>',
        '<blockquote><b>1 USDT = '+esc(_s(rate))+' ريال يمني</b>\n'+
        '📌 أقل إيداع: <b>'+esc(_s(minimum))+' ريالات يمنية</b></blockquote>',
        '<i>مبالغ الريال الصحيحة فقط، دون كسور عشرية.</i>', '',
        '<b>⚠️ سياسة منع التحويل من طرف ثالث</b>',
        '<blockquote>'+policy+'</blockquote>', '',
        '<b>🧾 بعد التحويل: أرسل رقم العملية فقط.</b>',
        '<i>لا تكرر الرقم أثناء الفحص.</i>']
    return '\n'.join(lines)

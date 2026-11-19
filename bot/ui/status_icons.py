"""Configurable HTML custom-emoji icons for wallet status messages.

Presentation only; never imported by accounting or monetary validation.
"""
from __future__ import annotations
from html import escape


def wallet_status_icon(name: str) -> str:
    from bot.ui.menu_view import STATUS_ICON_DEFAULTS, valid_emoji_id
    value = STATUS_ICON_DEFAULTS.get(name)
    if value is None:
        return '⏳'
    try:
        from bot.services.menu_customization import menu_settings
        cfg = menu_settings()
        if isinstance(cfg, dict):
            override = cfg.get('status_icons', {}).get(name, {})
            if isinstance(override, dict):
                value = {'emoji_id': override.get('emoji_id', value['emoji_id']),
                         'fallback': override.get('fallback', value['fallback'])}
            if not cfg.get('rich_enabled', True):
                return escape(str(value['fallback']))
    except Exception:
        pass
    emoji_id = str(value.get('emoji_id', ''))
    fallback = escape(str(value.get('fallback', '⏳')))
    if valid_emoji_id(emoji_id) and emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def wallet_display_text(name: str, default: str) -> str:
    from bot.ui.menu_view import TEXT_DEFAULTS
    try:
        from bot.services.menu_customization import menu_settings
        text = menu_settings().get('texts', {}).get(name, default)
        if isinstance(text, str) and text.strip():
            return escape(text)
    except Exception:
        pass
    return escape(default)


def wallet_withdraw_icon(network: str, *, completed: bool = False) -> str:
    network = str(network or '').upper()
    is_ton = network in ('TON', 'TON_USDT', 'TON_NATIVE')
    name = ('success_' if completed else 'withdraw_wait_') + ('ton' if is_ton else 'bep20')
    return wallet_status_icon(name)

"""Trusted-customer UI extension.

"""
from __future__ import annotations

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

# ---------------- واجهة العميل الموثوق ----------------

_vnext_customer_detail_text = customer_detail_text
_vnext_customer_actions_kb = customer_actions_kb


def customer_detail_text(row: sqlite3.Row) -> str:
    trusted = bool(row["is_trusted"]) if "is_trusted" in row.keys() else False
    return _vnext_customer_detail_text(row) + f"\n⭐ عميل موثوق: <b>{'نعم' if trusted else 'لا'}</b>"


def customer_actions_kb(row: sqlite3.Row, admin_id: int) -> InlineKeyboardMarkup:
    base = _vnext_customer_actions_kb(row, admin_id)
    rows = [list(item) for item in base.inline_keyboard]
    permissions = DBX.get_admin_permissions(admin_id)
    if "customers_edit" in permissions:
        trusted = bool(row["is_trusted"]) if "is_trusted" in row.keys() else False
        button = btn(
            render_bot_button('bot.ui.trusted.py:26:button','{v0}', [str('⭐ إلغاء الثقة' if trusted else '☆ تعيين كعميل موثوق')]),
            f"cust_trusted_toggle:{row['id']}",
            "danger" if trusted else "success",
        )
        insert_at = max(0, len(rows) - 1)
        rows.insert(insert_at, [button])
    return InlineKeyboardMarkup(rows)




_export_globals(globals())

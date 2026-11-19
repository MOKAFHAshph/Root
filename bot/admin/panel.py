"""Stable import surface for administration UI."""
from bot.app import runtime

admin_kb = runtime.get("admin_kb")
wallet_admin_panel_kb = runtime.get("wallet_admin_panel_kb")
wallet_admin_settings_kb = runtime.get("wallet_admin_settings_kb")

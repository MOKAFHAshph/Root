#!/usr/bin/env python3
"""Offline unit checks for Telegram backup scheduling, retention, and restore.

The test stubs Telegram only inside the test process. Production still uses the
real python-telegram-bot dependency from requirements.txt.
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, api_kwargs=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.api_kwargs = api_kwargs
        self.kwargs = kwargs


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = tuple(tuple(row) for row in inline_keyboard)


class Update:
    pass


class TelegramError(Exception):
    pass


class BadRequest(TelegramError):
    pass


class Forbidden(TelegramError):
    pass


def install_telegram_stubs() -> None:
    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.Update = Update

    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = SimpleNamespace(HTML="HTML")
    constants.ChatType = SimpleNamespace(CHANNEL="channel")
    constants.ChatMemberStatus = SimpleNamespace(OWNER="creator", ADMINISTRATOR="administrator")

    errors = types.ModuleType("telegram.error")
    errors.TelegramError = TelegramError
    errors.BadRequest = BadRequest
    errors.Forbidden = Forbidden

    ext = types.ModuleType("telegram.ext")
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)

    sys.modules["telegram"] = telegram
    sys.modules["telegram.constants"] = constants
    sys.modules["telegram.error"] = errors
    sys.modules["telegram.ext"] = ext


class FakeDB:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings_v2(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, details TEXT)"
            )
            con.execute("CREATE TABLE IF NOT EXISTS sample_data(id INTEGER PRIMARY KEY, value TEXT)")
            con.commit()

    def connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def get_setting(self, key):
        with self.connect() as con:
            row = con.execute("SELECT value FROM settings_v2 WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key, value):
        with self.connect() as con:
            con.execute(
                "INSERT INTO settings_v2(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            con.commit()

    def audit(self, actor_tg_id, actor_name, action, entity_type, entity_id, details=""):
        with self.connect() as con:
            con.execute("INSERT INTO audit_logs(action,details) VALUES(?,?)", (action, details))
            con.commit()

    def is_admin(self, telegram_id):
        return int(telegram_id) == 1

    def has_permission(self, telegram_id, permission):
        return self.is_admin(telegram_id)

    def export_snapshot(self):
        with self.connect() as con:
            settings = [dict(row) for row in con.execute("SELECT * FROM settings_v2 ORDER BY key")]
            sample = [dict(row) for row in con.execute("SELECT * FROM sample_data ORDER BY id")]
        return {"settings_v2": settings, "customers": [], "orders_v2": [], "sample_data": sample}

    def import_snapshot(self, payload):
        with self.connect() as con:
            con.execute("DELETE FROM settings_v2")
            con.execute("DELETE FROM sample_data")
            for row in payload.get("settings_v2", []):
                con.execute(
                    "INSERT INTO settings_v2(key,value,updated_at) VALUES(?,?,?)",
                    (row["key"], row["value"], row.get("updated_at", "")),
                )
            for row in payload.get("sample_data", []):
                con.execute("INSERT INTO sample_data(id,value) VALUES(?,?)", (row["id"], row["value"]))
            con.commit()


class FakeDocument:
    def __init__(self, file_id, filename, size):
        self.file_id = file_id
        self.file_unique_id = f"unique-{file_id}"
        self.file_name = filename
        self.file_size = size


class FakeMessage:
    def __init__(self, message_id, channel_id, document):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=channel_id)
        self.document = document


class FakeRemoteFile:
    def __init__(self, source: Path):
        self.source = source

    async def download_to_drive(self, custom_path):
        shutil.copyfile(self.source, custom_path)
        return custom_path


class FakeBot:
    def __init__(self, channel_id: int):
        self.id = 999
        self.channel_id = channel_id
        self.sent = []
        self.deleted = []
        self.files = {}
        self.admin_messages = []

    async def send_document(self, chat_id, document, filename, **kwargs):
        assert int(chat_id) == self.channel_id
        content = document.read()
        message_id = len(self.sent) + 1
        file_id = f"file-{message_id}"
        stored = Path(document.name)
        self.files[file_id] = stored
        msg = FakeMessage(message_id, self.channel_id, FakeDocument(file_id, filename, len(content)))
        self.sent.append(msg)
        return msg

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((int(chat_id), int(message_id)))
        return True

    async def get_file(self, file_id):
        return FakeRemoteFile(self.files[file_id])

    async def send_message(self, chat_id, text, **kwargs):
        self.admin_messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=1)


async def noop(*args, **kwargs):
    return None


def main() -> None:
    install_telegram_stubs()
    with tempfile.TemporaryDirectory(prefix="bn-wallet-v6-backup-") as temp:
        root = Path(temp)
        backup_dir = root / "backups"
        backup_dir.mkdir()
        dbx = FakeDB(root / "test.db")

        from bot import runtime

        runtime._REGISTRY.clear()
        runtime._MODULE_GLOBALS.clear()

        def env(name, default=None, required=False):
            value = os.getenv(name, default)
            if required and not value:
                raise RuntimeError(name)
            return value or ""

        def now_str():
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def create_backup_file():
            path = backup_dir / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
            path.write_text(json.dumps(dbx.export_snapshot(), ensure_ascii=False), encoding="utf-8")
            return path

        def btn(text, callback_data, style=None):
            return InlineKeyboardButton(text, callback_data=callback_data, api_kwargs={"style": style} if style else None)

        def admin_kb(admin_id=1):
            return InlineKeyboardMarkup([[btn("home", "main_menu")]])

        def set_state(context, state, **payload):
            context.user_data["state"] = state
            context.user_data["payload"] = payload

        runtime._REGISTRY.update(
            {
                "env": env,
                "DBX": dbx,
                "DB": FakeDB,
                "BACKUP_DIR": backup_dir,
                "ADMIN_ID": 1,
                "ADMIN_PERMISSION_LABELS": {
                    "backup_create": "create",
                    "backup_import": "import",
                },
                "ALL_ADMIN_PERMISSIONS": ("backup_create", "backup_import"),
                "admin_kb": admin_kb,
                "callback_permission": lambda data: None,
                "state_permission": lambda state: None,
                "on_callback": noop,
                "on_message": noop,
                "post_init": noop,
                "create_backup_file": create_backup_file,
                "btn": btn,
                "html": html_module.escape,
                "now_str": now_str,
                "set_state": set_state,
                "actor_name": lambda user: "Test Admin",
                "log": logging.getLogger("v6-backup-test"),
                "Application": object,
                "sqlite3": sqlite3,
            }
        )

        import importlib

        module = importlib.import_module("bot.services.backup_automation")

        assert module.backup_mode() == "MANUAL"
        assert module.backup_interval_seconds() == 86400
        assert module.backup_max_copies() == 100
        assert "backup_manage" in module.ALL_ADMIN_PERMISSIONS

        channel_id = -1001234567890
        dbx.set_setting("backup_channel_id", channel_id)
        dbx.set_setting("backup_channel_title", "Private Backups")
        dbx.set_setting("backup_max_copies", "2")
        bot = FakeBot(channel_id)
        context = SimpleNamespace(bot=bot, user_data={})

        asyncio.run(module.create_and_send_channel_backup(context, source="MANUAL", actor_id=1))
        asyncio.run(module.create_and_send_channel_backup(context, source="AUTO", actor_id=None))
        asyncio.run(module.create_and_send_channel_backup(context, source="AUTO", actor_id=None))
        assert len(bot.sent) == 3
        assert bot.deleted == [(channel_id, 1)]
        assert module._tracked_backup_count(channel_id) == 2

        snapshot = dbx.export_snapshot()
        assert "backup_channel_files" in snapshot
        assert len(snapshot["backup_channel_files"]) == 3

        # Restore uses the existing snapshot functions and creates a local safety copy.
        dbx.set_setting("marker", "before")
        restore_payload = dbx.export_snapshot()
        for row in restore_payload["settings_v2"]:
            if row["key"] == "marker":
                row["value"] = "restored"
        restore_path = backup_dir / "manual_restore.json"
        restore_path.write_text(json.dumps(restore_payload), encoding="utf-8")
        safety = module._restore_from_path(restore_path)
        assert safety.exists()
        assert dbx.get_setting("marker") == "restored"
        # Current tracked messages are merged, not erased by an older restore.
        assert module._tracked_backup_count(channel_id) == 2

        # Automatic scheduler creates a due backup and advances next_run_at.
        dbx.set_setting("backup_mode", "AUTO")
        dbx.set_setting("backup_interval_seconds", "3600")
        dbx.set_setting("backup_next_run_at", (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"))
        before = len(bot.sent)
        asyncio.run(module.backup_scheduler_tick(context))
        assert len(bot.sent) == before + 1
        next_run = datetime.strptime(dbx.get_setting("backup_next_run_at"), "%Y-%m-%d %H:%M:%S")
        assert next_run > datetime.now()

        print("PASS: backup settings, channel send, retention, restore, and scheduler")


if __name__ == "__main__":
    main()

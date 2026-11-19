#!/usr/bin/env python3
"""Offline integration checks for the final consolidated project.

The test stubs only third-party packages unavailable in the build container. It
loads the real project modules, creates a real temporary SQLite database, and
exercises registration, referrals, amount-free deposits, restore, and logging.
"""
from __future__ import annotations

import asyncio
import builtins
import importlib
import logging
import os
import symtable
import sys
import tempfile
import types
import hashlib
import hmac
import json
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DummyObject:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.text = kwargs.get("text", args[0] if args else None)
        self.callback_data = kwargs.get("callback_data")
        self.url = kwargs.get("url")
        self.style = kwargs.get("style")

    def __call__(self, *args, **kwargs):
        return DummyObject(*args, **kwargs)

    def __getattr__(self, _name):
        return DummyObject()


class InlineKeyboardButton(DummyObject):
    pass


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = tuple(tuple(row) for row in inline_keyboard)


class Update:
    ALL_TYPES: list[str] = []


class WebAppInfo(DummyObject):
    pass


class MenuButtonWebApp(DummyObject):
    pass


class TelegramError(Exception):
    pass


class BadRequest(TelegramError):
    pass


class Forbidden(TelegramError):
    pass


class RetryAfter(TelegramError):
    def __init__(self, *args, retry_after=1, **kwargs):
        super().__init__(*args)
        self.retry_after = retry_after


class Filter:
    def __invert__(self):
        return self


class Filters:
    COMMAND = Filter()


def install_dependency_stubs() -> None:
    for name in ('JAIB_CLIENT_KEY','JAIB_CLIENT_SECRET','JAIB_RETURN_KEY'):
        os.environ.setdefault(name,'TEST_ONLY_PROFILE')
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class Session:
        def __init__(self):
            self.verify = True
            self.headers = {}

    requests.RequestException = RequestException
    requests.Timeout = Timeout
    requests.ConnectionError = ConnectionError
    requests.Session = Session
    requests.get = lambda *args, **kwargs: DummyObject()

    urllib3 = types.ModuleType("urllib3")
    urllib3.exceptions = SimpleNamespace(InsecureRequestWarning=Warning)
    urllib3.disable_warnings = lambda *args, **kwargs: None

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False

    fastapi = types.ModuleType("fastapi")

    class BackgroundTasks:
        def add_task(self, function, *args, **kwargs):
            return (function, args, kwargs)

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def mount(self, *args, **kwargs):
            return None

        def _decorator(self, *args, **kwargs):
            return lambda function: function

        get = post = put = patch = delete = on_event = _decorator

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None, **kwargs):
            super().__init__(str(detail or status_code))
            self.status_code = status_code
            self.detail = detail

    fastapi.BackgroundTasks = BackgroundTasks
    fastapi.Depends = lambda dependency=None, **kwargs: dependency
    fastapi.FastAPI = FastAPI
    fastapi.Header = lambda default=None, **kwargs: default
    fastapi.HTTPException = HTTPException
    fastapi.Query = lambda default=None, **kwargs: default
    fastapi.Request = DummyObject

    fastapi_responses = types.ModuleType("fastapi.responses")
    fastapi_responses.Response = DummyObject
    fastapi_responses.FileResponse = DummyObject
    fastapi_responses.StreamingResponse = DummyObject
    fastapi_staticfiles = types.ModuleType("fastapi.staticfiles")
    fastapi_staticfiles.StaticFiles = DummyObject
    starlette = types.ModuleType("starlette")
    starlette_background = types.ModuleType("starlette.background")
    starlette_background.BackgroundTask = DummyObject

    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.Update = Update
    telegram.WebAppInfo = WebAppInfo
    telegram.MenuButtonWebApp = MenuButtonWebApp

    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = SimpleNamespace(HTML="HTML")
    constants.ChatMemberStatus = SimpleNamespace(
        OWNER="creator",
        ADMINISTRATOR="administrator",
    )
    constants.ChatType = SimpleNamespace(
        CHANNEL="channel",
        GROUP="group",
        SUPERGROUP="supergroup",
    )

    errors = types.ModuleType("telegram.error")
    errors.TelegramError = TelegramError
    errors.BadRequest = BadRequest
    errors.Forbidden = Forbidden
    errors.RetryAfter = RetryAfter

    ext = types.ModuleType("telegram.ext")
    ext.Application = DummyObject
    ext.CallbackQueryHandler = DummyObject
    ext.CommandHandler = DummyObject
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = DummyObject
    ext.filters = Filters()

    crypto = types.ModuleType("Crypto")
    cipher = types.ModuleType("Crypto.Cipher")

    class AES:
        MODE_CBC = 1

    cipher.AES = AES
    crypto.Cipher = cipher

    reshaper = types.ModuleType("arabic_reshaper")
    reshaper.reshape = lambda value: value
    bidi = types.ModuleType("bidi")
    bidi_algorithm = types.ModuleType("bidi.algorithm")
    bidi_algorithm.get_display = lambda value: value

    sys.modules.update(
        {
            "telegram": telegram,
            "telegram.constants": constants,
            "telegram.error": errors,
            "telegram.ext": ext,
            "requests": requests,
            "urllib3": urllib3,
            "dotenv": dotenv,
            "fastapi": fastapi,
            "fastapi.responses": fastapi_responses,
            "fastapi.staticfiles": fastapi_staticfiles,
            "starlette": starlette,
            "starlette.background": starlette_background,
            "Crypto": crypto,
            "Crypto.Cipher": cipher,
            "arabic_reshaper": reshaper,
            "bidi": bidi,
            "bidi.algorithm": bidi_algorithm,
        }
    )


class FakeUser:
    def __init__(self, user_id: int, username: str = "user"):
        self.id = int(user_id)
        self.username = username
        self.first_name = f"User{user_id}"
        self.full_name = f"User {user_id}"


class FakeSentMessage:
    def __init__(self, text: str, kwargs: dict):
        self.text = text
        self.kwargs = kwargs
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs):
        self.text = text
        self.kwargs = kwargs
        self.edits.append((text, kwargs))
        return self


class FakeMessage:
    def __init__(self, text: str = "/start"):
        self.text = text
        self.replies: list[tuple[str, dict]] = []
        self.sent_messages: list[FakeSentMessage] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        sent = FakeSentMessage(text, kwargs)
        self.sent_messages.append(sent)
        return sent


class FakeUpdate(Update):
    def __init__(self, user: FakeUser, text: str = "/start"):
        self.effective_user = user
        self.effective_message = FakeMessage(text)
        self.message = self.effective_message
        self.callback_query = None
        self.update_id = user.id


class FakeBot:
    def __init__(self):
        self.id = 999999
        self.member_status = "member"
        self.photos: list[dict] = []
        self.messages: list[tuple[tuple, dict]] = []

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace()

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return SimpleNamespace()

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status=self.member_status,
            is_member=self.member_status in {"member", "administrator", "creator", "owner"},
        )


class FakeContext:
    def __init__(self, args=None):
        self.args = list(args or [])
        self.user_data: dict = {}
        self.bot = FakeBot()
        self.application = SimpleNamespace(bot_data={})
        self.error = None


def main() -> None:
    install_dependency_stubs()
    with tempfile.TemporaryDirectory(prefix="bn-wallet-final-") as temp:
        root = Path(temp)
        token = "TEST_ONLY_TELEGRAM_TOKEN_NOT_VALID"
        os.environ.update(
            {
                "TELEGRAM_BOT_TOKEN": token,
                "ADMIN_ID": "1",
                "DB_FILE": str(root / "final.db"),
                "LOG_FILE": str(root / "final.log"),
                "BINANCE_WITHDRAW_ENABLED": "false",
                "CAPTCHA_ENABLED": "true",
                "REFERRAL_REWARD_USDT": "0.5",
                "LOG_LEVEL": "DEBUG",
                "DEBUG_MODE": "true",
                "MINI_APP_URL": "https://example.com/app",
                "PUBLIC_LIQUIDITY_ENABLED_DEFAULT": "false",
                "ACCOUNT_VERIFICATION_RECIPIENT_PHONE": "74292",
                "ACCOUNT_VERIFICATION_RECIPIENT_NAME": "عبدالرحمن علي",
                "JAIB_API_UTC_OFFSET_HOURS": "0",
            }
        )

        from bot.app import runtime

        dbx = runtime.get("DBX")
        cmd_start = runtime.get("cmd_start")
        register_bot_user = runtime.get("register_bot_user")
        save_required_channel = runtime.get("save_required_channel")
        delete_required_channel = runtime.get("delete_required_channel")
        current_state = runtime.get("current_state")
        complete_referral = runtime.get("complete_referral_after_activation")
        final_on_message = runtime.get("on_message")
        wallet_prepare_deposit = runtime.get("wallet_prepare_deposit")
        wallet_create_deposit = runtime.get("wallet_create_deposit")
        wallet_claim_reference = runtime.get("wallet_claim_bank_reference")
        wallet_balances = runtime.get("wallet_balances")
        wallet_verify_reference = runtime.get("wallet_verify_deposit_reference")
        wallet_create_withdrawal = runtime.get("wallet_create_withdrawal")
        wallet_release_withdrawal = runtime.get("wallet_release_withdrawal")
        wallet_mark_submitted = runtime.get("wallet_mark_submitted")
        wallet_complete_withdrawal = runtime.get("wallet_complete_withdrawal")
        wallet_user_stats = runtime.get("wallet_user_stats")
        wallet_report_operations = runtime.get("wallet_report_operations")
        wallet_user_main_kb = runtime.get("wallet_user_main_kb")
        rate_notification_text = runtime.get("rate_notification_text")
        flush_logs = runtime.get("flush_log_handlers")
        truncate_log = runtime.get("_truncate_active_log")
        error_user_message = runtime.get("_error_user_message")
        capture_referral = runtime.get("capture_referral")
        withdrawal_operation_limit_enabled = runtime.get("withdrawal_operation_limit_enabled")
        set_withdrawal_operation_limit_enabled = runtime.get("set_withdrawal_operation_limit_enabled")
        withdrawal_operation_limit = runtime.get("withdrawal_operation_limit")
        set_withdrawal_operation_limit = runtime.get("set_withdrawal_operation_limit")
        validate_operation_limit = runtime.get("validate_operation_limit")
        get_account_verification = runtime.get("get_account_verification")
        ensure_account_verification = runtime.get("ensure_account_verification")
        save_verification_full_name = runtime.get("save_verification_full_name")
        save_verification_primary_phone = runtime.get("save_verification_primary_phone")
        save_verification_alt_phone = runtime.get("save_verification_alt_phone")
        account_verification_public = runtime.get("account_verification_public")
        normalize_yemen_mobile = runtime.get("normalize_yemen_mobile")

        # 1) A brand-new /start opens durable automated onboarding.
        new_user = FakeUser(200, "new_user")
        start_context = FakeContext()
        start_update = FakeUpdate(new_user)
        asyncio.run(cmd_start(start_update, start_context))
        assert current_state(start_context) == (None, {})
        assert len(start_context.bot.photos) == 0
        assert start_update.effective_message.replies
        assert "تسجيل الدخول / تفعيل الحساب" not in start_update.effective_message.replies[-1][0]
        assert "توثيق الحساب" in start_update.effective_message.replies[-1][0]
        assert dbx.connect().execute(
            "SELECT 1 FROM bot_users WHERE telegram_id = 200"
        ).fetchone()
        assert get_account_verification(200) is None
        request = ensure_account_verification(new_user)
        assert request and request["status"] == "DRAFT"

        # Identity values are validated, normalized, and persisted once.
        try:
            save_verification_full_name(200, "اسم ثلاثي فقط")
        except ValueError as exc:
            assert "رباع" in str(exc)
        else:
            raise AssertionError("A three-part name was accepted")
        save_verification_full_name(200, "عبدالرحمن علي محمد أحمد")
        assert normalize_yemen_mobile("+967771234567") == "771234567"
        save_verification_primary_phone(200, "+967771234567")
        save_verification_alt_phone(200, "74292")
        public_request = account_verification_public(get_account_verification(200))
        assert public_request["steps"]["full_name"] is True
        assert public_request["steps"]["phone_primary"] is True
        assert public_request["steps"]["phone_alt"] is True
        verification_amount = Decimal(public_request["recipient"]["amount_yer"])
        assert Decimal("100") <= verification_amount <= Decimal("500")
        assert verification_amount % Decimal("50") == 0

        # 2) Referral payload is recorded before onboarding and does not interrupt it.
        referrer = FakeUser(100, "referrer")
        assert register_bot_user(referrer) is True
        referred = FakeUser(201, "referred")
        referral_context = FakeContext(["ref_100"])
        referral_update = FakeUpdate(referred, "/start ref_100")
        asyncio.run(cmd_start(referral_update, referral_context))
        referral_row = dbx.connect().execute(
            "SELECT * FROM referrals WHERE referred_telegram_id = 201"
        ).fetchone()
        assert referral_row and referral_row["referrer_telegram_id"] == 100
        assert get_account_verification(201) is None
        assert "توثيق الحساب" in referral_update.effective_message.replies[-1][0]
        assert len(referral_context.bot.photos) == 0

        # Required subscription is checked after referral capture and before onboarding.
        save_required_channel("-1001234567890", "required_test", "Required Test", "https://t.me/required_test", 1)
        channel_row = dbx.connect().execute(
            "SELECT id FROM required_channels WHERE chat_id='-1001234567890'"
        ).fetchone()
        gated_user = FakeUser(202, "gated_referred")
        gated_context = FakeContext(["ref_100"])
        gated_context.bot.member_status = "left"
        gated_update = FakeUpdate(gated_user, "/start ref_100")
        asyncio.run(cmd_start(gated_update, gated_context))
        assert dbx.connect().execute(
            "SELECT 1 FROM referrals WHERE referred_telegram_id=202 AND referrer_telegram_id=100"
        ).fetchone()
        assert get_account_verification(202) is None
        assert gated_update.effective_message.replies
        assert "الاشتراك الإجباري" in gated_update.effective_message.replies[-1][0]
        gated_context.bot.member_status = "member"
        asyncio.run(cmd_start(gated_update, gated_context))
        assert get_account_verification(202) is None
        assert "توثيق الحساب" in gated_update.effective_message.replies[-1][0]
        delete_required_channel(int(channel_row["id"]))

        # 3) Referral reward is atomic and idempotent.
        first_reward = asyncio.run(complete_referral(referral_context, 201))
        second_reward = asyncio.run(complete_referral(referral_context, 201))
        assert first_reward == Decimal("0.50000000")
        assert second_reward == Decimal("0")
        available, locked = wallet_balances(100)
        assert available == Decimal("0.50000000")
        assert locked == Decimal("0E-8")
        reward_ledger_count = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_ledger WHERE transaction_type='REFERRAL_REWARD'"
        ).fetchone()["c"]
        assert reward_ledger_count == 1

        # 4) Opening deposit instructions creates no financial operation.
        dbx.create_customer("Deposit User", "777000300", telegram_id=300)
        before_prepare = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_deposits WHERE telegram_user_id=300"
        ).fetchone()["c"]
        prepared = wallet_prepare_deposit(300)
        after_prepare = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_deposits WHERE telegram_user_id=300"
        ).fetchone()["c"]
        assert before_prepare == after_prepare == 0
        assert prepared["exchange_rate_yer"] == "550.00"

        # The operation row is created only when a reference is submitted.
        deposit = wallet_create_deposit(300, exchange_rate_yer=Decimal(prepared["exchange_rate_yer"]))
        assert deposit["status"] == "WAITING_REFERENCE"
        assert deposit["fiat_amount_yer"] == "0.00"
        assert deposit["usdt_amount"] == "0.00000000"
        assert deposit["amount_source"] == "JAIB_TRANSACTION"
        claimed, used_by = wallet_claim_reference(deposit["id"], "JAIB-REF-300")
        assert claimed is True and used_by is None
        duplicate, duplicate_owner = wallet_claim_reference(deposit["id"], "JAIB-REF-300")
        assert duplicate is False and duplicate_owner == deposit["deposit_code"]

        # 5) Jaib is the source of truth for amount and payer metadata.
        import bot.wallet.deposit_verification as deposit_verification

        class FakeJaibTx:
            def __init__(self):
                self.amount = Decimal("5500")
                self.raw = {"amount": "5500", "isDebitTranID": False}
                self.api_details = SimpleNamespace(
                    payer_display="Deposit User",
                    phone_candidates=[
                        {"value": "777000300", "source": "senderPhone"},
                        {"value": "733000300", "source": "alternateNumber"},
                    ],
                    identifiers=[{"value": "777000300", "source": "senderPhone"}],
                    tx_date=runtime.get("now_str")(),
                    fields=[],
                )

        class FakeJaibClient:
            @staticmethod
            def enabled():
                return True

            def init(self):
                return True

            def login(self):
                return True, "ok"

            def query_ref(self, _reference):
                tx = FakeJaibTx()
                tx.reference_id = _reference
                return True, tx

        async def no_op_async(*_args, **_kwargs):
            return None

        original_client = deposit_verification.JaibClient
        original_tx = deposit_verification.JaibTx
        original_notify = deposit_verification.notify_admin
        original_technical = deposit_verification.send_technical_log
        deposit_verification.JaibClient = FakeJaibClient
        deposit_verification.JaibTx = FakeJaibTx
        deposit_verification.notify_admin = no_op_async
        deposit_verification.send_technical_log = no_op_async
        try:
            verification_deposit = wallet_create_deposit(300)
            verification_update = FakeUpdate(FakeUser(300, "deposit_user"), "JAIB-REF-301")
            verification_context = FakeContext()
            consumed = asyncio.run(
                wallet_verify_reference(
                    verification_update,
                    verification_context,
                    verification_deposit["id"],
                    "JAIB-REF-301",
                )
            )
        finally:
            deposit_verification.JaibClient = original_client
            deposit_verification.JaibTx = original_tx
            deposit_verification.notify_admin = original_notify
            deposit_verification.send_technical_log = original_technical

        assert consumed is True
        verified = dbx.connect().execute(
            "SELECT * FROM wallet_deposits WHERE id=?",
            (verification_deposit["id"],),
        ).fetchone()
        assert verified["status"] == "COMPLETED"
        assert verified["fiat_amount_yer"] == "5500.00"
        assert verified["usdt_amount"] == "10.00000000"
        assert verified["payer_name"] == "Deposit User"
        assert verified["payer_phone"] == "777000300"
        assert verified["payer_phone_alt"] == "733000300"
        deposit_available, _ = wallet_balances(300)
        assert deposit_available == Decimal("10.00000000")

        # 6) Wallet reservations are released on failure and finalized once on success.
        released_withdrawal = wallet_create_withdrawal(
            300, Decimal("2"), "BSC", "0x" + "1" * 40, "BINANCE"
        )
        available, locked = wallet_balances(300)
        assert available == Decimal("8.00000000") and locked == Decimal("2.00000000")
        wallet_release_withdrawal(
            released_withdrawal["id"],
            error_code="OFFLINE_TEST",
            error_message="release path",
        )
        available, locked = wallet_balances(300)
        assert available == Decimal("10.00000000") and locked == Decimal("0E-8")

        completed_withdrawal = wallet_create_withdrawal(
            300, Decimal("3"), "BSC", "0x" + "2" * 40, "BINANCE"
        )
        wallet_mark_submitted(
            completed_withdrawal["id"],
            provider_withdraw_id="OFFLINE-PROVIDER-ID",
            txid="OFFLINE-TXID",
        )
        wallet_complete_withdrawal(completed_withdrawal["id"], "OFFLINE-TXID")
        available, locked = wallet_balances(300)
        assert available == Decimal("7.00000000") and locked == Decimal("0E-8")
        # Abandoned/cancelled UI sessions never enter user statistics or reports.
        with dbx.connect() as con:
            con.execute(
                "UPDATE wallet_deposits SET status='CANCELLED', updated_at=? WHERE id=?",
                (runtime.get("now_str")(), deposit["id"]),
            )
            con.commit()
        stats = wallet_user_stats(300)
        assert stats["purchase_count"] == 1
        assert stats["withdraw_count"] == 1
        assert stats["purchase_usdt"] == Decimal("10.00000000")
        assert stats["withdraw_usdt"] == Decimal("3.00000000")
        assert stats["operation_count"] == 3  # completed deposit + completed/failed withdrawals
        assert stats["successful_operations"] == 2
        assert stats["failed_operations"] == 1
        report_rows = wallet_report_operations(300)
        assert len(report_rows) == 3
        assert {row["status"] for row in report_rows} == {"COMPLETED", "FAILED_RELEASED"}
        assert all(row["status"] != "CANCELLED" for row in report_rows)

        # Main keyboard has no duplicate balance button and exposes the full Mini App.
        keyboard = wallet_user_main_kb(False)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        assert all(getattr(button, "callback_data", None) != "wallet_balance" for button in buttons)
        assert sum(getattr(button, "callback_data", None) == "wallet_referrals" for button in buttons) == 1
        assert any(getattr(button, "kwargs", {}).get("web_app") for button in buttons)

        # 7) Dynamic Binance limits use provider data and refresh on demand.
        import bot.services.dynamic_withdrawals as dynamic_withdrawals

        class FakeBinanceProvider:
            def enabled(self):
                return True

        class FakeBinance:
            def __init__(self):
                self.minimum = "2.5"
                self.refreshes: list[bool] = []

            def get_coin_config(self, _coin, force_refresh=False):
                self.refreshes.append(bool(force_refresh))
                return {
                    "withdrawAllEnable": True,
                    "networkList": [{
                        "network": "BSC",
                        "withdrawEnable": True,
                        "busy": False,
                        "withdrawMin": self.minimum,
                        "withdrawFee": "0.1",
                    }],
                }

        original_provider = dynamic_withdrawals.BINANCE_PROVIDER
        original_binance = dynamic_withdrawals.BINANCE
        fake_binance = FakeBinance()
        dynamic_withdrawals.BINANCE_PROVIDER = FakeBinanceProvider()
        dynamic_withdrawals.BINANCE = fake_binance
        try:
            first_limits = dynamic_withdrawals.binance_live_limits("BSC", force_refresh=True)
            fake_binance.minimum = "5"
            second_limits = dynamic_withdrawals.binance_live_limits("BSC", force_refresh=True)
        finally:
            dynamic_withdrawals.BINANCE_PROVIDER = original_provider
            dynamic_withdrawals.BINANCE = original_binance
        assert first_limits["minimum"] == Decimal("2.50000000")
        assert second_limits["minimum"] == Decimal("5.00000000")
        assert fake_binance.refreshes == [True, True]

        # 8) The per-operation ceiling is administrator controlled and enforced centrally.
        original_limit_enabled = withdrawal_operation_limit_enabled()
        original_limit = withdrawal_operation_limit()
        try:
            set_withdrawal_operation_limit_enabled(True)
            set_withdrawal_operation_limit("1.5")
            assert validate_operation_limit("1.5") == Decimal("1.50000000")
            try:
                validate_operation_limit("1.50000001")
            except Exception as exc:
                assert "الحد الأقصى" in str(exc)
            else:
                raise AssertionError("Per-operation withdrawal ceiling was not enforced")
            set_withdrawal_operation_limit_enabled(False)
            assert validate_operation_limit("2") == Decimal("2.00000000")
        finally:
            set_withdrawal_operation_limit(original_limit)
            set_withdrawal_operation_limit_enabled(original_limit_enabled)

        # 9) Rate notices and Telegram Mini App authentication stay valid.
        assert "1 USDT = 540.0000 ريال يمني" in rate_notification_text(Decimal("540"))
        from bot.webapp.server import validate_init_data

        auth_user = {"id": 300, "first_name": "Deposit User"}
        init_pairs = {
            "auth_date": str(int(time.time())),
            "query_id": "AAEAAAE",
            "user": json.dumps(auth_user, separators=(",", ":")),
        }
        check_string = "\n".join(f"{key}={init_pairs[key]}" for key in sorted(init_pairs))
        secret = hmac.new(
            b"WebAppData",
            token.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        init_pairs["hash"] = hmac.new(
            secret, check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert int(validate_init_data(urlencode(init_pairs))["id"]) == 300

        import bot.webapp.server as webapp_server

        # Unknown users receive a professional limited bootstrap instead of an access error.
        unknown_bootstrap = asyncio.run(
            webapp_server.bootstrap({"id": 9090, "first_name": "New Visitor", "username": "visitor"})
        )
        assert unknown_bootstrap["access"]["granted"] is False
        assert unknown_bootstrap["access"]["status"] == "NEW"
        assert "Jaib" in unknown_bootstrap["access"]["description"]
        assert unknown_bootstrap["access"]["contact_url"]
        assert unknown_bootstrap["onboarding"]["status"] == "NEW"
        started_onboarding = webapp_server.onboarding_start(
            {"id": 9090, "first_name": "New Visitor", "username": "visitor"}
        )
        assert started_onboarding["request"]["status"] == "DRAFT"
        onboarding_amount = Decimal(started_onboarding["request"]["recipient"]["amount_yer"])
        assert Decimal("100") <= onboarding_amount <= Decimal("500")
        assert onboarding_amount % Decimal("50") == 0

        # Active users receive liquidity and withdrawal policy without a public provider name.
        active_bootstrap = asyncio.run(webapp_server.bootstrap(auth_user))
        assert active_bootstrap["access"]["granted"] is True
        assert "liquidity" in active_bootstrap
        assert "provider" not in active_bootstrap["liquidity"]
        assert active_bootstrap["payment"]["secondary_beneficiary_name"] == "عبدالرحمن علي"
        assert active_bootstrap["withdrawal_policy"]["operation_limit"]
        deposit_count_before = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_deposits"
        ).fetchone()["c"]
        draft = webapp_server.prepare_deposit(auth_user)
        deposit_count_after = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_deposits"
        ).fetchone()["c"]
        assert deposit_count_before == deposit_count_after
        action_body = webapp_server._decode_action(draft["session_token"], "deposit", 300)
        webapp_server._consume_action(action_body)
        try:
            webapp_server._consume_action(action_body)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("Mini App one-time action token was reused")

        # Mini App admin activation rewards a pending referral exactly once.
        referred_via_admin = FakeUser(203, "admin_referred")
        assert register_bot_user(referred_via_admin) is True
        assert capture_referral(203, "ref_100") == "CAPTURED"
        admin_referred_customer_id = dbx.create_customer(
            "Admin Referred", "777000203", telegram_id=203
        )
        dbx.update_customer(admin_referred_customer_id, status="PENDING")
        original_bot_context = webapp_server._bot_context

        async def fake_bot_context():
            return FakeBot()

        webapp_server._bot_context = fake_bot_context
        try:
            status_request = webapp_server.CustomerStatusRequest(status="ACTIVE")
            first_activation = asyncio.run(
                webapp_server.admin_set_user_status(203, status_request, {"id": 1})
            )
            second_activation = asyncio.run(
                webapp_server.admin_set_user_status(203, status_request, {"id": 1})
            )
        finally:
            webapp_server._bot_context = original_bot_context
        assert first_activation["referral_reward"] == "0.50000000"
        assert second_activation["referral_reward"] == "0.00000000"
        admin_reward_rows = dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_ledger "
            "WHERE transaction_type='REFERRAL_REWARD' AND notes LIKE '%Telegram ID 203%'"
        ).fetchone()["c"]
        assert admin_reward_rows == 1

        # 10) A full backup restore removes newer rows and preserves wallet/referral data.
        snapshot = dbx.export_snapshot()
        dbx.create_customer("Temporary User", "777999999", telegram_id=999)
        dbx.import_snapshot(snapshot)
        assert dbx.get_customer_by_telegram(999) is None
        assert dbx.connect().execute(
            "SELECT COUNT(*) AS c FROM wallet_ledger WHERE transaction_type='REFERRAL_REWARD'"
        ).fetchone()["c"] == 2
        assert dbx.connect().execute("PRAGMA foreign_key_check").fetchall() == []

        # 11) /start errors use registration wording, never wallet transaction wording.
        error_text = error_user_message(start_update, start_context, "REG-TEST")
        assert "بدء التسجيل" in error_text
        assert "حالة العملية" not in error_text

        # 12) File logs redact secrets and can be truncated without deleting the file.
        logger = logging.getLogger("bn_wallet.final_test")
        logger.error("secret=%s", token)
        flush_logs()
        log_path = Path(runtime.get("LOG_PATH"))
        log_text = log_path.read_text(encoding="utf-8")
        assert token not in log_text
        assert "***REDACTED***" in log_text
        truncate_log()
        assert log_path.exists() and log_path.stat().st_size == 0

        # 13) Every referenced global resolves after the compatibility loader syncs modules.
        allowed_globals = set(dir(builtins)) | {"__name__", "__file__", "__package__"}
        unresolved: list[str] = []
        for module_path in sorted((ROOT / "bot").rglob("*.py")):
            module_name = ".".join(module_path.relative_to(ROOT).with_suffix("").parts)
            module = importlib.import_module(module_name)
            module_names = set(module.__dict__)
            table = symtable.symtable(
                module_path.read_text(encoding="utf-8"),
                str(module_path),
                "exec",
            )

            def inspect_table(symbol_table):
                if symbol_table.get_type() in {"function", "class"}:
                    for symbol in symbol_table.get_symbols():
                        name = symbol.get_name()
                        if (
                            symbol.is_referenced()
                            and symbol.is_global()
                            and name not in module_names
                            and name not in allowed_globals
                        ):
                            unresolved.append(
                                f"{module_name}:{symbol_table.get_lineno()}:{symbol_table.get_name()}:{name}"
                            )
                for child in symbol_table.get_children():
                    inspect_table(child)

            inspect_table(table)
        assert unresolved == [], "\n".join(unresolved)

        # 14) Static regression guards for phase-two UI and module wiring.
        source = (ROOT / "bot/handlers/wallet_text.py").read_text(encoding="utf-8")
        assert "await_wallet_deposit_yer" not in source
        app_source = (ROOT / "bot/app.py").read_text(encoding="utf-8")
        assert '"bot.services.log_management"' in app_source
        assert '"bot.services.liquidity"' in app_source
        assert "RotatingFileHandler" in (ROOT / "bot/config.py").read_text(encoding="utf-8")
        mini_app_source = (ROOT / "bot/webapp/static/index.html").read_text(encoding="utf-8")
        assert 'id="landing"' in mini_app_source
        assert 'id="resultOverlay"' in mini_app_source
        assert "data-copy" in mini_app_source
        assert "تقديم طلب التفعيل" in mini_app_source
        assert 'data-onboarding-start' in mini_app_source
        assert 'data-onboarding-submit' in mini_app_source
        assert 'id="withdrawDisclaimerAccepted"' in mini_app_source
        assert 'id="depositBeneficiary"' in mini_app_source

    print(
        "PASS: automated onboarding, referral, Jaib-sourced deposit, wallet ledger, "
        "live limits, configurable withdrawal ceiling, landing/access flow, Mini App activation rewards, "
        "auth/action tokens, backup restore, logging, symbol integrity, and regression checks"
    )


if __name__ == "__main__":
    main()

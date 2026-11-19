"""Application lifecycle, global error handling, and Telegram polling entry point."""
from __future__ import annotations

import traceback
from types import SimpleNamespace
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo
from telegram.error import BadRequest
try:
    from telegram.error import NetworkError, TimedOut
except ImportError:  # توافق مع بيئة الاختبارات الخفيفة
    class NetworkError(Exception):
        pass

    class TimedOut(NetworkError):
        pass
from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())


_wallet_legacy_post_init = post_init


async def monitor_runtime_heartbeat(_context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.to_thread(record_runtime_identity, "TELEGRAM_BOT")
    except Exception as exc:
        log.warning("Runtime heartbeat failed service=TELEGRAM_BOT error=%s", type(exc).__name__)


async def post_init(application: Application):
    await _wallet_legacy_post_init(application)

    diagnostics = await asyncio.to_thread(record_runtime_identity, "TELEGRAM_BOT", refresh_ip=True)
    log.info(
        "Runtime identity service=%s pid=%s parent_pid=%s cwd=%s env=%s db=%s key_fp=%s egress_ip=%s",
        diagnostics["service"], diagnostics["pid"], diagnostics["parent_pid"],
        diagnostics["cwd"], diagnostics["env_path"], diagnostics["db_path"],
        diagnostics["key_fingerprint"], diagnostics["egress_ip"],
    )
    health = await asyncio.to_thread(binance_health_check, force_ip=True)
    health_context = SimpleNamespace(bot=application.bot)
    if health.get("ok"):
        log.info(
            "Binance startup check succeeded key_fp=%s egress_ip=%s",
            health.get("key_fingerprint"),
            health.get("egress_ip"),
        )
    else:
        log.error(
            "Binance startup check failed reason=%s key_fp=%s egress_ip=%s",
            health.get("reason"),
            health.get("key_fingerprint"),
            health.get("egress_ip"),
        )
        await notify_admin(
            health_context,
            render_bot_text('notice.bot.application.py:58','<b>🚨 فشل فحص Binance عند تشغيل البوت</b>\n\nالسبب: <code>{v0}</code>\nIP: <code>{v1}</code>\nبصمة المفتاح: <code>{v2}</code>\n\nتم إيقاف السحب فقط، وسيُعاد الفحص كل 30 ثانية.',[f"{html(health.get('reason') or 'غير معروف')}",f"{html(health.get('egress_ip') or 'UNKNOWN')}",f"{html(health.get('key_fingerprint') or 'UNSET')}"]),
        )

    jaib_health = await asyncio.to_thread(jaib_health_check)
    if jaib_health.get("ok"):
        log.info(
            "Jaib startup check succeeded response_ms=%s",
            jaib_health.get("response_ms"),
        )
    else:
        log.error(
            "Jaib startup check failed reason=%s",
            jaib_health.get("reason"),
        )
        await notify_admin(
            health_context,
            render_bot_text('notice.bot.application.py:78','<b>🚨 فشل فحص Jaib عند تشغيل البوت</b>\n\nالسبب: <code>{v0}</code>\nوقت الفحص: <code>{v1}</code>\n\nسيُعاد الفحص تلقائيًا كل {v2} ثانية، وسيصل إشعار عند عودة الاتصال.',[f"{html(jaib_health.get('reason') or 'غير معروف')}",f"{html(jaib_health.get('checked_at') or '—')}",f'{JAIB_HEALTH_CHECK_INTERVAL_SECONDS}']),
        )
    await recover_wallet_withdrawals_on_startup(health_context)

    try:
        me = await application.bot.get_me()
        if getattr(me, "username", None):
            DBX.set_setting("bot_username", str(me.username))
        mini_url = str(globals().get("MINI_APP_URL", "") or "").strip()
        if mini_url:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="فتح التطبيق",
                    web_app=WebAppInfo(url=mini_url),
                )
            )
    except Exception:
        # The menu button is an enhancement; a temporary Telegram transport
        # failure must not prevent the polling service from starting.
        log.exception("Failed to configure Telegram Mini App menu button")

    if application.job_queue is None:
        raise RuntimeError("JobQueue غير متاح. ثبّت python-telegram-bot[job-queue].")
    if not application.job_queue.get_jobs_by_name("wallet-withdraw-monitor"):
        application.job_queue.run_repeating(
            monitor_wallet_withdrawals,
            interval=30,
            first=20,
            name="wallet-withdraw-monitor",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    if not application.job_queue.get_jobs_by_name("binance-health-monitor"):
        application.job_queue.run_repeating(
            monitor_binance_health,
            interval=30,
            first=30,
            name="binance-health-monitor",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    if not application.job_queue.get_jobs_by_name("jaib-health-monitor"):
        application.job_queue.run_repeating(
            monitor_jaib_health,
            interval=JAIB_HEALTH_CHECK_INTERVAL_SECONDS,
            first=JAIB_HEALTH_CHECK_INTERVAL_SECONDS,
            name="jaib-health-monitor",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    if not application.job_queue.get_jobs_by_name("wallet-notification-outbox"):
        application.job_queue.run_repeating(
            monitor_wallet_notification_outbox,
            interval=60,
            first=15,
            name="wallet-notification-outbox",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    if not application.job_queue.get_jobs_by_name("wallet-liquidity-alerts"):
        application.job_queue.run_repeating(
            monitor_liquidity_alerts,
            interval=60,
            first=35,
            name="wallet-liquidity-alerts",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    if not application.job_queue.get_jobs_by_name("runtime-heartbeat"):
        application.job_queue.run_repeating(
            monitor_runtime_heartbeat,
            interval=60,
            first=60,
            name="runtime-heartbeat",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    log.info(
        "Wallet service enabled deposit=%s withdraw=%s auto_withdraw=%s decimals=%s",
        wallet_deposit_enabled(),
        wallet_withdraw_enabled(),
        wallet_auto_withdraw_enabled(),
        display_decimals(),
    )


def _error_user_message(update: object, context: ContextTypes.DEFAULT_TYPE, reference: str) -> str:
    state, _ = current_state(context)
    message = update.effective_message if isinstance(update, Update) else None
    text = (getattr(message, "text", None) or "").strip().lower()
    callback_data = ""
    if isinstance(update, Update) and update.callback_query:
        callback_data = str(update.callback_query.data or "")

    if text.startswith("/start") or state == "await_registration_captcha":
        return (
            "⚠️ تعذر بدء التسجيل مؤقتًا بسبب مشكلة تقنية.\n\n"
            "حاول إرسال /start مرة أخرى، وإذا استمرت المشكلة فتواصل مع الإدارة.\n"
            f"رقم المرجع: <code>{html(reference)}</code>"
        )

    financial = bool(
        (state and str(state).startswith("await_wallet_"))
        or callback_data.startswith("wallet_")
        or callback_data.startswith("adm_wallet")
    )
    if financial:
        return wallet_user_status_message(reference)

    return (
        "⚠️ حدث خطأ تقني أثناء تنفيذ الطلب.\n\n"
        "حاول مرة أخرى، وإذا استمرت المشكلة أرسل رقم المرجع للإدارة.\n"
        f"رقم المرجع: <code>{html(reference)}</code>"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    
    # --- التعديل: تجاهل خطأ تطابق النص عند الضغط المزدوج على الأزرار ---
    if isinstance(error, BadRequest):
        lowered = str(error).lower()
        if "message is not modified" in lowered:
            return
        if "query is too old" in lowered or "query id is invalid" in lowered or "response timeout expired" in lowered:
            log.info("Ignored expired callback query: %s", error)
            return
    # -------------------------------------------------------------------

    if isinstance(error, (NetworkError, TimedOut)):
        # انقطاع Telegram مؤقت ويعالجه polling تلقائيًا؛ لا نرسل إنذارًا حرجًا متكررًا.
        log.warning("Temporary Telegram network interruption: %s", error)
        return

    reference = wallet_ref("ERR")
    user_id = None
    state, _ = current_state(context)

    if isinstance(update, Update) and update.effective_user:
        user_id = int(update.effective_user.id)

    if error is not None:
        log.error(
            "Unhandled bot error reference=%s user=%s state=%s update_id=%s",
            reference,
            user_id,
            state,
            getattr(update, "update_id", None),
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        log.error(
            "Unhandled bot error without exception reference=%s user=%s state=%s",
            reference,
            user_id,
            state,
        )

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                render_bot_text('bot.application.py:235:message','{v0}', [str(_error_user_message(update, context, reference))]),
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        # A Telegram transport failure may be the original exception; do not loop.
        log.exception("Failed to send safe error message reference=%s", reference)

    try:
        details = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ) if error is not None else "context.error is None"
        await send_technical_log(
            context,
            reference_id=reference,
            event_type="Unhandled bot error",
            details=(
                f"state={state}\n"
                f"update_id={getattr(update, 'update_id', None)}\n"
                f"{details}"
            ),
            severity="CRITICAL",
            telegram_user_id=user_id,
        )
    except Exception:
        log.exception("Failed to report unhandled error reference=%s", reference)


_wallet_legacy_post_shutdown = globals().get("post_shutdown")


async def post_shutdown(application: Application):
    try:
        await asyncio.to_thread(record_runtime_identity, "TELEGRAM_BOT", stopped=True)
    finally:
        if _wallet_legacy_post_shutdown is not None:
            await _wallet_legacy_post_shutdown(application)


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the full Mini App while keeping all bot commands available."""
    mini_url = str(globals().get("MINI_APP_URL", "") or "").strip()
    if not mini_url:
        await update.effective_message.reply_text(render_bot_text('bot.application.py:278:message','⚠️ رابط التطبيق غير مضبوط حاليًا.', []))
        return
    await update.effective_message.reply_text(
        render_bot_text('bot.application.py:280:message','🚀 افتح التطبيق الكامل لإدارة الرصيد والإيداع والسحب والعمليات.', []),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(render_bot_button('bot.application.py:283:button','فتح التطبيق الكامل', []), web_app=WebAppInfo(url=mini_url))]]
        ),
    )

def main():
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
    )
    # ``post_shutdown`` is provided by the log-management layer.
    if "post_shutdown" in globals():
        builder = builder.post_shutdown(post_shutdown)

    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("buttons", cmd_buttons))
    app.add_handler(CommandHandler("setminwithdraw", cmd_setminwithdraw))
    app.add_handler(CommandHandler("customize", cmd_customize))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
        timeout=30,
    )


_export_globals(globals())

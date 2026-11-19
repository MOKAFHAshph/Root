"""Extended user statistics, Mini App entry button, and PDF export callback."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

MINI_APP_URL = env("MINI_APP_URL", "").strip()

_reporting_legacy_wallet_user_main_kb = wallet_user_main_kb


def wallet_user_main_kb(is_admin: bool) -> InlineKeyboardMarkup:
    base = _reporting_legacy_wallet_user_main_kb(is_admin)
    rows = [list(row) for row in base.inline_keyboard]
    callbacks = {
        getattr(button, "callback_data", None)
        for row in rows
        for button in row
    }
    insert_at = max(0, len(rows) - (1 if is_admin else 0))
    if "wallet_referrals" not in callbacks:
        rows.insert(insert_at, [btn(render_bot_button('bot.services.user_reporting.py:30:button','👥 الإحالات', []), "wallet_referrals", "primary")])
        insert_at += 1
    if MINI_APP_URL:
        rows.insert(
            insert_at,
            [InlineKeyboardButton(render_bot_button('bot.services.user_reporting.py:35:button','🚀 فتح التطبيق الكامل', []), web_app=WebAppInfo(url=MINI_APP_URL))],
        )
    return InlineKeyboardMarkup(rows)


user_main_kb = wallet_user_main_kb


def wallet_report_operations(telegram_user_id: int) -> list[dict[str, Any]]:
    """Return only final, reportable financial outcomes.

    UI drafts, waiting states, reviews, and user cancellations are deliberately
    excluded. A failed withdrawal is reportable only after the reserved balance
    has been released (``FAILED_RELEASED``), and a deposit failure is reportable
    only after an explicit final rejection (``REJECTED``).
    """
    user_id = int(telegram_user_id)
    with DBX.connect() as con:
        rows = con.execute(
            """
            SELECT COALESCE(completed_at, updated_at, created_at) AS event_at,
                   created_at, 'PURCHASE' AS operation_type,
                   deposit_code AS reference, usdt_amount AS amount,
                   'USDT' AS currency, status,
                   review_reason AS failure_code,
                   error_message, NULL AS txid, NULL AS network_code,
                   NULL AS destination_address
            FROM wallet_deposits
            WHERE telegram_user_id = ? AND status IN ('COMPLETED', 'REJECTED')
            UNION ALL
            SELECT COALESCE(completed_at, updated_at, created_at) AS event_at,
                   created_at, 'WITHDRAWAL' AS operation_type,
                   withdrawal_code AS reference, amount_usdt AS amount,
                   'USDT' AS currency, status,
                   error_code AS failure_code,
                   error_message, txid, network_code, destination_address
            FROM wallet_withdrawals
            WHERE telegram_user_id = ? AND status IN ('COMPLETED', 'FAILED_RELEASED', 'EXPIRED_RELEASED')
            ORDER BY event_at DESC
            """,
            (user_id, user_id),
        ).fetchall()
    labels = {"PURCHASE": "شراء USDT", "WITHDRAWAL": "سحب USDT"}
    return [
        {
            "created_at": row["event_at"] or row["created_at"],
            "type": row["operation_type"],
            "type_label": labels.get(row["operation_type"], row["operation_type"]),
            "reference": row["reference"],
            "amount": row["amount"],
            "currency": row["currency"],
            "status": row["status"],
            "successful": row["status"] == "COMPLETED",
            "failure_code": row["failure_code"] or "",
            "error_message": row["error_message"] or "",
            "txid": normalize_txid(row["txid"]),
            "network_code": row["network_code"] or "",
            "destination_address": row["destination_address"] or "",
        }
        for row in rows
    ]


def _sum_decimal(values: list[Any]) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += wallet_decimal(value or 0)
    return wallet_decimal(total)


def _display_amount(value: Any, *, places: int | None = None) -> str:
    """تنسيق العرض وفق إعداد الخانات العشرية دون تغيير دقة التخزين."""
    return format_display_amount(value, places)


def wallet_user_stats(telegram_user_id: int) -> dict[str, Any]:
    user_id = int(telegram_user_id)
    wallet = wallet_get(user_id)
    with DBX.connect() as con:
        purchase_rows = con.execute(
            """
            SELECT usdt_amount, fiat_amount_yer, created_at,
                   COALESCE(completed_at, updated_at, created_at) AS last_at
            FROM wallet_deposits
            WHERE telegram_user_id = ? AND status = 'COMPLETED'
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        withdrawal_rows = con.execute(
            """
            SELECT amount_usdt, created_at,
                   COALESCE(completed_at, updated_at, created_at) AS last_at
            FROM wallet_withdrawals
            WHERE telegram_user_id = ? AND status = 'COMPLETED'
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        attempts = con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM wallet_deposits
                 WHERE telegram_user_id = ? AND status IN ('COMPLETED','REJECTED')) +
                (SELECT COUNT(*) FROM wallet_withdrawals
                 WHERE telegram_user_id = ? AND status IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED')) AS total,
                (SELECT COUNT(*) FROM wallet_deposits
                 WHERE telegram_user_id = ? AND status = 'COMPLETED') +
                (SELECT COUNT(*) FROM wallet_withdrawals
                 WHERE telegram_user_id = ? AND status = 'COMPLETED') AS successful,
                (SELECT COUNT(*) FROM wallet_deposits
                 WHERE telegram_user_id = ? AND status = 'REJECTED') +
                (SELECT COUNT(*) FROM wallet_withdrawals
                 WHERE telegram_user_id = ? AND status IN ('FAILED_RELEASED','EXPIRED_RELEASED')) AS failed
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id),
        ).fetchone()
        activity = con.execute(
            """
            SELECT MIN(event_at) AS first_at, MAX(event_at) AS last_at
            FROM (
                SELECT COALESCE(completed_at, updated_at, created_at) AS event_at
                FROM wallet_deposits
                WHERE telegram_user_id = ? AND status IN ('COMPLETED','REJECTED')
                UNION ALL
                SELECT COALESCE(completed_at, updated_at, created_at) AS event_at
                FROM wallet_withdrawals
                WHERE telegram_user_id = ? AND status IN ('COMPLETED','FAILED_RELEASED','EXPIRED_RELEASED')
            )
            """,
            (user_id, user_id),
        ).fetchone()
        first_seen = con.execute(
            "SELECT first_seen_at FROM bot_users WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        customer = con.execute(
            "SELECT created_at FROM customers WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        recent = con.execute(
            """
            SELECT transaction_type, status, amount_usdt, fiat_amount_yer,
                   network_code, txid, created_at
            FROM wallet_ledger
            WHERE telegram_user_id = ?
            ORDER BY id DESC LIMIT 8
            """,
            (user_id,),
        ).fetchall()

    referral = referral_summary(user_id)
    purchase_usdt = _sum_decimal([row["usdt_amount"] for row in purchase_rows])
    purchase_yer = sum(
        (Decimal(str(row["fiat_amount_yer"] or 0)) for row in purchase_rows),
        Decimal("0"),
    ).quantize(Decimal("0.01"))
    withdraw_usdt = _sum_decimal([row["amount_usdt"] for row in withdrawal_rows])
    first_dates = [
        value for value in (
            activity["first_at"] if activity else None,
            customer["created_at"] if customer else None,
            first_seen["first_seen_at"] if first_seen else None,
        ) if value
    ]
    total_attempts = int(attempts["total"] or 0)
    successful = int(attempts["successful"] or 0)
    failed = int(attempts["failed"] or 0)
    success_rate = (successful / total_attempts * 100.0) if total_attempts else 0.0
    withdrawal_ratio = (float(withdraw_usdt / purchase_usdt * 100)) if purchase_usdt > 0 else 0.0

    return {
        "available": wallet_decimal(wallet["available_balance"]),
        "locked": wallet_decimal(wallet["locked_balance"]),
        "total": wallet_decimal(wallet["available_balance"]) + wallet_decimal(wallet["locked_balance"]),
        "purchase_count": len(purchase_rows),
        "deposit_count": len(purchase_rows),
        "purchase_usdt": purchase_usdt,
        "deposit_usdt": purchase_usdt,
        "purchase_yer": purchase_yer,
        "deposit_yer": purchase_yer,
        "withdraw_count": len(withdrawal_rows),
        "withdraw_usdt": withdraw_usdt,
        "referral_count": referral["count"],
        "active_referral_count": referral["active_count"],
        "referral_balance": referral["balance"],
        "operation_count": total_attempts,
        "successful_operations": successful,
        "failed_operations": failed,
        "success_rate": success_rate,
        "withdrawal_ratio": min(withdrawal_ratio, 999.9),
        "first_used_at": min(first_dates) if first_dates else None,
        "first_at": min(first_dates) if first_dates else None,
        "last_at": activity["last_at"] if activity and activity["last_at"] else None,
        "recent_operations": [dict(row) for row in recent],
    }


def wallet_stats_text(user_id: int) -> str:
    stats = wallet_user_stats(user_id)
    recent = wallet_report_operations(user_id)[:5]
    recent_lines = []
    for operation in recent:
        recent_lines.append(
            f"• {html(operation['type_label'])} — "
            f"<b>{html(_display_amount(operation['amount']))} USDT</b> — "
            f"{html(operation['created_at'] or '—')}"
        )
    recent_text = "\n".join(recent_lines) if recent_lines else "• لا توجد عمليات مسجلة"
    return (
        render_bot_text('fragment.bot.services.user_reporting.py:wallet_stats_text:246','<b>📊 الإحصائيات المالية</b>\n\n💰 الرصيد المتاح: <b>{v0} USDT</b>\n🔒 الرصيد المحجوز: <b>{v1} USDT</b>\n\n<b>📥 المشتريات</b>\n• عدد العمليات: <b>{v2}</b>\n• إجمالي USDT: <b>{v3} USDT</b>\n• إجمالي الريال: <b>{v4} ريال</b>\n\n<b>📤 السحوبات</b>\n• عدد العمليات: <b>{v5}</b>\n• إجمالي المسحوب: <b>{v6} USDT</b>\n\n<b>👥 الإحالات</b>\n• عدد الإحالات: <b>{v7}</b>\n• الإحالات المفعلة: <b>{v8}</b>\n• مكافآت الإحالات: <b>{v9} USDT</b>\n\n✅ العمليات النهائية الناجحة: <b>{v10}</b>\n❌ العمليات النهائية الفاشلة: <b>{v11}</b>\n📈 نسبة نجاح النتائج النهائية: <b>{v12}%</b>\n📅 أول استخدام: <code>{v13}</code>\n📅 آخر عملية: <code>{v14}</code>\n\n<b>🕘 آخر العمليات</b>\n{v15}',[f"{html(_display_amount(stats['available']))}",f"{html(_display_amount(stats['locked']))}",f"{stats['purchase_count']}",f"{html(_display_amount(stats['purchase_usdt']))}",f"{html(format_display_amount(stats['purchase_yer']))}",f"{stats['withdraw_count']}",f"{html(_display_amount(stats['withdraw_usdt']))}",f"{stats['referral_count']}",f"{stats['active_referral_count']}",f"{html(_display_amount(stats['referral_balance']))}",f"{stats['successful_operations']}",f"{stats['failed_operations']}",f"{stats['success_rate']:.1f}",f"{html(stats['first_used_at'] or '—')}",f"{html(stats['last_at'] or '—')}",f'{recent_text}'])
    )


def statistics_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if MINI_APP_URL:
        rows.append(
            [InlineKeyboardButton(render_bot_button('bot.services.user_reporting.py:273:button','🚀 فتح التطبيق الكامل', []), web_app=WebAppInfo(url=MINI_APP_URL))]
        )
    rows.append([btn(render_bot_button('bot.services.user_reporting.py:275:button','📄 تصدير تقرير PDF', []), "wallet_export_pdf", "primary")])
    rows.append([btn(render_bot_button('bot.services.user_reporting.py:276:button','📒 سجل العمليات', []), "wallet_history")])
    rows.append([btn(render_bot_button('bot.services.user_reporting.py:277:button','🏠 القائمة الرئيسية', []), "main_menu")])
    return InlineKeyboardMarkup(rows)


_reporting_legacy_on_callback = on_callback


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    if data == "wallet_referrals":
        await query.answer()
        customer = DBX.get_customer_by_telegram(user.id)
        if not customer or customer["status"] != "ACTIVE":
            await query.edit_message_text(
                render_bot_text('bot.services.user_reporting.py:293:message','⛔ حسابك غير نشط.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.user_reporting.py:295:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        summary = referral_summary(user.id)
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user.id}"
        reward_line = (
            f"🎁 مكافأة الإحالة الحالية: <b>{html(format_display_amount(REFERRAL_REWARD_USDT))} USDT</b>\n"
            if REFERRAL_REWARD_USDT > 0
            else "🎁 يتم تسجيل الإحالات، وقيمة المكافأة الحالية مضبوطة من الإدارة.\n"
        )
        await query.edit_message_text(
            render_bot_text('bot.services.user_reporting.py:306:message','<b>👥 برنامج الإحالات</b>\n\n🔗 رابطك الخاص:\n<code>{v0}</code>\n\n👤 إجمالي الإحالات: <b>{v1}</b>\n✅ الإحالات المفعلة: <b>{v2}</b>\n💰 رصيد الإحالات المكتسب: <b>{v3} USDT</b>\n{v4}\nالإحالة تعتمد بعد  التحقق من بيانات المستخدم وتفعيل  الحساب ', [f'{html(link)}',f"{summary['count']}",f"{summary['active_count']}",f"{html(format_display_amount(summary['balance']))}",f'{reward_line}']),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(render_bot_button('bot.services.user_reporting.py:317:button','🔗 فتح رابط الإحالة', []), url=link)],
                    [btn(render_bot_button('bot.services.user_reporting.py:318:button','📊 الإحصائيات', []), "wallet_stats")],
                    [btn(render_bot_button('bot.services.user_reporting.py:319:button','🏠 القائمة الرئيسية', []), "main_menu")],
                ]
            ),
        )
        return

    if data == "wallet_stats":
        await query.answer()
        customer = DBX.get_customer_by_telegram(user.id)
        if not DBX.is_admin(user.id) and (not customer or customer["status"] != "ACTIVE"):
            await query.edit_message_text(
                render_bot_text('bot.services.user_reporting.py:329:message','⛔ حسابك غير نشط.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.user_reporting.py:331:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        await query.edit_message_text(
            render_bot_text('bot.services.user_reporting.py:334:message','{v0}', [str(wallet_stats_text(user.id))]),
            parse_mode=ParseMode.HTML,
            reply_markup=statistics_keyboard(),
        )
        return

    if data == "wallet_export_pdf":
        await query.answer("جاري إنشاء التقرير")
        customer = DBX.get_customer_by_telegram(user.id)
        if not DBX.is_admin(user.id) and (not customer or customer["status"] != "ACTIVE"):
            await query.edit_message_text(
                render_bot_text('bot.services.user_reporting.py:345:message','⛔ حسابك غير نشط.', []),
                reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.user_reporting.py:347:button','🏠 القائمة الرئيسية', []), "main_menu")]]),
            )
            return
        progress = await query.edit_message_text(render_bot_text('bot.services.user_reporting.py:350:message','⏳ جاري إنشاء تقرير PDF باللغة العربية...', []))
        path = None
        try:
            path = await asyncio.to_thread(generate_user_pdf_report, user.id)
            with path.open("rb") as handle:
                await context.bot.send_document(
                    chat_id=user.id,
                    document=handle,
                    filename=path.name,
                    caption=render_bot_text('bot.services.user_reporting.py:355:message','📄 تقرير العمليات المالية', []),
                )
            await progress.edit_text(
                "✅ تم إنشاء التقرير وإرساله بنجاح.",
                reply_markup=statistics_keyboard(),
            )
            DBX.audit(user.id, actor_name(user), "EXPORT_USER_PDF", "report", path.name)
        except Exception as exc:
            reference = wallet_ref("PDF")
            await send_safe_wallet_error(
                query,
                context,
                reference_id=reference,
                event_type="فشل إنشاء تقرير PDF",
                exc=exc,
                telegram_user_id=user.id,
            )
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("تعذر حذف ملف التقرير المؤقت %s", path)
        return

    await _reporting_legacy_on_callback(update, context)


_export_globals(globals())

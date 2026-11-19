"""Self-registration, numeric CAPTCHA, pending accounts, and referral activation.

The module owns the complete new-user entry flow while delegating unrelated
customer and administrator actions to the existing handler chain.
"""
from __future__ import annotations

import hashlib
import io
import random
import re
import secrets
import sqlite3
import time
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.ui.design_render import render_bot_text, render_bot_button
from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

CAPTCHA_ENABLED = env("CAPTCHA_ENABLED", "true").lower() == "true"
CAPTCHA_WIDTH = int(env("CAPTCHA_WIDTH", "260"))
CAPTCHA_HEIGHT = int(env("CAPTCHA_HEIGHT", "100"))
CAPTCHA_DIGITS = 4
CAPTCHA_MAX_AGE_SECONDS = int(env("CAPTCHA_MAX_AGE_SECONDS", "600"))
REFERRAL_REWARD_USDT = Decimal(env("REFERRAL_REWARD_USDT", "0"))


def _registration_schema() -> None:
    with DBX.connect() as con:
        # Multiple pending users must be allowed to have an empty phone number.
        con.execute("DROP INDEX IF EXISTS idx_customers_phone_primary")
        con.execute("DROP INDEX IF EXISTS idx_customers_phone_alt")
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone_primary_nonempty "
                "ON customers(phone_primary) WHERE phone_primary IS NOT NULL AND phone_primary != ''"
            )
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone_alt_nonempty "
                "ON customers(phone_alt) WHERE phone_alt IS NOT NULL AND phone_alt != ''"
            )
        except sqlite3.IntegrityError:
            log.warning("تعذر إنشاء فهارس الهواتف الجزئية بسبب بيانات مكررة قديمة")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_registrations (
                telegram_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'NEW',
                captcha_verified_at TEXT,
                requested_at TEXT,
                activated_at TEXT,
                referred_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_telegram_id INTEGER NOT NULL,
                referred_telegram_id INTEGER UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                reward_usdt TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL,
                activated_at TEXT,
                rewarded_at TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_telegram_id, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_referrals_status ON referrals(status, id)")
        con.commit()


_registration_schema()


def get_registration(telegram_id: int) -> Optional[sqlite3.Row]:
    with DBX.connect() as con:
        return con.execute(
            "SELECT * FROM user_registrations WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()


def _upsert_registration(telegram_id: int, **fields: Any) -> None:
    now = now_str()
    with DBX.connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO user_registrations(
                telegram_id, status, created_at, updated_at
            ) VALUES (?, 'NEW', ?, ?)
            """,
            (int(telegram_id), now, now),
        )
        if fields:
            fields["updated_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in fields)
            con.execute(
                f"UPDATE user_registrations SET {assignments} WHERE telegram_id = ?",
                list(fields.values()) + [int(telegram_id)],
            )
        con.commit()


def capture_referral(referred_id: int, start_payload: str) -> str:
    """Persist a referral without allowing referral failures to block /start."""
    payload = (start_payload or "").strip()
    match = re.fullmatch(r"ref_(\d+)", payload)
    if not match:
        return "NO_REFERRAL"

    referrer_id = int(match.group(1))
    referred_id = int(referred_id)
    if referrer_id == referred_id:
        log.info("Referral ignored: self referral user=%s", referred_id)
        return "SELF_REFERRAL"

    try:
        with DBX.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            known_referrer = con.execute(
                "SELECT 1 FROM bot_users WHERE telegram_id = ? "
                "UNION SELECT 1 FROM customers WHERE telegram_id = ? LIMIT 1",
                (referrer_id, referrer_id),
            ).fetchone()
            if not known_referrer:
                con.rollback()
                log.warning(
                    "Referral ignored: unknown referrer=%s referred=%s",
                    referrer_id,
                    referred_id,
                )
                return "UNKNOWN_REFERRER"

            existing = con.execute(
                "SELECT referrer_telegram_id, status FROM referrals WHERE referred_telegram_id = ?",
                (referred_id,),
            ).fetchone()
            if existing:
                con.rollback()
                return "ALREADY_CAPTURED"

            con.execute(
                """
                INSERT INTO referrals(
                    referrer_telegram_id, referred_telegram_id, status,
                    reward_usdt, created_at
                ) VALUES (?, ?, 'PENDING', ?, ?)
                """,
                (referrer_id, referred_id, q8(REFERRAL_REWARD_USDT), now_str()),
            )
            con.execute(
                """
                INSERT INTO user_registrations(
                    telegram_id, status, referred_by, created_at, updated_at
                ) VALUES (?, 'NEW', ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    referred_by = COALESCE(user_registrations.referred_by, excluded.referred_by),
                    updated_at = excluded.updated_at
                """,
                (referred_id, referrer_id, now_str(), now_str()),
            )
            con.commit()
        log.info(
            "Referral captured referrer=%s referred=%s reward=%s",
            referrer_id,
            referred_id,
            q8(REFERRAL_REWARD_USDT),
        )
        return "CAPTURED"
    except sqlite3.IntegrityError:
        # A concurrent /start can race with the first insert. Treat it as idempotent.
        log.info("Referral already captured concurrently referred=%s", referred_id)
        return "ALREADY_CAPTURED"



def referral_summary(telegram_id: int) -> dict[str, Any]:
    with DBX.connect() as con:
        rows = con.execute(
            "SELECT status, reward_usdt FROM referrals WHERE referrer_telegram_id = ?",
            (int(telegram_id),),
        ).fetchall()
    completed = [row for row in rows if str(row["status"]).upper() == "COMPLETED"]
    balance = sum(
        (wallet_decimal(row["reward_usdt"] or 0) for row in completed),
        Decimal("0"),
    )
    return {
        "count": len(rows),
        "active_count": len(completed),
        "balance": wallet_decimal(balance),
    }


async def complete_referral_after_activation(
    context: ContextTypes.DEFAULT_TYPE,
    referred_id: int,
) -> Decimal:
    """Complete and reward one referral atomically after account activation.

    The referral status, wallet balance, and ledger row are committed in one
    SQLite transaction. A crash can therefore never credit the same referral
    twice while leaving its status pending.
    """
    referred_id = int(referred_id)
    now = now_str()
    referrer_id: Optional[int] = None
    reward = Decimal("0")

    with DBX.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM referrals WHERE referred_telegram_id = ?",
            (referred_id,),
        ).fetchone()
        if not row or str(row["status"]).upper() != "PENDING":
            con.rollback()
            return Decimal("0")

        referrer_id = int(row["referrer_telegram_id"])
        reward = wallet_decimal(row["reward_usdt"] or REFERRAL_REWARD_USDT)
        customer = con.execute(
            "SELECT id FROM customers WHERE telegram_id = ? LIMIT 1",
            (referrer_id,),
        ).fetchone()
        customer_id = int(customer["id"]) if customer else None

        if reward > 0:
            wallet = con.execute(
                "SELECT * FROM user_wallets WHERE telegram_user_id = ?",
                (referrer_id,),
            ).fetchone()
            if not wallet:
                con.execute(
                    """
                    INSERT INTO user_wallets(
                        telegram_user_id, customer_id, available_balance, locked_balance,
                        total_deposited, total_withdrawn, created_at, updated_at
                    ) VALUES (?, ?, '0', '0', '0', '0', ?, ?)
                    """,
                    (referrer_id, customer_id, now, now),
                )
                wallet = con.execute(
                    "SELECT * FROM user_wallets WHERE telegram_user_id = ?",
                    (referrer_id,),
                ).fetchone()

            available_before = wallet_decimal(wallet["available_balance"])
            locked_before = wallet_decimal(wallet["locked_balance"])
            available_after = available_before + reward
            con.execute(
                "UPDATE user_wallets SET available_balance = ?, updated_at = ? WHERE telegram_user_id = ?",
                (q8(available_after), now, referrer_id),
            )
            con.execute(
                """
                INSERT INTO wallet_ledger(
                    ledger_code, telegram_user_id, customer_id, transaction_type, status,
                    amount_usdt, available_before, available_after,
                    locked_before, locked_after, notes, created_at, completed_at
                ) VALUES (?, ?, ?, 'REFERRAL_REWARD', 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet_ref("RF"),
                    referrer_id,
                    customer_id,
                    q8(reward),
                    q8(available_before),
                    q8(available_after),
                    q8(locked_before),
                    q8(locked_before),
                    f"Referral reward for Telegram ID {referred_id}",
                    now,
                    now,
                ),
            )

        cursor = con.execute(
            """
            UPDATE referrals
            SET status = 'COMPLETED', activated_at = ?, rewarded_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (now, now, int(row["id"])),
        )
        if cursor.rowcount != 1:
            con.rollback()
            return Decimal("0")
        con.commit()

    log.info(
        "Referral completed referrer=%s referred=%s reward=%s",
        referrer_id,
        referred_id,
        q8(reward),
    )
    if reward > 0 and referrer_id is not None:
        try:
            await context.bot.send_message(
                referrer_id,
                render_bot_text('bot.services.registration_flow.py:314:message','🎉 <b>تم تفعيل إحدى إحالاتك</b>\n\nأضيفت مكافأة <b>{v0} USDT</b> إلى رصيدك.', [f'{html(format_display_amount(reward))}']),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            # Notification failure does not roll back an already committed reward.
            log.exception("Failed to notify referral owner=%s", referrer_id)
    return reward


def _admin_contact_url() -> str:
    value = (ADMIN_CONTACT or "").strip()
    if value.startswith("https://t.me/") or value.startswith("tg://"):
        return value
    if value.startswith("@") and len(value) > 1:
        return f"https://t.me/{value[1:]}"
    return f"tg://user?id={int(ADMIN_ID)}"


def pending_welcome_text(user: Any) -> str:
    return (
        render_bot_text('fragment.bot.services.registration_flow.py:pending_welcome_text:337','<b>مرحبًا بك في {v0}</b>\n\nمن خلال البوت يمكنك شراء USDT، إدارة رصيدك الداخلي، وطلب تحويل USDT إلى شبكة الاستلام المدعومة.\n\n🔐 <b>الحساب بانتظار تفعيل الإدارة</b>\nيتطلب استخدام الخدمات المالية تفعيل الحساب بعد التحقق من بيانات العميل. ستقوم الإدارة بإضافة بياناتك الأساسية وربطها بحساب تيليجرام ثم تفعيل الحساب.\n\nللاستمرار في استخدام الخدمة، تواصل مع الإدارة من الزر أدناه.',[f'{html(APP_NAME)}'])
    )


def pending_welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(render_bot_button('bot.services.registration_flow.py:349:button','📩 التواصل مع الإدارة', []), url=_admin_contact_url())]]
    )


def ensure_pending_customer(user: Any) -> sqlite3.Row:
    current = DBX.get_customer_by_telegram(int(user.id))
    if current:
        if current["status"] not in {"ACTIVE", "SUSPENDED", "PENDING"}:
            DBX.update_customer(int(current["id"]), status="PENDING")
        return DBX.get_customer(int(current["id"]))
    with DBX.connect() as con:
        cur = con.execute(
            """
            INSERT INTO customers(
                full_name, phone_primary, phone_alt, telegram_id,
                telegram_username, status, notes, added_by, created_at, updated_at
            ) VALUES (?, '', '', ?, ?, 'PENDING', ?, NULL, ?, ?)
            """,
            (
                (getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)).strip(),
                int(user.id), getattr(user, "username", None),
                "تم إنشاء الحساب تلقائيًا بعد اجتياز CAPTCHA؛ البيانات بانتظار الإدارة.",
                now_str(), now_str(),
            ),
        )
        con.commit()
        return con.execute("SELECT * FROM customers WHERE id = ?", (int(cur.lastrowid),)).fetchone()


def _registration_captcha_font_candidates() -> list[Path]:
    configured = env("CAPTCHA_FONT_PATH", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ]
    )
    return candidates


def _registration_captcha_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _registration_captcha_font_candidates():
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def build_numeric_captcha(code: str) -> io.BytesIO:
    width, height = CAPTCHA_WIDTH, CAPTCHA_HEIGHT
    background = tuple(random.randint(218, 248) for _ in range(3))
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    # Light random noise and lines.
    for _ in range(6):
        points = [
            (random.randint(0, width), random.randint(0, height)),
            (random.randint(0, width), random.randint(0, height)),
        ]
        color = tuple(random.randint(80, 180) for _ in range(3))
        draw.line(points, fill=color, width=random.randint(1, 3))
    for _ in range(160):
        x, y = random.randint(0, width - 1), random.randint(0, height - 1)
        color = tuple(random.randint(110, 210) for _ in range(3))
        draw.ellipse((x, y, x + random.randint(1, 3), y + random.randint(1, 3)), fill=color)

    font = _registration_captcha_font(58)
    slot = width // CAPTCHA_DIGITS
    for index, digit in enumerate(code):
        layer = Image.new("RGBA", (slot + 30, height), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        color = tuple(random.randint(20, 95) for _ in range(3)) + (255,)
        bbox = layer_draw.textbbox((0, 0), digit, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        layer_draw.text(
            ((layer.width - text_w) / 2, (height - text_h) / 2 - bbox[1]),
            digit,
            font=font,
            fill=color,
        )
        layer = layer.rotate(random.uniform(-12, 12), resample=Image.Resampling.BICUBIC, expand=False)
        image.paste(layer, (index * slot - 5, 0), layer)

    image = image.filter(ImageFilter.SMOOTH)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.name = "captcha.png"
    buffer.seek(0)
    return buffer


def _captcha_hash(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


async def send_registration_captcha(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    error: str = "",
) -> None:
    """Create and send a fresh four-digit CAPTCHA for the current user."""
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    if user is None:
        raise RuntimeError("تعذر تحديد مستخدم CAPTCHA")

    code = f"{secrets.randbelow(10000):04d}"
    salt = secrets.token_hex(8)
    set_state(
        context,
        "await_registration_captcha",
        captcha_hash=_captcha_hash(code, salt),
        captcha_salt=salt,
        generated_at=int(time.time()),
    )
    caption = (
        "🤖 <b>التحقق من المستخدم</b>\n\n"
        "اكتب الأرقام الأربعة الظاهرة في الصورة فقط."
    )
    if error:
        caption = f"❌ {html(error)}\n\n" + caption

    image = build_numeric_captcha(code)
    try:
        await context.bot.send_photo(
            chat_id=int(user.id),
            photo=image,
            caption=render_bot_text('bot.services.registration_flow.py:479:message','{v0}', [str(caption)]),
            parse_mode=ParseMode.HTML,
        )
        log.info("Registration CAPTCHA sent user=%s", user.id)
    finally:
        image.close()


async def finish_registration(user: Any, context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Row:
    customer = ensure_pending_customer(user)
    _upsert_registration(
        int(user.id),
        status="ACTIVE" if customer["status"] == "ACTIVE" else "PENDING",
        captcha_verified_at=now_str(),
        requested_at=now_str(),
    )
    set_state(context, None)
    log.info(
        "Registration completed user=%s customer=%s status=%s",
        user.id,
        customer["id"],
        customer["status"],
    )
    return customer


_registration_legacy_show_main = show_main
_registration_legacy_cmd_start = cmd_start
_registration_legacy_on_callback = on_callback
_registration_legacy_on_message = on_message


def _start_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    args = getattr(context, "args", None) or []
    if args:
        return str(args[0]).strip()
    message = getattr(update, "effective_message", None)
    text = (getattr(message, "text", None) or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


async def show_main(target: Any, context: ContextTypes.DEFAULT_TYPE):
    """Route a user through subscription, CAPTCHA, activation, then the main menu."""
    user = target.from_user if hasattr(target, "from_user") else target.effective_user
    if user is None:
        raise RuntimeError("تعذر تحديد مستخدم /start")

    register_bot_user(user)
    log.info("Start flow stage=subscription user=%s", user.id)
    if not await ensure_required_subscription(target, context, user.id):
        log.info("Start flow paused for required subscription user=%s", user.id)
        return

    if DBX.is_admin(user.id):
        log.info("Start flow stage=admin_menu user=%s", user.id)
        await _registration_legacy_show_main(target, context)
        return

    customer = DBX.get_customer_by_telegram(user.id)
    if customer and customer["status"] == "ACTIVE":
        _upsert_registration(user.id, status="ACTIVE", activated_at=now_str())
        log.info("Start flow stage=active_menu user=%s customer=%s", user.id, customer["id"])
        await _registration_legacy_show_main(target, context)
        return

    if customer and customer["status"] == "SUSPENDED":
        log.warning("Suspended user attempted start user=%s customer=%s", user.id, customer["id"])
        await send_or_edit(
            target,
            render_bot_text('bot.services.registration_flow.py:550:message','⛔ <b>حسابك موقوف حاليًا</b>\n\nتواصل مع الإدارة لمراجعة حالة الحساب.', []),
            pending_welcome_kb(),
        )
        return

    registration = get_registration(user.id)
    verified = bool(registration and registration["captcha_verified_at"])
    if CAPTCHA_ENABLED and not verified:
        log.info("Start flow stage=captcha user=%s", user.id)
        await send_registration_captcha(target, context)
        return

    if not verified:
        await finish_registration(user, context)
    else:
        ensure_pending_customer(user)
        set_state(context, None)

    log.info("Start flow stage=pending_welcome user=%s", user.id)
    await send_or_edit(target, render_bot_text('bot.services.registration_flow.py:571:message','{v0}', [str(pending_welcome_text(user))]), pending_welcome_kb())


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stable /start entry point with an isolated referral stage.

    Every step is inside the same guarded flow so database/state failures that
    happen before subscription or CAPTCHA cannot escape into a wallet-specific
    error message. A referral failure is reported independently and never blocks
    the new-user registration sequence.
    """
    user = getattr(update, "effective_user", None)
    reference = wallet_ref("REG")

    try:
        set_state(context, None)
        if user is None:
            raise RuntimeError("وصل أمر /start بدون مستخدم فعّال")

        first_seen = register_bot_user(user)
        log.info(
            "Command /start user=%s username=%s first_seen=%s reference=%s",
            user.id,
            getattr(user, "username", None),
            first_seen,
            reference,
        )

        payload = _start_payload(update, context)
        if payload:
            try:
                result = capture_referral(user.id, payload)
                log.info(
                    "Referral start payload result=%s user=%s payload=%s",
                    result,
                    user.id,
                    payload,
                )
            except Exception:
                # Registration must continue even if referral persistence has a problem.
                log.exception("Referral capture failed but start will continue user=%s", user.id)
                try:
                    await send_technical_log(
                        context,
                        reference_id=reference,
                        event_type="Referral capture failure",
                        details=traceback.format_exc(),
                        severity="ERROR",
                        telegram_user_id=user.id,
                    )
                except Exception:
                    log.exception("Failed to report referral capture error user=%s", user.id)

        await show_main(update, context)
    except TelegramError:
        # Let the global handler record Telegram transport/API failures with the
        # original traceback. It now returns a registration-specific message.
        log.exception(
            "Telegram error during /start user=%s reference=%s",
            getattr(user, "id", None),
            reference,
        )
        raise
    except Exception:
        details = traceback.format_exc()
        log.exception(
            "Registration start flow failed user=%s reference=%s",
            getattr(user, "id", None),
            reference,
        )
        try:
            await send_technical_log(
                context,
                reference_id=reference,
                event_type="Registration start failure",
                details=details,
                severity="ERROR",
                telegram_user_id=getattr(user, "id", None),
            )
        except Exception:
            log.exception(
                "Failed to report registration start failure user=%s",
                getattr(user, "id", None),
            )

        message = getattr(update, "effective_message", None)
        if message:
            try:
                await message.reply_text(
                    render_bot_text('bot.services.registration_flow.py:659:message','⚠️ تعذر بدء التسجيل مؤقتًا بسبب مشكلة تقنية.\n\nأرسل /start مرة أخرى، وإذا استمرت المشكلة فتواصل مع الإدارة.\nرقم المرجع: <code>{v0}</code>', [f'{html(reference)}']),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                log.exception(
                    "Could not send registration failure message user=%s reference=%s",
                    getattr(user, "id", None),
                    reference,
                )



async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user

    if data == "registration_new_captcha":
        await query.answer()
        await send_registration_captcha(query, context)
        return

    if data.startswith("cust_activate:"):
        if not DBX.is_admin(user.id) or not DBX.has_permission(user.id, "customers_status"):
            await query.answer("لا تملك صلاحية تفعيل العملاء", show_alert=True)
            return
        customer_id = int(data.split(":", 1)[1])
        row = DBX.get_customer(customer_id)
        if not row:
            await query.answer("العميل غير موجود", show_alert=True)
            return
        if not normalize_phone(row["phone_primary"]):
            await query.answer("أدخل الرقم الأساسي للعميل قبل التفعيل", show_alert=True)
            await query.edit_message_text(
                render_bot_text('bot.services.registration_flow.py:695:message','{v0}\n\n⚠️ <b>يجب إضافة الرقم الأساسي قبل تفعيل الحساب.</b>', [str(customer_detail_text(row))]),
                parse_mode=ParseMode.HTML,
                reply_markup=customer_actions_kb(row, user.id),
            )
            return
        await query.answer("تم تفعيل الحساب")
        DBX.update_customer(customer_id, status="ACTIVE")
        activated = DBX.get_customer(customer_id)
        log.info(
            "Customer activated admin=%s customer=%s telegram_id=%s",
            user.id,
            customer_id,
            activated["telegram_id"],
        )
        telegram_id = activated["telegram_id"]
        reward = Decimal("0")
        if telegram_id is not None:
            _upsert_registration(
                int(telegram_id),
                status="ACTIVE",
                activated_at=now_str(),
            )
            try:
                reward = await complete_referral_after_activation(context, int(telegram_id))
            except Exception:
                log.exception(
                    "Referral completion failed after customer activation customer=%s telegram_id=%s",
                    customer_id,
                    telegram_id,
                )
                try:
                    await send_technical_log(
                        context,
                        reference_id=wallet_ref("REF"),
                        event_type="Referral completion failure after activation",
                        details=traceback.format_exc(),
                        severity="ERROR",
                        telegram_user_id=int(telegram_id),
                    )
                except Exception:
                    log.exception("Failed to report referral completion error telegram_id=%s", telegram_id)
                reward = Decimal("0")
            try:
                await context.bot.send_message(
                    int(telegram_id),
                    render_bot_text('bot.services.registration_flow.py:739:message','✅ <b>تم تفعيل حسابك</b>\n\nيمكنك الآن استخدام خدمات البوت.', []),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[btn(render_bot_button('bot.services.registration_flow.py:743:button','🏠 فتح القائمة الرئيسية', []), "main_menu", "success")]]),
                )
            except Exception:
                log.exception("failed to notify activated customer=%s", telegram_id)
        DBX.audit(user.id, actor_name(user), "ACTIVATE_CUSTOMER", "customer", customer_id, "registration activation")
        note = f"\n\n🎁 مكافأة الإحالة: <b>{html(format_display_amount(reward))} USDT</b>" if reward > 0 else ""
        await query.edit_message_text(
            render_bot_text('bot.services.registration_flow.py:749:message','{v0}{v1}', [str(customer_detail_text(activated)),str(note)]),
            parse_mode=ParseMode.HTML,
            reply_markup=customer_actions_kb(activated, user.id),
        )
        return

    await _registration_legacy_on_callback(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_bot_user(user)
    state, payload = current_state(context)
    if state != "await_registration_captcha":
        await _registration_legacy_on_message(update, context)
        return

    text_value = (update.effective_message.text or "").strip().translate(ARABIC_DIGITS)
    generated_at = int(payload.get("generated_at") or 0)
    expired = not generated_at or int(time.time()) - generated_at > CAPTCHA_MAX_AGE_SECONDS
    valid_shape = bool(re.fullmatch(r"\d{4}", text_value))
    expected = str(payload.get("captcha_hash") or "")
    salt = str(payload.get("captcha_salt") or "")
    valid = valid_shape and not expired and secrets.compare_digest(_captcha_hash(text_value, salt), expected)
    if not valid:
        DBX.security(user.id, user.username, "captcha_failed", "LOW", "فشل أو انتهاء CAPTCHA")
        log.warning("Registration CAPTCHA failed user=%s expired=%s", user.id, expired)
        await send_registration_captcha(
            update,
            context,
            error="الرمز غير صحيح أو انتهت صلاحيته. تم إنشاء صورة جديدة.",
        )
        return

    await finish_registration(user, context)
    log.info("Registration CAPTCHA verified user=%s", user.id)
    await update.effective_message.reply_text(
        render_bot_text('bot.services.registration_flow.py:786:message','{v0}', [str(pending_welcome_text(user))]),
        parse_mode=ParseMode.HTML,
        reply_markup=pending_welcome_kb(),
    )


def customer_status_label(status: str) -> str:
    labels = {
        "ACTIVE": "🟢 نشط",
        "PENDING": "⏳ بانتظار التفعيل",
        "SUSPENDED": "⛔ موقوف",
    }
    return labels.get(str(status or "").upper(), str(status or "—"))


def customers_list_kb(rows: list[sqlite3.Row], page: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    current = rows[page * page_size:(page + 1) * page_size]
    buttons: list[list[InlineKeyboardButton]] = []
    icons = {"ACTIVE": "🟢", "PENDING": "⏳", "SUSPENDED": "⛔"}
    for row in current:
        icon = icons.get(str(row["status"]), "▫️")
        phone = row["phone_primary"] or "بدون رقم"
        buttons.append([btn(render_bot_button('bot.services.registration_flow.py:811:button','{v0} {v1} | {v2}', [f'{icon}',f"{row['full_name']}",f'{phone}']), f"cust_view:{row['id']}", "primary")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn(render_bot_button('bot.services.registration_flow.py:814:button','⬅️ السابق', []), f"cust_page:{page - 1}"))
    if page + 1 < pages:
        nav.append(btn(render_bot_button('bot.services.registration_flow.py:816:button','التالي ➡️', []), f"cust_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn(render_bot_button('bot.services.registration_flow.py:819:button','🏠 لوحة الأدمن', []), "admin_panel")])
    return InlineKeyboardMarkup(buttons)


_export_globals(globals())

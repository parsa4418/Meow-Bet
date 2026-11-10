# -*- coding: utf-8 -*-
"""MEOWBET v9 - non-wagering group games, virtual points, Meowie auto-credit and reviewed point withdrawals."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import signal
import secrets
import string
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    TELETHON_OK = True
except ImportError:
    TelegramClient = None
    events = None
    StringSession = None
    TELETHON_OK = False

import asyncpg
from aiohttp import ClientSession, ClientTimeout, web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError, BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PORT = int(os.getenv("PORT", "10000") or 10000)
DAILY_BONUS = max(1, int(os.getenv("DAILY_BONUS", "100") or 100))
REFERRER_BONUS = max(0, int(os.getenv("REFERRER_BONUS", "150") or 150))
REFERRED_BONUS = max(0, int(os.getenv("REFERRED_BONUS", "75") or 75))
QUIZ_PRIZE = max(1, int(os.getenv("QUIZ_PRIZE", "50") or 50))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()
DB_POOL_MAX = max(2, int(os.getenv("DB_POOL_MAX", "6") or 6))
TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "").strip()
# Bank-bot usernames are fixed in code; no MEOWIE_BANK_BOT env var needed.
MEOWIE_BANK_BOTS = {
    "meowieqbot",
    "meowieeqbot",
    "meowieeeqbot",
    "meowieqivbot",
    "meowieqvbot",
}
MEOWIE_BANK_ACCOUNT = re.sub(r"\D", "", os.getenv("MEOWIE_BANK_ACCOUNT", ""))
DEPOSIT_WINDOW_MINUTES = max(5, min(1440, int(os.getenv("DEPOSIT_WINDOW_MINUTES", "60") or 60)))
BANK_FILTER_RE = os.getenv("BANK_FILTER_RE", r"(?s)کارت به کارت میویی.*تراکنش موفقیت\s*آمیز")
BANK_AMOUNT_RE = os.getenv("BANK_AMOUNT_RE", r"(?m)^\W*مبلغ دریافت شده\s*:\s*(\d[\d,٬،.]*)")
BANK_SENDER_RE = os.getenv("BANK_SENDER_RE", r"(?m)^\W*شماره حساب واریز\s*کننده\s*:\s*(\d{12})(?!\d)")
# Custom emoji are attempted automatically when configured. If Telegram rejects
# them for this bot, outgoing messages/keyboards are retried with standard emoji.
CUSTOM_EMOJI_IDS: dict[str, str] = {}
EMOJI_KEYS = {
    "HOME": "🏠", "BALANCE": "💠", "GAMES": "🎮", "DAILY": "🎁", "TOP": "🏆",
    "PROFILE": "👤", "HISTORY": "📜", "REFERRAL": "👥", "GIFT": "🎁", "HELP": "❔",
    "ADMIN": "🛠", "BACK": "⬅️", "CHANNEL": "📢", "CHECK": "✅", "CAT": "🐾",
    "POINTS": "💠", "QUIZ": "🧠", "DICE": "🎲", "DART": "🎯", "FOOTBALL": "⚽",
    "SUCCESS": "🎉", "RANK": "🏆", "WARNING": "⚠️",
}


def custom_emoji_id(key: str) -> Optional[str]:
    value = CUSTOM_EMOJI_IDS.get(key.upper(), "")
    return value if re.fullmatch(r"\d{5,30}", value or "") else None


def _fallback_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    # Keep the visible fallback character while removing Telegram's custom tag.
    return re.sub(r'<tg-emoji\s+emoji-id="\d+">(.*?)</tg-emoji>', r"\1", text, flags=re.S)


def _fallback_markup(markup):
    if markup is None:
        return None
    try:
        data = markup.to_dict()
        if "inline_keyboard" in data:
            changed = False
            for row in data["inline_keyboard"]:
                for button in row:
                    changed = button.pop("icon_custom_emoji_id", None) is not None or changed
            return InlineKeyboardMarkup.de_json(data, None) if changed else markup
        if "keyboard" in data:
            changed = False
            for row in data["keyboard"]:
                for button in row:
                    if isinstance(button, dict):
                        changed = button.pop("icon_custom_emoji_id", None) is not None or changed
            return ReplyKeyboardMarkup.de_json(data, None) if changed else markup
    except Exception:
        log.exception("Could not strip custom emoji from keyboard markup")
    return markup


async def safe_reply_text(message, text: str, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except BadRequest:
        markup = kwargs.get("reply_markup")
        plain = _fallback_text(text)
        plain_markup = _fallback_markup(markup)
        if plain == text and plain_markup is markup:
            raise
        retry_kwargs = dict(kwargs)
        if markup is not None:
            retry_kwargs["reply_markup"] = plain_markup
        return await message.reply_text(plain, **retry_kwargs)


async def safe_edit_message_text(query, text: str, **kwargs):
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest:
        markup = kwargs.get("reply_markup")
        plain = _fallback_text(text)
        plain_markup = _fallback_markup(markup)
        if plain == text and plain_markup is markup:
            raise
        retry_kwargs = dict(kwargs)
        if markup is not None:
            retry_kwargs["reply_markup"] = plain_markup
        return await query.edit_message_text(plain, **retry_kwargs)


async def safe_send_message(bot, chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except BadRequest:
        markup = kwargs.get("reply_markup")
        plain = _fallback_text(text)
        plain_markup = _fallback_markup(markup)
        if plain == text and plain_markup is markup:
            raise
        retry_kwargs = dict(kwargs)
        if markup is not None:
            retry_kwargs["reply_markup"] = plain_markup
        return await bot.send_message(chat_id=chat_id, text=plain, **retry_kwargs)


def emoji_html(key: str, fallback: Optional[str] = None) -> str:
    fallback = fallback or EMOJI_KEYS.get(key.upper(), "✨")
    emoji_id = custom_emoji_id(key)
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>' if emoji_id else fallback


def _button_text(label: str, key: str) -> str:
    return label if custom_emoji_id(key) else f"{EMOJI_KEYS.get(key, '✨')} {label}"


def inline_button(label: str, key: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label if custom_emoji_id(key) else _button_text(label, key),
                                callback_data=callback_data, icon_custom_emoji_id=custom_emoji_id(key))


def reply_button(label: str, key: str) -> KeyboardButton:
    return KeyboardButton(_button_text(label, key), icon_custom_emoji_id=custom_emoji_id(key))


if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID must be your numeric Telegram user ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("meowbet")
pool: asyncpg.Pool

QUESTIONS = [
    ("پایتخت فرانسه کدام شهر است؟", ["پاریس"]),
    ("بزرگ‌ترین سیاره منظومه شمسی چیست؟", ["مشتری"]),
    ("حاصل ۹ × ۸ چند می‌شود؟", ["72", "۷۲"]),
    ("کدام حیوان به سلطان جنگل معروف است؟", ["شیر"]),
    ("نویسنده شاهنامه چه کسی است؟", ["فردوسی"]),
    ("کدام سیاره به سیاره سرخ معروف است؟", ["مریخ"]),
    ("آب در فشار معمولی در چند درجه می‌جوشد؟", ["100", "۱۰۰", "صد"]),
    ("واحد پول ژاپن چیست؟", ["ین"]),
]


def fmt(n: int) -> str:
    return f"{int(n):,}"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower().replace("ي", "ی").replace("ك", "ک"))


def home_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [reply_button("منوی اصلی", "HOME"), reply_button("موجودی", "BALANCE")],
        [reply_button("بازی‌های گروه", "GAMES"), reply_button("جایزه روزانه", "DAILY")],
        [reply_button("رتبه‌بندی", "TOP"), reply_button("پروفایل", "PROFILE")],
        [reply_button("دعوت دوستان", "REFERRAL"), reply_button("تاریخچه", "HISTORY")],
        [reply_button("ساخت لینک هدیه", "GIFT"), reply_button("راهنما", "HELP")],
        [reply_button("واریز امتیاز", "POINTS"), reply_button("درخواست برداشت", "POINTS")],
    ]
    if uid == ADMIN_ID:
        rows.append([reply_button("پنل مدیریت", "ADMIN")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_inline(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [inline_button("موجودی و کیف امتیاز", "BALANCE", "m:balance"), inline_button("بازی‌های گروه", "GAMES", "m:games")],
        [inline_button("جایزه روزانه", "DAILY", "m:daily"), inline_button("رتبه‌بندی", "TOP", "m:top")],
        [inline_button("پروفایل", "PROFILE", "m:profile"), inline_button("تاریخچه", "HISTORY", "m:history")],
        [inline_button("دعوت دوستان", "REFERRAL", "m:ref"), inline_button("لینک هدیه", "GIFT", "m:gift")],
        [inline_button("واریز امتیاز", "POINTS", "m:deposit"), inline_button("درخواست برداشت", "POINTS", "m:withdraw")],
        [inline_button("راهنما", "HELP", "m:help")],
    ]
    if uid == ADMIN_ID:
        rows.append([inline_button("پنل مدیریت", "ADMIN", "m:admin")])
    return InlineKeyboardMarkup(rows)


async def init_db():
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS mb_users (
          user_id BIGINT PRIMARY KEY, username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT 'کاربر',
          points BIGINT NOT NULL DEFAULT 0 CHECK(points >= 0), referred_by BIGINT, referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
          games_count INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_daily DATE
        );
        CREATE TABLE IF NOT EXISTS mb_ledger (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES mb_users(user_id), delta BIGINT NOT NULL,
          reason TEXT NOT NULL, ref TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS mb_games (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES mb_users(user_id), game TEXT NOT NULL,
          result TEXT NOT NULL, points BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS mb_groups (
          chat_id BIGINT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
          active BOOLEAN NOT NULL DEFAULT TRUE, last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS mb_gifts (
          code TEXT PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES mb_users(user_id), points_each BIGINT NOT NULL,
          capacity INT NOT NULL, claimed INT NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS mb_gift_claims (
          code TEXT NOT NULL REFERENCES mb_gifts(code), user_id BIGINT NOT NULL REFERENCES mb_users(user_id),
          points BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS mb_quiz_sessions (
          chat_id BIGINT PRIMARY KEY, question TEXT NOT NULL, answers TEXT[] NOT NULL,
          prize BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mb_emoji_settings (
          emoji_key TEXT PRIMARY KEY, custom_emoji_id TEXT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS mb_ledger_user_idx ON mb_ledger(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS mb_games_user_idx ON mb_games(user_id, id DESC);
        CREATE TABLE IF NOT EXISTS mb_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mb_deposit_requests (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES mb_users(user_id),
          amount BIGINT NOT NULL CHECK(amount > 0), sender_account TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', bank_chat_id BIGINT, bank_message_id BIGINT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
          UNIQUE(bank_chat_id, bank_message_id)
        );
        CREATE INDEX IF NOT EXISTS mb_deposit_pending_idx ON mb_deposit_requests(sender_account, amount, status, expires_at);
        CREATE TABLE IF NOT EXISTS mb_bank_messages (
          id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
          body TEXT NOT NULL DEFAULT '', amount BIGINT, sender_account TEXT,
          status TEXT NOT NULL DEFAULT 'new', request_id BIGINT REFERENCES mb_deposit_requests(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS mb_withdraw_requests (
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES mb_users(user_id),
          amount BIGINT NOT NULL CHECK(amount > 0), destination_account TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          reviewed_at TIMESTAMPTZ, reviewed_by BIGINT
        );
        INSERT INTO mb_settings(key,value) VALUES('auto_deposit_enabled','0') ON CONFLICT(key) DO NOTHING;
        """)
        rows = await c.fetch("SELECT emoji_key, custom_emoji_id FROM mb_emoji_settings")
        CUSTOM_EMOJI_IDS.update({str(r["emoji_key"]): str(r["custom_emoji_id"]) for r in rows})


async def ensure_user(u, referrer: Optional[int] = None):
    if not u:
        return
    await pool.execute(
        "INSERT INTO mb_users(user_id,username,first_name,referred_by) VALUES($1,$2,$3,$4) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name",
        u.id, u.username or "", u.first_name or "کاربر", referrer if referrer and referrer != u.id else None)


async def balance(uid: int) -> int:
    return int(await pool.fetchval("SELECT points FROM mb_users WHERE user_id=$1", uid) or 0)


async def change_points(uid: int, delta: int, reason: str, ref: Optional[str] = None):
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow("UPDATE mb_users SET points=points+$2 WHERE user_id=$1 AND points+$2>=0 RETURNING points", uid, delta)
        if not row:
            raise ValueError("موجودی امتیاز کافی نیست یا کاربر پیدا نشد.")
        await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,$3,$4)", uid, delta, reason, ref)
        return int(row["points"])


async def log_game(uid: int, game: str, result: str, points: int):
    async with pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO mb_games(user_id,game,result,points) VALUES($1,$2,$3,$4)", uid, game, result, points)
        await c.execute("UPDATE mb_users SET games_count=games_count+1 WHERE user_id=$1", uid)
        await c.execute("INSERT INTO mb_ledger(user_id,delta,reason) VALUES($1,$2,$3)", uid, points, f"game:{game}")
        await c.execute("UPDATE mb_users SET points=GREATEST(0,points+$2) WHERE user_id=$1", uid, points)
        await maybe_reward_referral(c, uid)


async def maybe_reward_referral(c, uid: int):
    row = await c.fetchrow("SELECT referred_by,referral_rewarded,games_count FROM mb_users WHERE user_id=$1 FOR UPDATE", uid)
    if not row or row["referral_rewarded"] or not row["referred_by"] or row["games_count"] < 3:
        return
    inviter = int(row["referred_by"])
    if not await c.fetchval("SELECT 1 FROM mb_users WHERE user_id=$1", inviter):
        return
    await c.execute("UPDATE mb_users SET referral_rewarded=TRUE WHERE user_id=$1", uid)
    for target, amount, reason in ((inviter, REFERRER_BONUS, "referral_bonus"), (uid, REFERRED_BONUS, "welcome_referral_bonus")):
        if amount > 0:
            await c.execute("UPDATE mb_users SET points=points+$2 WHERE user_id=$1", target, amount)
            await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,$3,$4)", target, amount, reason, str(uid))


async def joined(bot, uid: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        return m.status in ("creator", "administrator", "member") or (m.status == "restricted" and m.is_member)
    except TelegramError:
        return False


async def membership_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not REQUIRED_CHANNEL or await joined(context.bot, update.effective_user.id):
        return True
    url = REQUIRED_CHANNEL_URL or (f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}" if REQUIRED_CHANNEL.startswith("@") else "")
    buttons = [[InlineKeyboardButton(_button_text("عضویت در کانال", "CHANNEL"), url=url, icon_custom_emoji_id=custom_emoji_id("CHANNEL"))]] if url else []
    buttons.append([inline_button("بررسی عضویت", "CHECK", "m:checkjoin")])
    text = "برای استفاده از MEOWBET ابتدا در کانال مشخص‌شده عضو شو و بعد بررسی عضویت را بزن."
    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message_text(update.callback_query, text, reply_markup=InlineKeyboardMarkup(buttons))
    elif update.message:
        await safe_reply_text(update.message, text, reply_markup=InlineKeyboardMarkup(buttons))
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message:
        return
    if update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await ensure_user(u)
        await group_touch(update)
        await safe_reply_text(update.message, games_text(), parse_mode="HTML")
        return
    ref = None
    gift_code = None
    if context.args:
        m = re.fullmatch(r"ref_(\d+)", context.args[0])
        if m:
            ref = int(m.group(1))
        gm = re.fullmatch(r"gift_([a-z0-9]+)", context.args[0], re.I)
        if gm:
            gift_code = gm.group(1).lower()
    await ensure_user(u, ref)
    if not await membership_gate(update, context):
        return
    if gift_code:
        await claim_gift(update, context, gift_code)
        return
    text = (f"{emoji_html('CAT')} <b>به MEOWBET خوش اومدی!</b>\n\n"
            "اینجا با بازی‌های گروهی، چالش‌ها و امتیازهای مجازی سرگرم می‌شیم.\n"
            "بازی‌ها فقط داخل گروه اجرا می‌شن و هیچ شرط یا پول واقعی‌ای در کار نیست.\n\n"
            f"{emoji_html('BALANCE')} موجودی امتیاز: <b>{fmt(await balance(u.id))}</b>")
    await safe_reply_text(update.message, text, parse_mode="HTML", reply_markup=home_keyboard(u.id))
    await safe_reply_text(update.message, "از پنل زیر بخش موردنظرت رو انتخاب کن:", reply_markup=main_inline(u.id))


async def menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message:
        return
    await ensure_user(u)
    t = (update.message.text or "").strip()
    if context.user_data.get("finance_step"):
        await handle_finance_text(update, context)
        return
    labels = ["منوی اصلی", "موجودی", "بازی‌های گروه", "جایزه روزانه", "رتبه‌بندی", "پروفایل", "دعوت دوستان", "تاریخچه", "ساخت لینک هدیه", "راهنما", "پنل مدیریت", "واریز امتیاز", "درخواست برداشت"]
    for label in labels:
        if t == label or t.endswith(label):
            t = label
            break
    mapping = {
        "منوی اصلی": (f"{emoji_html('CAT')} <b>پنل اصلی MEOWBET</b>", main_inline(u.id)),
        "موجودی": (f"{emoji_html('BALANCE')} موجودی امتیاز شما: <b>{fmt(await balance(u.id))}</b>", None),
        "بازی‌های گروه": (games_text(), None),
        "جایزه روزانه": (None, None),
        "رتبه‌بندی": (None, None),
        "پروفایل": (None, None),
        "دعوت دوستان": (None, None),
        "تاریخچه": (None, None),
        "ساخت لینک هدیه": (None, None),
        "راهنما": (help_text(), None),
        "پنل مدیریت": (admin_text() if u.id == ADMIN_ID else "این بخش مخصوص مدیر است.", None),
        "واریز امتیاز": ("مبلغ امتیاز مجازی را بفرست.", None),
        "درخواست برداشت": ("مبلغ امتیاز مجازی برای درخواست برداشت را بفرست.", None),
    }
    if t not in mapping:
        return
    if t == "جایزه روزانه":
        await daily(update, context); return
    if t == "رتبه‌بندی":
        await leaderboard(update, context); return
    if t == "پروفایل":
        await profile(update, context); return
    if t == "دعوت دوستان":
        await referral(update, context); return
    if t == "تاریخچه":
        await history(update, context); return
    if t == "ساخت لینک هدیه":
        context.user_data["await_gift"] = True
        await safe_reply_text(update.message, "برای ساخت هدیه، تعداد دریافت‌کننده و امتیاز هر نفر را بفرست. نمونه: <code>3 50</code>\nامتیاز کل از موجودی خودت کم می‌شود.", parse_mode="HTML"); return
    if t == "واریز امتیاز":
        context.user_data["finance_step"] = "deposit_amount"
        await safe_reply_text(update.message, "مقدار امتیاز مجازی موردنظر را به عدد بفرست."); return
    if t == "درخواست برداشت":
        context.user_data["finance_step"] = "withdraw_amount"
        await safe_reply_text(update.message, "مقدار امتیاز مجازی برای درخواست برداشت را به عدد بفرست."); return
    if context.user_data.get("finance_step"):
        await handle_finance_text(update, context)
        return
    text, markup = mapping[t]
    await safe_reply_text(update.message, text, parse_mode="HTML", reply_markup=markup)


def games_text() -> str:
    return (f"{emoji_html('GAMES')} <b>بازی‌های گروهی MEOWBET</b>\n\n"
            "این دستورها را داخل گروهی که ربات در آن عضو است بفرست:\n"
            "• <code>#زوج</code> یا <code>#فرد</code> برای حدس تاس\n"
            "• <code>#دارت</code> برای پرتاب دارت\n"
            "• <code>#فوتبال</code> برای شوت فوتبال\n"
            "• <code>/quiz</code> برای شروع چالش دانستنی\n\n"
            "بازی‌ها رایگان‌اند و فقط امتیاز سرگرمی می‌دهند؛ امتیازها ارزش نقدی ندارند.")


def help_text() -> str:
    return (f"{emoji_html('HELP')} <b>راهنمای MEOWBET</b>\n\n"
            "💠 موجودی: دیدن امتیازهای مجازی\n"
            "🎁 جایزه روزانه: دریافت امتیاز روزانه\n"
            "👥 دعوت دوستان: لینک دعوت و پاداش امتیازی\n"
            "🎁 لینک هدیه: ساخت لینک انتقال امتیاز به دوستان\n"
            "🏆 رتبه‌بندی: دیدن کاربران فعال\n"
            "📜 تاریخچه: دیدن تغییرات اخیر امتیاز\n\n"
            "🎮 بازی‌ها فقط در گروه انجام می‌شوند. امتیازها کاملاً مجازی و بدون ارزش نقدی‌اند؛ شارژ فقط با تطبیق پیام بات میویی و درخواست ثبت‌شده انجام می‌شود. برداشت امتیاز هم نیازمند تأیید مدیر است.")


async def show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user
    await ensure_user(u)
    key = q.data.split(":", 1)[1]
    if key == "checkjoin":
        if await joined(context.bot, u.id):
            await q.answer("عضویت تأیید شد ✅")
            await safe_edit_message_text(q, "عضویت تأیید شد. از منوی اصلی استفاده کن.", reply_markup=main_inline(u.id))
        else:
            await q.answer("هنوز عضویت تأیید نشده است.", show_alert=True)
        return
    await q.answer()
    if key == "home":
        await safe_edit_message_text(q, f"{emoji_html('CAT')} <b>پنل اصلی MEOWBET</b>", parse_mode="HTML", reply_markup=main_inline(u.id)); return
    if key == "balance":
        text = f"{emoji_html('BALANCE')} موجودی امتیاز شما: <b>{fmt(await balance(u.id))}</b>"; markup = back_markup()
    elif key == "games":
        text = games_text(); markup = back_markup()
    elif key == "daily":
        text = await claim_daily(u.id); markup = back_markup()
    elif key == "top":
        text = await top_text(); markup = back_markup()
    elif key == "profile":
        row = await pool.fetchrow("SELECT first_name,username,games_count,created_at FROM mb_users WHERE user_id=$1", u.id)
        text = (f"{emoji_html('PROFILE')} <b>پروفایل</b>\nنام: {row['first_name']}\nنام کاربری: @{row['username'] or 'ندارد'}\n"
                f"بازی‌های انجام‌شده: {row['games_count']}\nامتیاز: {fmt(await balance(u.id))}")
        markup = back_markup()
    elif key == "history":
        text = await history_text(u.id); markup = back_markup()
    elif key == "ref":
        me = await context.bot.get_me()
        text = f"{emoji_html('REFERRAL')} لینک دعوت شما:\nhttps://t.me/{me.username}?start=ref_{u.id}\n\nپس از انجام ۳ بازی توسط دوست دعوت‌شده، پاداش امتیازی فعال می‌شود."; markup = back_markup()
    elif key == "gift":
        text = f"{emoji_html('GIFT')} برای ساخت لینک هدیه، از دکمه «🎁 ساخت لینک هدیه» استفاده کن و تعداد دریافت‌کننده و امتیاز هر نفر را مانند <code>3 50</code> بفرست."; markup = back_markup()
    elif key == "deposit":
        context.user_data["finance_step"] = "deposit_amount"
        await safe_send_message(context.bot, u.id, "مقدار امتیاز مجازی موردنظر را به عدد بفرست.")
        return
    elif key == "withdraw":
        context.user_data["finance_step"] = "withdraw_amount"
        await safe_send_message(context.bot, u.id, "مقدار امتیاز مجازی برای درخواست برداشت را به عدد بفرست.")
        return
    elif key == "help":
        text = help_text(); markup = back_markup()
    elif key == "admin" and u.id == ADMIN_ID:
        text = admin_text(); markup = back_markup()
    else:
        text = "این بخش در دسترس نیست."; markup = back_markup()
    await safe_edit_message_text(q, text, parse_mode="HTML", reply_markup=markup)


def back_markup():
    return InlineKeyboardMarkup([[inline_button("بازگشت به منو", "BACK", "m:home")]])


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await claim_daily(update.effective_user.id)
    await safe_reply_text(update.message, text, parse_mode="HTML")


async def claim_daily(uid: int) -> str:
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow("SELECT last_daily FROM mb_users WHERE user_id=$1 FOR UPDATE", uid)
        today = datetime.now(timezone.utc).date()
        if row["last_daily"] == today:
            return f"{emoji_html('DAILY')} جایزه امروزت رو قبلاً گرفتی. فردا دوباره سر بزن!"
        await c.execute("UPDATE mb_users SET points=points+$2,last_daily=$3 WHERE user_id=$1", uid, DAILY_BONUS, today)
        await c.execute("INSERT INTO mb_ledger(user_id,delta,reason) VALUES($1,$2,'daily_bonus')", uid, DAILY_BONUS)
    return f"{emoji_html('SUCCESS')} جایزه روزانه فعال شد!\n{emoji_html('POINTS')} +{fmt(DAILY_BONUS)} امتیاز\nموجودی جدید: {fmt(await balance(uid))}"


async def top_text() -> str:
    rows = await pool.fetch("SELECT first_name,points FROM mb_users ORDER BY points DESC,games_count DESC LIMIT 10")
    if not rows:
        return "هنوز رتبه‌بندی‌ای وجود ندارد."
    return f"{emoji_html('RANK')} <b>۱۰ نفر برتر MEOWBET</b>\n\n" + "\n".join(f"{i}. {r['first_name']} | 💠 {fmt(r['points'])}" for i, r in enumerate(rows, 1))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(update.message, await top_text(), parse_mode="HTML")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    row = await pool.fetchrow("SELECT first_name,username,games_count FROM mb_users WHERE user_id=$1", u.id)
    await safe_reply_text(update.message, f"{emoji_html('PROFILE')} پروفایل\nنام: {row['first_name']}\nنام کاربری: @{row['username'] or 'ندارد'}\nبازی‌ها: {row['games_count']}\nامتیاز: {fmt(await balance(u.id))}", parse_mode="HTML")


async def history_text(uid: int) -> str:
    rows = await pool.fetch("SELECT delta,reason,created_at FROM mb_ledger WHERE user_id=$1 ORDER BY id DESC LIMIT 10", uid)
    if not rows:
        return "📜 هنوز سابقه‌ای ثبت نشده است."
    lines = []
    for r in rows:
        sign = "+" if r["delta"] >= 0 else ""
        lines.append(f"{sign}{fmt(r['delta'])} | {r['reason']}")
    return f"{emoji_html('HISTORY')} <b>۱۰ تغییر اخیر امتیاز</b>\n\n" + "\n".join(lines)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(update.message, await history_text(update.effective_user.id), parse_mode="HTML")


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    await safe_reply_text(update.message, f"{emoji_html('REFERRAL')} لینک دعوت شما:\nhttps://t.me/{me.username}?start=ref_{update.effective_user.id}\n\nبعد از ۳ بازی دوست دعوت‌شده، پاداش امتیازی داده می‌شود.", parse_mode="HTML")


async def create_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_gift"):
        return False
    context.user_data.pop("await_gift", None)
    m = re.fullmatch(r"\s*(\d{1,3})\s+(\d{1,9})\s*", update.message.text or "")
    if not m:
        await safe_reply_text(update.message, "فرمت درست نیست. نمونه: <code>3 50</code>", parse_mode="HTML"); return True
    capacity, each = map(int, m.groups())
    if not (1 <= capacity <= 100 and 1 <= each <= 1_000_000):
        await safe_reply_text(update.message, "تعداد باید ۱ تا ۱۰۰ و امتیاز هر نفر باید ۱ تا ۱٬۰۰۰٬۰۰۰ باشد."); return True
    total = capacity * each
    uid = update.effective_user.id
    code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    try:
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow("UPDATE mb_users SET points=points-$2 WHERE user_id=$1 AND points >= $2 RETURNING points", uid, total)
            if not row:
                raise ValueError
            await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,'gift_created',$3)", uid, -total, code)
            await c.execute("INSERT INTO mb_gifts(code,owner_id,points_each,capacity) VALUES($1,$2,$3,$4)", code, uid, each, capacity)
    except ValueError:
        await safe_reply_text(update.message, "امتیاز کافی برای ساخت این هدیه نداری."); return True
    me = await context.bot.get_me()
    await safe_reply_text(update.message, f"{emoji_html('GIFT')} لینک هدیه ساخته شد:\nhttps://t.me/{me.username}?start=gift_{code}\n\nظرفیت: {capacity} نفر | سهم هر نفر: {fmt(each)} امتیاز\nامتیاز کل از موجودی تو رزرو شد.", parse_mode="HTML")
    return True


async def claim_gift(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    uid = update.effective_user.id
    async with pool.acquire() as c, c.transaction():
        gift = await c.fetchrow("SELECT * FROM mb_gifts WHERE code=$1 AND active FOR UPDATE", code)
        if not gift or gift["claimed"] >= gift["capacity"]:
            await safe_reply_text(update.message, "این لینک هدیه معتبر نیست یا ظرفیتش تمام شده است."); return
        if gift["owner_id"] == uid:
            await safe_reply_text(update.message, "نمی‌توانی هدیه خودت را دریافت کنی."); return
        exists = await c.fetchval("SELECT 1 FROM mb_gift_claims WHERE code=$1 AND user_id=$2", code, uid)
        if exists:
            await safe_reply_text(update.message, "این هدیه را قبلاً دریافت کرده‌ای."); return
        amount = int(gift["points_each"])
        await c.execute("INSERT INTO mb_gift_claims(code,user_id,points) VALUES($1,$2,$3)", code, uid, amount)
        await c.execute("UPDATE mb_gifts SET claimed=claimed+1,active=(claimed+1<capacity) WHERE code=$1", code)
        await c.execute("UPDATE mb_users SET points=points+$2 WHERE user_id=$1", uid, amount)
        await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,'gift_claim',$3)", uid, amount, code)
    await safe_reply_text(update.message, f"{emoji_html('SUCCESS')} هدیه دریافت شد! +{fmt(amount)} امتیاز\nموجودی: {fmt(await balance(uid))}", parse_mode="HTML")


# ---------------- virtual-point deposits and admin-reviewed withdrawals ----------------
def parse_points(raw: str) -> int:
    t = str(raw or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    t = re.sub(r"[,٬،\s]", "", t)
    return int(t) if re.fullmatch(r"\d{1,12}", t) else 0


async def handle_finance_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    step = context.user_data.get("finance_step")
    raw = (msg.text or "").strip()
    if step in ("deposit_amount", "withdraw_amount"):
        amount = parse_points(raw)
        if not 1 <= amount <= 10_000_000:
            await safe_reply_text(msg, "مقدار نامعتبر است. عددی بین ۱ تا ۱۰٬۰۰۰٬۰۰۰ بفرست.")
            return
        context.user_data["finance_amount"] = amount
        if step == "deposit_amount":
            if not MEOWIE_BANK_ACCOUNT or len(MEOWIE_BANK_ACCOUNT) != 12:
                context.user_data.clear()
                await safe_reply_text(msg, "حساب مقصد بانک میویی هنوز توسط مدیر تنظیم نشده است.")
                return
            context.user_data["finance_step"] = "deposit_sender"
            await safe_reply_text(msg, f"مبلغ درخواست: {fmt(amount)} امتیاز\nحساب مقصد میویی: <code>{MEOWIE_BANK_ACCOUNT}</code>\n\nحساب ۱۲ رقمی خودت را که از آن امتیاز می‌فرستی وارد کن.", parse_mode="HTML")
        else:
            if amount > await balance(uid):
                context.user_data.clear()
                await safe_reply_text(msg, "موجودی امتیاز کافی نیست.")
                return
            context.user_data["finance_step"] = "withdraw_account"
            await safe_reply_text(msg, f"مقدار درخواست: {fmt(amount)} امتیاز\nحساب مقصد ۱۲ رقمی میویی را بفرست. درخواست قبل از تأیید مدیر نهایی نمی‌شود.")
        return
    if step == "deposit_sender":
        account = re.sub(r"\D", "", raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
        amount = int(context.user_data.get("finance_amount", 0))
        if not re.fullmatch(r"\d{12}", account):
            await safe_reply_text(msg, "شماره حساب باید دقیقاً ۱۲ رقم باشد.")
            return
        if not AUTO_BANK.connected or not await get_setting("auto_deposit_enabled", "0") == "1":
            await safe_reply_text(msg, "تأیید خودکار فعلاً فعال نیست؛ بعداً دوباره تلاش کن یا با مدیر هماهنگ کن.")
            context.user_data.clear()
            return
        if account == MEOWIE_BANK_ACCOUNT:
            await safe_reply_text(msg, "حساب مقصد را به‌عنوان حساب فرستنده وارد نکن؛ حساب شخصی خودت را بفرست.")
            return
        existing = await pool.fetchval("SELECT 1 FROM mb_deposit_requests WHERE user_id=$1 AND status='pending'", uid)
        sender_busy = await pool.fetchval("SELECT 1 FROM mb_deposit_requests WHERE sender_account=$1 AND status='pending' AND expires_at > now()", account)
        if existing or sender_busy:
            context.user_data.clear()
            await safe_reply_text(msg, "برای این حساب یا کاربر یک درخواست واریز در انتظار وجود دارد. ابتدا همان درخواست را تکمیل کن یا منتظر انقضایش بمان.")
            return
        req_id = await pool.fetchval(
            "INSERT INTO mb_deposit_requests(user_id,amount,sender_account,expires_at) VALUES($1,$2,$3,now()+($4 * interval '1 minute')) RETURNING id",
            uid, amount, account, DEPOSIT_WINDOW_MINUTES)
        context.user_data.clear()
        await safe_reply_text(msg, f"✅ درخواست واریز امتیاز ثبت شد.\nشناسه: <code>{req_id}</code>\nمبلغ: <b>{fmt(amount)}</b>\nحساب مقصد: <code>{MEOWIE_BANK_ACCOUNT}</code>\nپس از ثبت انتقال در بانک میویی، پیام بات بررسی می‌شود. فقط مبلغ دقیقاً برابر با درخواست شارژ خواهد شد.", parse_mode="HTML")
        return
    if step == "withdraw_account":
        account = re.sub(r"\D", "", raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
        amount = int(context.user_data.get("finance_amount", 0))
        if not re.fullmatch(r"\d{12}", account):
            await safe_reply_text(msg, "شماره حساب باید دقیقاً ۱۲ رقم باشد.")
            return
        req_id = None
        try:
            async with pool.acquire() as c, c.transaction():
                row = await c.fetchrow("UPDATE mb_users SET points=points-$2 WHERE user_id=$1 AND points >= $2 RETURNING points", uid, amount)
                if not row:
                    raise ValueError("موجودی امتیاز کافی نیست.")
                req_id = await c.fetchval("INSERT INTO mb_withdraw_requests(user_id,amount,destination_account) VALUES($1,$2,$3) RETURNING id", uid, amount, account)
                await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,'withdraw_reserved',$3)", uid, -amount, str(req_id))
        except ValueError as exc:
            context.user_data.clear()
            await safe_reply_text(msg, str(exc))
            return
        context.user_data.clear()
        await safe_reply_text(msg, f"✅ درخواست برداشت امتیاز ثبت شد.\nشناسه: <code>{req_id}</code>\nمقدار: {fmt(amount)}\nپس از بررسی مدیر نتیجه به تو اعلام می‌شود.", parse_mode="HTML")
        try:
            await safe_send_message(APP_BOT, ADMIN_ID,
                f"📥 <b>درخواست برداشت امتیاز</b>\nکاربر: <code>{uid}</code>\nمقدار: <b>{fmt(amount)}</b>\nحساب مقصد: <code>{account}</code>\nشناسه: <code>{req_id}</code>",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("تأیید", callback_data=f"wd:approve:{req_id}"), InlineKeyboardButton("رد", callback_data=f"wd:reject:{req_id}")]]))
        except Exception:
            log.exception("withdraw request notify admin failed")
        return


async def get_setting(key: str, default: str = "") -> str:
    return str(await pool.fetchval("SELECT value FROM mb_settings WHERE key=$1", key) or default)


async def set_setting(key: str, value: str):
    await pool.execute("INSERT INTO mb_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, value)


async def admin_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = await pool.fetch("SELECT id,user_id,amount,destination_account FROM mb_withdraw_requests WHERE status='pending' ORDER BY id ASC LIMIT 20")
    if not rows:
        await safe_reply_text(update.message, "درخواست برداشت در انتظاری وجود ندارد.")
        return
    for r in rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("تأیید", callback_data=f"wd:approve:{r['id']}"), InlineKeyboardButton("رد", callback_data=f"wd:reject:{r['id']}")]])
        await safe_reply_text(update.message, f"📥 درخواست #{r['id']}\nکاربر: <code>{r['user_id']}</code>\nمقدار: <b>{fmt(r['amount'])}</b>\nحساب مقصد: <code>{r['destination_account']}</code>", parse_mode="HTML", reply_markup=kb)


async def withdrawal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("فقط مدیر می‌تواند بررسی کند.", show_alert=True)
        return
    _, action, raw_id = q.data.split(":", 2)
    req_id = int(raw_id)
    user_id = None
    amount = 0
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow("SELECT * FROM mb_withdraw_requests WHERE id=$1 FOR UPDATE", req_id)
        if not row or row["status"] != "pending":
            await q.answer("این درخواست قبلاً بررسی شده است.", show_alert=True)
            return
        user_id, amount = int(row["user_id"]), int(row["amount"])
        if action == "approve":
            await c.execute("UPDATE mb_withdraw_requests SET status='approved',reviewed_at=now(),reviewed_by=$2 WHERE id=$1", req_id, ADMIN_ID)
            await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,0,'withdraw_approved',$2)", user_id, str(req_id))
        else:
            await c.execute("UPDATE mb_withdraw_requests SET status='rejected',reviewed_at=now(),reviewed_by=$2 WHERE id=$1", req_id, ADMIN_ID)
            await c.execute("UPDATE mb_users SET points=points+$2 WHERE user_id=$1", user_id, amount)
            await c.execute("INSERT INTO mb_ledger(user_id,delta,reason,ref) VALUES($1,$2,'withdraw_refund',$3)", user_id, amount, str(req_id))
    await q.answer("ثبت شد.")
    await safe_edit_message_text(q, f"درخواست برداشت #{req_id} {'تأیید' if action == 'approve' else 'رد'} شد.")
    try:
        await safe_send_message(context.bot, user_id,
            f"درخواست برداشت امتیاز شما {'تأیید شد' if action == 'approve' else 'رد شد'}." + (f"\n{fmt(amount)} امتیاز به موجودی برگشت داده شد." if action == 'reject' else ""))
    except TelegramError:
        pass


class AutoBankWatcher:
    def __init__(self):
        self.client = None
        self.connected = False

    def available(self):
        return bool(TELETHON_OK and TG_API_ID.isdigit() and TG_API_HASH and TG_SESSION and MEOWIE_BANK_BOTS)

    async def start(self):
        if not self.available():
            log.warning("Auto deposit unavailable: configure TG_API_ID, TG_API_HASH and TG_SESSION")
            return False
        client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH, flood_sleep_threshold=120)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            log.error("TG_SESSION is not authorized; generate a new session locally")
            return False
        client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.client, self.connected = client, True
        log.info("Meowie bank watcher connected")
        return True

    async def on_message(self, event):
        try:
            if not event.is_private:
                return
            sender = await event.get_sender()
            if not sender or not getattr(sender, "bot", False):
                return
            username = (getattr(sender, "username", "") or "").lstrip("@").lower()
            if username not in MEOWIE_BANK_BOTS:
                return
            await process_bank_message(event.chat_id, event.message.id, event.raw_text or "", username)
        except Exception:
            log.exception("Meowie bank update processing failed")

    async def stop(self):
        if self.client:
            await self.client.disconnect()
        self.client, self.connected = None, False


AUTO_BANK = AutoBankWatcher()
APP_BOT = None


def parse_bank_message(body: str):
    normalized = (body or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    try:
        if not re.search(BANK_FILTER_RE, normalized):
            return None
        am = re.search(BANK_AMOUNT_RE, normalized)
        sm = re.search(BANK_SENDER_RE, normalized)
        if not am or not sm:
            return {"error": "مبلغ یا حساب فرستنده از پیام قابل استخراج نبود."}
        amount_raw = am.group(1).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        amount = int(re.sub(r"[,٬،.]", "", amount_raw))
        sender = re.sub(r"\D", "", sm.group(1))
        if amount <= 0 or not re.fullmatch(r"\d{12}", sender):
            return {"error": "مبلغ یا شماره حساب نامعتبر است."}
        return {"amount": amount, "sender": sender}
    except (re.error, ValueError, IndexError) as exc:
        return {"error": f"الگوی پیام بانک نامعتبر است: {exc}"}


async def process_bank_message(chat_id: int, message_id: int, body: str, source_bot: str = ""):
    """Record matching notifications for admin review; never auto-credit points."""
    async with pool.acquire() as c:
        inserted = await c.fetchval(
            "INSERT INTO mb_bank_messages(chat_id,message_id,body) VALUES($1,$2,$3) "
            "ON CONFLICT(chat_id,message_id) DO NOTHING RETURNING id",
            chat_id, message_id, body[:2000]
        )
    if inserted is None:
        return

    parsed = parse_bank_message(body)
    if parsed is None:
        await pool.execute("UPDATE mb_bank_messages SET status='ignored' WHERE id=$1", inserted)
        return
    if parsed.get("error"):
        await pool.execute("UPDATE mb_bank_messages SET status='unparsed' WHERE id=$1", inserted)
        await safe_send_message(
            APP_BOT, ADMIN_ID,
            "⚠️ پیام از یکی از بات‌های ثبت‌شده دریافت شد اما قابل‌خواندن نبود: " + parsed["error"]
            + (f"\nمنبع: @{source_bot}" if source_bot else "")
        )
        return

    amount, sender = parsed["amount"], parsed["sender"]
    await pool.execute(
        "UPDATE mb_bank_messages SET amount=$2,sender_account=$3,status='logged' WHERE id=$1",
        inserted, amount, sender
    )
    await safe_send_message(
        APP_BOT, ADMIN_ID,
        f"📩 اعلان واریز ثبت شد و نیاز به بررسی مدیر دارد.\n"
        f"🤖 منبع: @{source_bot or 'نامشخص'}\n"
        f"💰 مبلغ اعلام‌شده: {fmt(amount)}\n"
        f"💳 حساب فرستنده: {sender}\n"
        f"🆔 شناسه پیام ثبت‌شده: {inserted}\n"
        "⚠️ صرف دریافت پیام باعث افزایش موجودی نمی‌شود."
    )


async def deposit_expiry_loop():
    while True:
        try:
            rows = await pool.fetch("UPDATE mb_deposit_requests SET status='expired' WHERE status='pending' AND expires_at < now() RETURNING user_id,amount")
            for row in rows:
                await safe_send_message(APP_BOT, row["user_id"], f"⌛ درخواست واریز {fmt(row['amount'])} امتیاز منقضی شد؛ امتیازی اضافه نشد.")
        except Exception:
            log.exception("deposit expiry loop failed")
        await asyncio.sleep(30)


async def admin_autodeposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await safe_reply_text(
        update.message,
        "در این نسخه، پیام‌های پنج بات فقط ثبت و برای بررسی مدیر ارسال می‌شوند؛ "
        "افزایش خودکار موجودی از روی اعلان بانکی فعال نیست."
    )


async def admin_bankstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await safe_reply_text(update.message,
        f"وضعیت اتصال: {'متصل' if AUTO_BANK.connected else 'قطع'}\n"
        f"بات‌های تحت‌نظر: {', '.join(sorted(MEOWIE_BANK_BOTS))}\n"
        "ثبت اعلان‌ها: فعال\n"
        "افزایش خودکار موجودی: غیرفعال")


async def group_touch(update: Update):
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await pool.execute("INSERT INTO mb_groups(chat_id,title,username) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,username=EXCLUDED.username,last_seen=now(),active=TRUE", chat.id, chat.title or "گروه", chat.username or "")


async def group_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    await group_touch(update)
    await ensure_user(u)
    if not await membership_gate(update, context):
        return
    text = (msg.text or "").strip()
    if text.startswith("#زوج") or text.startswith("#فرد"):
        guess = "زوج" if text.startswith("#زوج") else "فرد"
        d = await context.bot.send_dice(msg.chat_id, emoji="🎲")
        value = d.dice.value
        actual = "زوج" if value % 2 == 0 else "فرد"
        points = 30 if guess == actual else 5
        await log_game(u.id, "تاس", "درست" if guess == actual else "نادرست", points)
        await safe_reply_text(msg, f"{emoji_html('DICE')} نتیجه: {value} ({actual})\n{'آفرین! حدست درست بود.' if guess == actual else 'این بار درست نشد!'}\n💠 +{fmt(points)} امتیاز سرگرمی", parse_mode="HTML")
    elif text.startswith("#دارت"):
        d = await context.bot.send_dice(msg.chat_id, emoji="🎯")
        points = 5 + d.dice.value * 5
        await log_game(u.id, "دارت", "پرتاب", points)
        await safe_reply_text(msg, f"{emoji_html('DART')} امتیاز پرتاب: {d.dice.value}\n💠 +{fmt(points)} امتیاز سرگرمی", parse_mode="HTML")
    elif text.startswith("#فوتبال"):
        d = await context.bot.send_dice(msg.chat_id, emoji="⚽")
        goal = d.dice.value >= 3
        points = 25 if goal else 5
        await log_game(u.id, "فوتبال", "گل" if goal else "مهار شد", points)
        await safe_reply_text(msg, f"{emoji_html('FOOTBALL')} {'گــــل!' if goal else 'دروازه‌بان توپ را گرفت!'}\n💠 +{fmt(points)} امتیاز سرگرمی", parse_mode="HTML")


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_reply_text(update.message, "چالش دانستنی فقط داخل گروه اجرا می‌شود."); return
    await group_touch(update)
    await ensure_user(update.effective_user)
    await launch_quiz(context.bot, update.effective_chat.id)


async def launch_quiz(bot, chat_id: int):
    question, answers = random.choice(QUESTIONS)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO mb_quiz_sessions(chat_id,question,answers,prize,expires_at) VALUES($1,$2,$3,$4,now()+interval '90 seconds') "
                        "ON CONFLICT(chat_id) DO UPDATE SET question=EXCLUDED.question,answers=EXCLUDED.answers,prize=EXCLUDED.prize,expires_at=EXCLUDED.expires_at",
                        chat_id, question, answers, QUIZ_PRIZE)
    await safe_send_message(bot, chat_id, f"{emoji_html('QUIZ')} <b>چالش دانستنی MEOWBET</b>\n\n{question}\n\nاولین پاسخ درست تا ۹۰ ثانیه، {fmt(QUIZ_PRIZE)} امتیاز می‌گیرد!", parse_mode="HTML")


async def quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not msg.text:
        return
    await group_touch(update)
    row = await pool.fetchrow("SELECT * FROM mb_quiz_sessions WHERE chat_id=$1 AND expires_at>now()", msg.chat_id)
    if not row or norm(msg.text) not in [norm(a) for a in row["answers"]]:
        return
    await ensure_user(msg.from_user)
    async with pool.acquire() as c, c.transaction():
        locked = await c.fetchrow("DELETE FROM mb_quiz_sessions WHERE chat_id=$1 AND expires_at>now() RETURNING prize", msg.chat_id)
        if not locked:
            return
        await c.execute("UPDATE mb_users SET points=points+$2 WHERE user_id=$1", msg.from_user.id, int(locked["prize"]))
        await c.execute("INSERT INTO mb_ledger(user_id,delta,reason) VALUES($1,$2,'quiz_win')", msg.from_user.id, int(locked["prize"]))
    await safe_reply_text(msg, f"{emoji_html('RANK')} پاسخ درست! {msg.from_user.first_name} برنده چالش شد و {fmt(int(locked['prize']))} امتیاز گرفت.", parse_mode="HTML")


async def quiz_loop(bot):
    while True:
        try:
            groups = await pool.fetch("SELECT chat_id FROM mb_groups WHERE active=TRUE")
            for row in groups:
                try:
                    await launch_quiz(bot, int(row["chat_id"]))
                except TelegramError as e:
                    if "chat not found" in str(e).lower() or "kicked" in str(e).lower():
                        await pool.execute("UPDATE mb_groups SET active=FALSE WHERE chat_id=$1", row["chat_id"])
                    else:
                        log.warning("quiz send failed for %s: %s", row["chat_id"], e)
                await asyncio.sleep(0.5)
        except Exception:
            log.exception("quiz loop error")
        await asyncio.sleep(600)


async def admin_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or not re.fullmatch(r"-?\d+", context.args[1]):
        await safe_reply_text(update.message, "فرمت: <code>/grant USER_ID POINTS</code>\nبرای کسر امتیاز عدد منفی بفرست: <code>/grant USER_ID -50</code>", parse_mode="HTML"); return
    uid = int(context.args[0]); amount = int(context.args[1])
    if abs(amount) > 10_000_000:
        await safe_reply_text(update.message, "مقدار بیش از حد مجاز است."); return
    try:
        new = await change_points(uid, amount, "admin_adjustment")
    except ValueError as e:
        await safe_reply_text(update.message, str(e)); return
    await safe_reply_text(update.message, f"انجام شد. موجودی جدید کاربر: {fmt(new)} امتیاز")
    try:
        await safe_send_message(context.bot, uid, f"مدیر موجودی امتیاز شما را {amount:+,} تغییر داد. موجودی جدید: {fmt(new)}")
    except TelegramError:
        pass


def admin_text() -> str:
    return (f"{emoji_html('ADMIN')} <b>پنل مدیریت</b>\n\n"
            "/grant USER_ID POINTS برای افزایش یا کاهش امتیاز\n"
            "/stats آمار کاربران و گروه‌ها\n/groups فهرست گروه‌ها\n"
            "/withdrawals درخواست‌های برداشت در انتظار\n"
            "/autodeposit on یا /autodeposit off برای روشن/خاموش‌کردن بررسی خودکار\n"
            "/bankstatus وضعیت اتصال بانک میویی\n\n"
            "اطلاعات ورود و توکن‌ها را در چت ارسال نکن؛ فقط در Environment Variables نگه دار.")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    users = await pool.fetchval("SELECT COUNT(*) FROM mb_users")
    groups = await pool.fetchval("SELECT COUNT(*) FROM mb_groups WHERE active")
    points = await pool.fetchval("SELECT COALESCE(SUM(points),0) FROM mb_users")
    await safe_reply_text(update.message, f"{emoji_html('ADMIN')} کاربران: {users}\n👥 گروه‌های فعال: {groups}\n{emoji_html('POINTS')} مجموع امتیازها: {fmt(points)}", parse_mode="HTML")


async def admin_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    rows = await pool.fetch("SELECT chat_id,title FROM mb_groups WHERE active ORDER BY last_seen DESC LIMIT 30")
    await safe_reply_text(update.message, "👥 گروه‌های فعال:\n" + ("\n".join(f"{r['title']} | <code>{r['chat_id']}</code>" for r in rows) if rows else "هنوز گروهی ثبت نشده است."), parse_mode="HTML")


async def admin_emoji_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or context.args[0].upper() not in EMOJI_KEYS:
        await safe_reply_text(update.message, "فرمت: روی پیامی که ایموجی پرمیوم دارد Reply کن و بنویس /emojiadd HOME\nکلیدها: " + ", ".join(EMOJI_KEYS.keys()))
        return
    target = update.message.reply_to_message
    if not target:
        await safe_reply_text(update.message, "این دستور را در پاسخ به پیامی بفرست که ایموجی پرمیوم داخل متنش دارد.")
        return
    entities = list(target.entities or []) + list(target.caption_entities or [])
    entity = next((e for e in entities if e.type == "custom_emoji" and e.custom_emoji_id), None)
    if not entity:
        await safe_reply_text(update.message, "در پیام پاسخ‌داده‌شده ایموجی سفارشی پیدا نکردم. خود ایموجی را به‌صورت پیام متنی بفرست و روی آن Reply کن.")
        return
    key = context.args[0].upper()
    emoji_id = str(entity.custom_emoji_id)
    await pool.execute("INSERT INTO mb_emoji_settings(emoji_key,custom_emoji_id) VALUES($1,$2) ON CONFLICT(emoji_key) DO UPDATE SET custom_emoji_id=EXCLUDED.custom_emoji_id,updated_at=now()", key, emoji_id)
    CUSTOM_EMOJI_IDS[key] = emoji_id
    await safe_reply_text(update.message, f"ثبت شد: {key}\nشناسه ایموجی ذخیره شد. برای استفاده نیازی به تنظیم متغیر Render نیست؛ ربات خودش امتحان می‌کند و در صورت نداشتن مجوز، ایموجی معمولی نشان می‌دهد.")


async def admin_emoji_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    lines = [f"{key}: {'تنظیم شده' if CUSTOM_EMOJI_IDS.get(key) else 'تنظیم نشده'}" for key in EMOJI_KEYS]
    await safe_reply_text(update.message, "🎨 وضعیت ایموجی‌های MEOWBET\n\n" + "\n".join(lines) + "\n\nبرای افزودن: روی پیام ایموجی Reply کن و /emojiadd KEY بفرست. برای پاک‌کردن: /emojiclear KEY")


async def admin_emoji_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or context.args[0].upper() not in EMOJI_KEYS:
        await safe_reply_text(update.message, "فرمت: /emojiclear KEY")
        return
    key = context.args[0].upper()
    await pool.execute("DELETE FROM mb_emoji_settings WHERE emoji_key=$1", key)
    CUSTOM_EMOJI_IDS.pop(key, None)
    await safe_reply_text(update.message, f"ایموجی {key} پاک شد.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled bot error", exc_info=context.error)


async def health(request: web.Request):
    return web.Response(text="MEOWBET v9 is running")


async def keepalive_loop():
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(url.rstrip("/") + "/health") as response:
                    await response.read()
            except Exception as exc:
                log.warning("keepalive ping failed: %s", exc)


async def main_async():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=DB_POOL_MAX, statement_cache_size=0, command_timeout=30)
    await init_db()
    global APP_BOT
    app = Application.builder().token(TOKEN).build()
    APP_BOT = app.bot
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("grant", admin_grant))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("groups", admin_groups))
    app.add_handler(CommandHandler("withdrawals", admin_withdrawals))
    app.add_handler(CommandHandler("autodeposit", admin_autodeposit))
    app.add_handler(CommandHandler("bankstatus", admin_bankstatus))
    app.add_handler(CallbackQueryHandler(withdrawal_callback, pattern=r"^wd:"))
    app.add_handler(CommandHandler("emojiadd", admin_emoji_add))
    app.add_handler(CommandHandler("emojis", admin_emoji_list))
    app.add_handler(CommandHandler("emojiclear", admin_emoji_clear))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("help", lambda u, c: safe_reply_text(u.effective_message, help_text(), parse_mode="HTML")))
    app.add_handler(CommandHandler("games", lambda u, c: safe_reply_text(u.effective_message, games_text(), parse_mode="HTML")))
    app.add_handler(CallbackQueryHandler(show_callback, pattern=r"^m:"))
    app.add_handler(MessageHandler(filters.Regex(r"^#(?:زوج|فرد|دارت|فوتبال)"), group_game), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, quiz_answer), group=0)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, create_gift), group=0)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, menu_text), group=1)
    app.add_error_handler(on_error)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await app.initialize(); await app.start()
    if AUTO_BANK.available():
        try:
            await AUTO_BANK.start()
        except Exception:
            log.exception("Could not start Meowie bank watcher")
    await app.updater.start_polling(drop_pending_updates=True)
    quiz_task = asyncio.create_task(quiz_loop(app.bot))
    keepalive_task = asyncio.create_task(keepalive_loop())
    expiry_task = asyncio.create_task(deposit_expiry_loop())
    log.info("MEOWBET v9 started | custom emoji configured=%s | auto-bank-connected=%s", len(CUSTOM_EMOJI_IDS), AUTO_BANK.connected)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    await stop.wait()
    quiz_task.cancel(); keepalive_task.cancel(); expiry_task.cancel()
    await asyncio.gather(quiz_task, keepalive_task, expiry_task, return_exceptions=True)
    await AUTO_BANK.stop()
    await app.updater.stop(); await app.stop(); await app.shutdown(); await runner.cleanup(); await pool.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

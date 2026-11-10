# -*- coding: utf-8 -*-
"""
MEOWBET v7 (glass UI)  -  Render Web Service + Supabase (PostgreSQL)

Environment variables (Render -> Environment):
  BOT_TOKEN        required  Telegram bot token
  ADMIN_ID         required  numeric Telegram id of the owner
  DATABASE_URL     required  Supabase connection string (see notes)
  PAYMENT_CARD     optional  Meowie-bank account that receives deposits (can also be changed from the admin panel)
  REQUIRED_CHANNEL optional  seed one mandatory channel, e.g. @mychannel
  REQUIRED_CHANNEL_URL optional
  KEEPALIVE        optional  "1" (default) = ping own URL every 10 min so the free plan does not sleep
  DB_POOL_MAX      optional  max DB connections (default 5)
  TG_API_ID / TG_API_HASH  optional  from my.telegram.org - needed only for auto-deposit (Telethon)
  BUTTON_COLORS    optional  "1" = coloured glass buttons (Bot API button styles); default off
  PREMIUM_EMOJI_JSON optional  {"🎲": "<custom emoji id>"}; can also be set live with /pe
  PORT             set by Render automatically
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import asyncpg
from aiohttp import ClientSession, ClientTimeout, web

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatType
from telegram.error import (
    BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)
from telegram.ext import (
    Application, ApplicationHandlerStop, BaseRateLimiter, BaseUpdateProcessor,
    CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

try:  # auto-deposit userbot (optional dependency)
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.errors import (
        FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
        PhoneCodeInvalidError, PhoneNumberInvalidError, SessionPasswordNeededError,
    )
    TELETHON_OK = True
except ImportError:
    TELETHON_OK = False

    class FloodWaitError(Exception):
        seconds = 0

    class PasswordHashInvalidError(Exception):
        pass

    class PhoneCodeExpiredError(Exception):
        pass

    class PhoneCodeInvalidError(Exception):
        pass

    class PhoneNumberInvalidError(Exception):
        pass

    class SessionPasswordNeededError(Exception):
        pass

# ============================================================
# Config
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENV_PAYMENT_CARD = os.getenv("PAYMENT_CARD", "").strip()
TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    PORT = int(os.getenv("PORT", "10000"))
except ValueError:
    raise RuntimeError("ADMIN_ID and PORT must be numeric")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# httpx logs full request URLs at INFO level, and those URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("meowbet")

# Game / economy constants (virtual currency "میو")
MIN_GAME = 20_000
MAX_GAME = 15_000_000
DICE_TIMEOUT = 60            # seconds to send 3 dice
DART_TIMEOUT = 60            # seconds to send the dart
FOOTBALL_TIMEOUT = 120       # seconds before an unfinished football game is dropped
POOP_TIMEOUT = 6 * 3600      # idle poop games are auto-cashed-out after 6 hours
TIMEOUT_PENALTY = 0.50       # share of the stake lost on timeout
POOP_ROWS, POOP_COLS = 5, 4
POOP_COUNTS = [1, 1, 2, 2, 3]
POOP_MULTIPLIERS = [1.2, 1.4, 1.6, 2.0, 3.0]
PAGE_SIZE = 8

DEFAULT_SETTINGS = {
    "dice_mult": "1.5",
    "poop_rows": str(POOP_ROWS),
    "poop_cols": str(POOP_COLS),
    "poop_counts": ",".join(map(str, POOP_COUNTS)),
    "poop_mults": ",".join(map(str, POOP_MULTIPLIERS)),
    "football_goal_values": "4,5",
    "football_shots": "3",
    "leaderboard_reward_count": "0",
    "leaderboard_rewards": "{}",
    "referrer_bonus": "100000",
    "referred_bonus": "50000",
    "referral_min_games": "3",
    "dart_multiplier": "2.0",
    "dart_white_values": "1,3,5",
    "dart_red_values": "2,4,6",
    "quiz_enabled": "1",
    "quiz_interval_minutes": "10",
    "quiz_prize": "50000",
    "quiz_timeout_seconds": "120",
    "daily_bonus": "5000",
    "min_withdraw": "100000",
    "win_fee_percent": "0",
    "auto_deposit_enabled": "0",
    "bank_bot": "",
    "premium_emoji": "{}",
    "deposit_window_minutes": "60",
    # Built from a real Meowie-bank "کارت به کارت" message. Patterns are anchored to the start of a line,
    # so text a sender puts in his display name ("به نام : ...") can never inject a fake amount/account.
    "bank_filter_re": r"(?s)کارت به کارت میویی.*تراکنش موفقیت\s*آمیز",
    "bank_amount_re": r"(?m)^\W*مبلغ دریافت شده\s*:\s*(\d[\d,٬،.]*)",
    "bank_sender_re": r"(?m)^\W*شماره حساب واریز\s*کننده\s*:\s*(\d{12})(?!\d)",
}

# Old guessed defaults (v6 first drop) - auto-upgraded to the real patterns if the admin never changed them.
LEGACY_BANK_DEFAULTS = {
    "bank_filter_re": ["(واریز|دریافت|انتقال)"],
    "bank_amount_re": ["(?:مبلغ|مقدار)[^\\d]{0,25}(\\d[\\d,.]*)", "(?:مبلغ|مقدار)[^\\d]{0,25}(\\d[\\d,٬،.]*)"],
    "bank_sender_re": ["(?:از|فرستنده|مبدا|مبدأ)[^\\d]{0,40}(\\d{12})"],
}

GAME_LABELS = {"dice": "🎲 تاس", "dart": "🎯 دارت", "poop": "💩 پوپ", "dynamite": "🧨 دینامیت", "quiz": "🧠 چالش", "football": "⚽ فوتبال"}
RESULT_LABELS = {"win": "برد", "loss": "باخت", "cashout": "کش‌اوت", "timeout": "تایم‌اوت", "refund": "بازگشت"}


# ============================================================
# Small helpers
# ============================================================
def utc_ts() -> float:
    return time.time()


def format_mio(n) -> str:
    return f"{int(n):,}"


def esc(s) -> str:
    return html.escape(str(s or ""))


def uname(u) -> str:
    """HTML-safe display name."""
    return esc(f"@{u.username}" if u.username else (u.first_name or "کاربر"))


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_amount(text: str) -> int:
    """Parse Latin/Persian amounts such as 50000, 50k, 50کی, 50کا, 50ک, 2m, 2میلیون."""
    if not text:
        return 0
    t = str(text).strip().lower().translate(_DIGITS)
    t = t.replace(",", "").replace("٬", "").replace("،", "").replace(" ", "")
    multiplier = 1
    if re.search(r"(?:میلیون|مگ|mn|million|m)$", t):
        multiplier = 1_000_000
        t = re.sub(r"(?:میلیون|مگ|mn|million|m)$", "", t)
    elif re.search(r"(?:هزار|کی|کا|ک|k|thousand)$", t):
        multiplier = 1_000
        t = re.sub(r"(?:هزار|کی|کا|ک|k|thousand)$", "", t)
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return 0
    try:
        return int(float(m.group(0)) * multiplier)
    except (ValueError, OverflowError):
        return 0


def parse_int_list(raw, fallback):
    try:
        vals = [int(x.strip()) for x in str(raw).split(",") if x.strip()]
    except ValueError:
        return list(fallback)
    return vals or list(fallback)


def parse_float_list(raw, fallback):
    try:
        vals = [float(x.strip()) for x in str(raw).split(",") if x.strip()]
    except ValueError:
        return list(fallback)
    return vals or list(fallback)


def is_group(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type in (ChatType.GROUP, ChatType.SUPERGROUP))


def is_private(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type == ChatType.PRIVATE)


def is_forwarded(msg) -> bool:
    return getattr(msg, "forward_origin", None) is not None


def terminal(fn):
    """Run the handler, then stop PTB from passing the same message to later handler groups.
    Without this, pressing a menu button (e.g. 'واریز') also reached the text state-machine
    and produced a bogus 'مبلغ نامعتبر' reply."""
    async def wrapper(update, context):
        await fn(update, context)
        raise ApplicationHandlerStop
    wrapper.__name__ = getattr(fn, "__name__", "terminal")
    return wrapper


# ============================================================
# Database layer (asyncpg -> Supabase)
# ============================================================
pool: Optional[asyncpg.Pool] = None
_settings: dict = {}


class InsufficientFunds(Exception):
    pass


class UserNotFound(Exception):
    pass


class ActiveGame(Exception):
    pass


async def init_pool() -> None:
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.getenv("DB_POOL_MAX", "6")),
        # Required for Supabase's transaction pooler (pgbouncer); harmless otherwise.
        statement_cache_size=0,
        command_timeout=30,
    )


async def init_db() -> None:
    path = os.path.join(BASE_DIR, "schema.sql")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            sql = f.read()
        async with pool.acquire() as c:
            await c.execute(sql)
    else:
        logger.warning("schema.sql not found next to the bot - assuming tables already exist")
    await init_dynamite()
    async with pool.acquire() as c:
        await c.executemany(
            "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING",
            list(DEFAULT_SETTINGS.items()),
        )
        for key, olds in LEGACY_BANK_DEFAULTS.items():
            await c.execute("UPDATE settings SET value=$2 WHERE key=$1 AND value = ANY($3::text[])",
                            key, DEFAULT_SETTINGS[key], olds)
        old = os.getenv("REQUIRED_CHANNEL", "").strip()
        if old:
            url = os.getenv("REQUIRED_CHANNEL_URL", "").strip()
            await c.execute(
                "INSERT INTO required_chats(chat_id,title,username,url,chat_type) "
                "VALUES($1,$1,$2,$3,'channel') ON CONFLICT(chat_id) DO NOTHING",
                old, old if old.startswith("@") else "", url,
            )


async def load_settings() -> None:
    rows = await pool.fetch("SELECT key,value FROM settings")
    _settings.clear()
    _settings.update({r["key"]: r["value"] for r in rows})


def setting(key: str, default: str = "") -> str:
    v = _settings.get(key)
    if v is None:
        v = DEFAULT_SETTINGS.get(key, default)
    return v


async def set_setting(key: str, value: str) -> None:
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, value)
    _settings[key] = value


def s_int(key: str, default: int = 0) -> int:
    try:
        return int(float(setting(key)))
    except (TypeError, ValueError):
        return default


def s_float(key: str, default: float = 0.0) -> float:
    try:
        return float(setting(key))
    except (TypeError, ValueError):
        return default


def payment_card() -> str:
    return setting("payment_card", "") or ENV_PAYMENT_CARD


# ---------------- balance / ledger ----------------
async def adjust(conn, uid: int, delta: int, reason: str, ref: Optional[str] = None) -> int:
    """Change a balance inside an existing transaction + write the ledger row.
    Never lets the balance go negative. Returns the new balance."""
    row = await conn.fetchrow(
        "UPDATE users SET balance=balance+$2, updated_at=now() "
        "WHERE user_id=$1 AND balance+$2>=0 RETURNING balance", uid, delta)
    if row is None:
        if await conn.fetchval("SELECT 1 FROM users WHERE user_id=$1", uid):
            raise InsufficientFunds()
        raise UserNotFound()
    await conn.execute(
        "INSERT INTO ledger(user_id,delta,reason,ref_id) VALUES($1,$2,$3,$4)", uid, delta, reason, ref)
    return int(row["balance"])


async def atomic_balance(uid: int, delta: int, reason: str, ref: Optional[str] = None) -> int:
    async with pool.acquire() as c, c.transaction():
        return await adjust(c, uid, delta, reason, ref)


_ensured: dict = {}


async def ensure_user(u, referral_by: Optional[int] = None) -> None:
    """Create/refresh the user row. Skips the DB write when nothing changed in the last 10 minutes."""
    key = (u.username or "", u.first_name or "")
    cached = _ensured.get(u.id)
    if referral_by is None and cached and cached[0] == key and time.time() - cached[1] < 600:
        return
    await pool.execute(
        "INSERT INTO users(user_id,username,first_name,referral_by) VALUES($1,$2,$3,$4) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, "
        "first_name=EXCLUDED.first_name, updated_at=now()",
        u.id, u.username or "", u.first_name or "کاربر",
        referral_by if referral_by and referral_by != u.id else None)
    _ensured[u.id] = (key, time.time())


async def get_user(uid: int):
    return await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def get_balance(uid: int) -> int:
    return int(await pool.fetchval("SELECT balance FROM users WHERE user_id=$1", uid) or 0)


# ---------------- background notices ----------------
_notice_q: "asyncio.Queue" = asyncio.Queue()


def notify(chat_id: int, text: str) -> None:
    _notice_q.put_nowait((chat_id, text))


# ---------------- referral ----------------
async def try_referral(uid: int, conn=None) -> bool:
    """Pay the referral bonus once the invited user has finished enough real games
    (referral_min_games, default 3; 0 = pay immediately). Stops fake-account farming."""
    if conn is None:
        async with pool.acquire() as c, c.transaction():
            return await try_referral(uid, c)
    u = await conn.fetchrow(
        "SELECT referral_by, referral_rewarded, games_count FROM users WHERE user_id=$1 FOR UPDATE", uid)
    if not u or u["referral_rewarded"] or not u["referral_by"] or u["referral_by"] == uid:
        return False
    if u["games_count"] < s_int("referral_min_games", 3):
        return False
    inviter = int(u["referral_by"])
    if not await conn.fetchval("SELECT 1 FROM users WHERE user_id=$1", inviter):
        return False
    rb, nb = s_int("referrer_bonus", 100000), s_int("referred_bonus", 50000)
    await conn.execute("UPDATE users SET referral_rewarded=TRUE WHERE user_id=$1", uid)
    if rb > 0:
        await adjust(conn, inviter, rb, "referral_bonus", str(uid))
        notify(inviter, f"🎉 دعوت‌شده‌ی شما فعال شد! +{format_mio(rb)} میو")
    if nb > 0:
        await adjust(conn, uid, nb, "referred_bonus", str(inviter))
        notify(uid, f"🎉 پاداش دعوت فعال شد!\n+{format_mio(nb)} میو به شما رسید.")
    return True


def apply_fee(stake: int, prize: int):
    """House fee on the PROFIT of a win (prize - stake). Returns (net_prize, fee)."""
    pct = s_float("win_fee_percent", 0.0)
    profit = prize - stake
    if pct <= 0 or profit <= 0:
        return prize, 0
    fee = int(profit * min(pct, 50.0) / 100)
    return prize - fee, fee


async def log_game(conn, uid: int, game: str, amount: int, result: str, payout: int = 0, fee: int = 0) -> None:
    await conn.execute(
        "INSERT INTO game_stats(user_id,game,amount,result,payout,fee) VALUES($1,$2,$3,$4,$5,$6)",
        uid, game, amount, result, payout, fee)
    # Only finished real games count (not timeouts / refunds / quiz answers).
    if game in ("dice", "dart", "poop", "dynamite") and result in ("win", "loss", "cashout"):
        await conn.execute(
            "UPDATE users SET games_count=games_count+1, wins_count=wins_count+$2 WHERE user_id=$1",
            uid, 1 if result in ("win", "cashout") else 0)
        await try_referral(uid, conn)


# ============================================================
# Keyboards / texts
# ============================================================



def help_text() -> str:
    return (
        "╭━━━「 🎰 MEOWBET | میوبت 」━━━╮\n"
        "│ 🐾 راهنمای کامل ربات\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "💰 «حساب و کیف پول»\n"
        "• 💰 موجودی — مشاهده موجودی لحظه‌ای\n"
        "• ➕ واریز — ثبت درخواست شارژ حساب و ارسال رسید\n"
        "• ➖ برداشت — ثبت درخواست برداشت و پیگیری وضعیت\n"
        "• 📜 تاریخچه — مشاهده سوابق بازی‌های شما\n\n"
        "🎮 «بازی‌ها»\n"
        "• 🎲 #زوج مبلغ — بازی زوج\n"
        "• 🎲 #فرد مبلغ — بازی فرد\n"
        "• 💩 #پوپ مبلغ — بازی پوپ و انتخاب خانه‌ها\n"
        "• 🎯 #سفید مبلغ — دارت سفید\n"
        "• 🎯 #قرمز مبلغ — دارت قرمز\n"
        "• ⚽ #فوتبال — بازی فوتبال؛ سپس شوت‌های ⚽ را بفرست\n"
        "• 🧨 «میو بت دینامیت» یا «دینامیت 50k» — بازی ۲ نفره؛ نفر دوم با «پیوستن» میاد\n\n"
        "🧩 «پنل شیشه‌ای»\n"
        "• توی گروه بنویس «میو بت پنل بازی ها» ← پنل بازی‌ها\n"
        "• توی گروه یا پیوی بنویس «بانک» ← موجودی، واریز و برداشت\n\n"
        "🎯 دارت: بعد از ثبت بازی، دقیقاً یک دارت 🎯 در همان گروه ارسال کن.\n"
        "⚽ فوتبال: با #فوتبال شروع کن و سپس توپ ⚽ بفرست.\n"
        "⏱️ اگر بعد از ثبت شرط، تاس/دارت را به‌موقع نفرستی، بخشی از مبلغ جریمه می‌شود.\n\n"
        "🎁 «هدیه و دعوت»\n"
        "• 🎁 لینک هدیه — ساخت لینک هدیه با ظرفیت و مبلغ دلخواه\n"
        "• 👥 دعوت دوستان — پاداش بعد از اینکه دوستت چند بازی انجام داد فعال می‌شود\n"
        "• 🎁 جایزه روزانه — دریافت پاداش روزانه\n\n"
        "🧠 «چالش گروهی»\n"
        "هر چند دقیقه یک سؤال در گروه‌های فعال ارسال می‌شود. نفر اول با پاسخ صحیح جایزه می‌گیرد.\n\n"
        "📌 بازی‌ها و چالش‌ها در گروه انجام می‌شوند. برای استفاده، ابتدا در کانال‌ها/گروه‌های اجباری عضو شوید.\n\n"
        "❤️ موفق باشی و با دقت بازی کن!"
    )


# ============================================================
# Mandatory membership
# ============================================================
_req_cache = {"ts": 0.0, "rows": []}
_join_ok: dict = {}


async def get_required_chats():
    if time.time() - _req_cache["ts"] > 30:
        _req_cache["rows"] = await pool.fetch("SELECT * FROM required_chats WHERE enabled ORDER BY id")
        _req_cache["ts"] = time.time()
    return _req_cache["rows"]


def invalidate_required() -> None:
    _req_cache["ts"] = 0.0
    _join_ok.clear()


async def is_joined(bot, user_id: int) -> bool:
    chats = await get_required_chats()
    if not chats:
        return True
    if _join_ok.get(user_id, 0) > time.time():
        return True
    for chat in chats:
        try:
            m = await bot.get_chat_member(chat["chat_id"], user_id)
        except TelegramError as e:
            logger.warning("membership check failed chat=%s user=%s: %s", chat["chat_id"], user_id, e)
            return False
        if not (m.status in ("creator", "administrator", "member")
                or (m.status == "restricted" and getattr(m, "is_member", False))):
            return False
    _join_ok[user_id] = time.time() + 120
    return True


async def require_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chats = await get_required_chats()
    if not chats:
        return True
    u = update.effective_user
    if not u or await is_joined(context.bot, u.id):
        return True
    buttons = []
    for i, chat in enumerate(chats, 1):
        url = chat["url"]
        if not url and chat["username"].startswith("@"):
            url = f"https://t.me/{chat['username'][1:]}"
        if url:
            label = f"📢 عضویت {i}: {chat['title'] or chat['username'] or chat['chat_id']}"
            buttons.append([InlineKeyboardButton(label[:60], url=url)])
    buttons.append([InlineKeyboardButton("✅ بررسی عضویت همه", callback_data="check_join")])
    text = ("🔒 برای استفاده از میوبت باید در همه کانال‌ها/گروه‌های اجباری عضو باشی.\n"
            "بعد از عضویت، «بررسی عضویت همه» را بزن.")
    markup = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)
    return False


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _join_ok.pop(q.from_user.id, None)
    if await is_joined(context.bot, q.from_user.id):
        await q.answer("عضویت تأیید شد ✅")
        await ensure_user(q.from_user)
        await try_referral(q.from_user.id)
        await q.edit_message_text(
            "╭━━━「 🎰 MEOWBET | میوبت 」━━━╮\n"
            "│ 🐾 عضویت تأیید شد! خوش اومدی\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🎮 بازی‌ها داخل گروه انجام می‌شن.\n\n"
            f"🪙 موجودی شما: {format_mio(await get_balance(q.from_user.id))} میو\n\n"
            "از منوی زیر استفاده کن 👇")
        await context.bot.send_message(q.from_user.id, "👇 منوی اصلی", reply_markup=main_keyboard(q.from_user.id))
    else:
        await q.answer("هنوز عضو همه کانال‌ها نیستی. بعد از عضویت دوباره بررسی کن.", show_alert=True)


# ============================================================
# Basic user commands
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message:
        return
    referral = None
    if context.args:
        arg = context.args[0]
        if arg in ("deposit", "withdraw", "newgift", "bank"):
            await ensure_user(u)
            if not await require_join(update, context):
                return
            if arg == "deposit":
                await deposit_start(update, context)
            elif arg == "withdraw":
                await withdraw_start(update, context)
            elif arg == "newgift":
                await gift_start(update, context)
            else:
                text, kb = await build_view("bank", u, True)
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
            return
        gm = re.fullmatch(r"gift_([a-f0-9]+)", arg, re.I)
        if gm:
            await ensure_user(u)
            if not await require_join(update, context):
                return
            await gift_claim(update, context, gm.group(1).lower())
            return
        m = re.fullmatch(r"ref_(\d+)", arg)
        if m:
            referral = int(m.group(1))
    await ensure_user(u, referral)
    if not await require_join(update, context):
        return
    await try_referral(u.id)  # only pays immediately when referral_min_games = 0
    if is_private(update):
        try:  # remove the old text keyboard that older versions left on the user's screen
            tmp = await update.message.reply_text("⏳", reply_markup=ReplyKeyboardRemove())
            await tmp.delete()
        except TelegramError:
            pass
    text, kb = await build_view("home", u, is_private(update))
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await ensure_user(u)
    await update.message.reply_text(
        f"💰 موجودی لحظه‌ای شما: {format_mio(await get_balance(u.id))} میو",
        reply_to_message_id=update.message.message_id)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), reply_to_message_id=update.message.message_id)


async def games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 بازی‌های MEOWBET\n\n🎲 #زوج 20کی | #فرد 20کی\n💩 #پوپ 20کی\n🎯 #سفید 20کی | #قرمز 20کی\n⚽ #فوتبال\n\n"
        "🎯 برای دارت، بعد از ثبت بازی دقیقاً یک دارت 🎯 در همان گروه بفرست.\n"
        "⚽ برای فوتبال، بعد از #فوتبال شوت ⚽ بفرست.\n"
        "📌 بازی فوتبال بدون مبلغ‌گذاری و شرط انجام می‌شود.")


async def add_group_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    url = f"https://t.me/{me.username}?startgroup=true"
    await update.message.reply_text(
        "➕ برای افزودن MEOWBET به گروه روی دکمه زیر بزن:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ افزودن ربات به گروه", url=url)]]))


async def daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await ensure_user(u)
    now = utc_ts()
    remain, reward = 0, 0
    async with pool.acquire() as c, c.transaction():
        last = float(await c.fetchval("SELECT last_daily FROM users WHERE user_id=$1 FOR UPDATE", u.id) or 0)
        if now - last < 86400:
            remain = int(86400 - (now - last))
        else:
            reward = max(0, s_int("daily_bonus", 5000))
            await c.execute("UPDATE users SET last_daily=$2 WHERE user_id=$1", u.id, now)
            if reward:
                await adjust(c, u.id, reward, "daily_bonus")
    if remain:
        h, rem = divmod(remain, 3600)
        await update.message.reply_text(f"⏳ جایزه روزانه‌ات قبلاً دریافت شده. حدود {h} ساعت و {rem // 60} دقیقه دیگه دوباره بیا.")
        return
    await update.message.reply_text(
        f"🎁 جایزه روزانه دریافت شد!\n+{format_mio(reward)} میو\n💰 موجودی: {format_mio(await get_balance(u.id))} میو")


def get_leaderboard_rewards():
    count = min(max(s_int("leaderboard_reward_count", 0), 0), 100)
    try:
        raw = json.loads(setting("leaderboard_rewards", "{}"))
        raw = raw if isinstance(raw, dict) else {}
    except (TypeError, ValueError):
        raw = {}
    rewards = {}
    for i in range(1, count + 1):
        try:
            v = int(raw.get(str(i), 0))
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            rewards[i] = v
    return count, rewards


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count, rewards = get_leaderboard_rewards()
    rows = await pool.fetch(
        "SELECT username,first_name,balance FROM users ORDER BY balance DESC, user_id ASC LIMIT $1",
        max(10, count))
    lines = ["🏆 <b>جدول برترین‌ها</b>", ""]
    for i, r in enumerate(rows, 1):
        name = esc(f"@{r['username']}" if r["username"] else r["first_name"])
        extra = f" | 🎁 جایزه: {format_mio(rewards[i])} میو" if i in rewards else ""
        lines.append(f"{i}. {name} — {format_mio(r['balance'])} میو{extra}")
    if not rows:
        lines.append("هنوز کاربری ثبت نشده است.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    rows = await pool.fetch(
        "SELECT game,amount,result,payout FROM game_stats WHERE user_id=$1 ORDER BY id DESC LIMIT 10", u.id)
    if not rows:
        await update.message.reply_text("📜 هنوز بازی ثبت‌شده‌ای نداری.")
        return
    lines = ["📜 آخرین بازی‌های شما\n"]
    for r in rows:
        lines.append(f"{GAME_LABELS.get(r['game'], r['game'])} | {format_mio(r['amount'])} | "
                     f"{RESULT_LABELS.get(r['result'], r['result'])} | پرداخت: {format_mio(r['payout'])}")
    await update.message.reply_text("\n".join(lines))


async def referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await ensure_user(u)
    me = await context.bot.get_me()
    invited = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referral_by=$1", u.id)
    link = f"https://t.me/{me.username}?start=ref_{u.id}" if me.username else ""
    await update.message.reply_text(
        f"👥 <b>دعوت دوستان</b>\n\n🔗 لینک شما:\n<code>{link}</code>\n\n"
        f"🎁 دعوت‌کننده: {format_mio(s_int('referrer_bonus', 100000))} میو\n"
        f"🎁 دعوت‌شده: {format_mio(s_int('referred_bonus', 50000))} میو\n"
        f"✅ پاداش بعد از انجام {s_int('referral_min_games', 3)} بازی توسط دعوت‌شده فعال می‌شود.\n"
        f"👥 تعداد دعوت‌ها: {invited}", parse_mode="HTML")


# ============================================================
# Finance (deposit / withdraw) and gift links
# ============================================================
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not is_private(update):
        await m.reply_text("💳 واریز فقط در چت خصوصی با ربات انجام می‌شود.")
        return
    if not await require_join(update, context):
        return
    card = payment_card()
    if not card:
        await m.reply_text("❌ حساب بانک میویی برای واریز هنوز توسط مالک تنظیم نشده است.")
        return
    context.user_data.clear()
    context.user_data["finance_action"] = "deposit"
    await m.reply_text(
        f"{pe('➕')} <b>واریز میو</b>\n\nیکی از مبلغ‌ها رو بزن یا مبلغ دلخواهت رو بنویس (مثال: 50000 یا 50k).\n\n"
        f"🏦 حساب بانک میویی (مقصد واریز):\n<code>{esc(card)}</code>",
        parse_mode="HTML", reply_markup=deposit_presets(update.effective_user.id))


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not is_private(update):
        await m.reply_text("💸 برداشت فقط در چت خصوصی با ربات انجام می‌شود.")
        return
    if not await require_join(update, context):
        return
    context.user_data.clear()
    context.user_data["finance_action"] = "withdraw"
    await m.reply_text(
        f"{pe('➖')} <b>برداشت میو</b>\n\n"
        f"حداقل برداشت: {format_mio(s_int('min_withdraw', 100000))} میو\n"
        "یکی از مبلغ‌ها رو بزن یا مبلغ دلخواهت رو بنویس.",
        parse_mode="HTML", reply_markup=withdraw_presets(update.effective_user.id))


async def gift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not is_private(update):
        await m.reply_text("🎁 ساخت لینک هدیه فقط در چت خصوصی با ربات انجام می‌شود.")
        return
    if not await require_join(update, context):
        return
    context.user_data.clear()
    context.user_data["flow"] = "gift"
    context.user_data["gift_step"] = "capacity"
    await m.reply_text("🎁 <b>لینک هدیه</b>\n\nظرفیت (تعداد نفرات) رو انتخاب کن یا عدد دلخواهت رو بنویس:",
                       parse_mode="HTML", reply_markup=gift_capacity_presets(update.effective_user.id))


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ عملیات لغو شد.", reply_markup=main_keyboard(update.effective_user.id))


async def _notify_admin(bot, text: str, tx: str) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"fin:approve:{tx}"),
        InlineKeyboardButton("❌ رد", callback_data=f"fin:reject:{tx}"),
    ]])
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        logger.exception("could not notify admin about %s", tx)


async def finance_and_gift_text(update: Update, context: ContextTypes.DEFAULT_TYPE, override: Optional[str] = None):
    u = update.effective_user
    msg = update.effective_message
    if not u or not msg or not is_private(update):
        return
    text = (override if override is not None else (msg.text or "")).strip()
    if u.id == ADMIN_ID and context.user_data.get("admin_action"):
        return
    flow = context.user_data.get("flow")
    action = context.user_data.get("finance_action")
    gift_step = context.user_data.get("gift_step")
    if not (flow == "gift" or action):
        return

    # ---------------- gift creation ----------------
    if flow == "gift" and gift_step == "capacity":
        try:
            capacity = int(text.translate(_DIGITS).replace(",", "").replace("٬", ""))
        except (TypeError, ValueError):
            capacity = 0
        if not 1 <= capacity <= 100000:
            await msg.reply_text("❌ ظرفیت باید یک عدد بین 1 تا 100000 باشد.")
            return
        context.user_data["gift_capacity"] = capacity
        context.user_data["gift_step"] = "amount"
        await msg.reply_text("💰 مبلغ هدیه برای هر نفر رو انتخاب کن یا بنویس:",
                             reply_markup=gift_amount_presets(u.id))
        return

    if flow == "gift" and gift_step == "amount":
        amount = parse_amount(text)
        capacity = int(context.user_data.get("gift_capacity", 0))
        if amount <= 0:
            await msg.reply_text("❌ مبلغ نامعتبر است. مثال: 50000")
            return
        if capacity <= 0:
            context.user_data.clear()
            await msg.reply_text("❌ اطلاعات لینک منقضی شد؛ دوباره «لینک هدیه» را بزن.")
            return
        total = amount * capacity
        try:
            me = await context.bot.get_me()
            if not me.username:
                raise RuntimeError("bot has no username")
        except Exception:
            logger.exception("gift link: get_me failed")
            context.user_data.clear()
            await msg.reply_text("❌ ساخت لینک ممکن نشد؛ هیچ مبلغی از موجودی کم نشد.")
            return
        code = os.urandom(12).hex()
        try:
            async with pool.acquire() as c, c.transaction():
                await adjust(c, u.id, -total, "gift_reserve", code)
                await c.execute(
                    "INSERT INTO gift_links(code,owner_id,amount_per_user,capacity,claimed_count,remaining_balance,active) "
                    "VALUES($1,$2,$3,$4,0,$5,TRUE)", code, u.id, amount, capacity, total)
        except InsufficientFunds:
            bal = await get_balance(u.id)
            context.user_data.clear()
            await msg.reply_text(
                f"❌ موجودی کافی نیست.\n💰 نیاز: {format_mio(total)} میو\n💳 موجودی: {format_mio(bal)} میو")
            return
        except Exception:
            logger.exception("gift_create_failed user=%s", u.id)
            context.user_data.clear()
            await msg.reply_text("❌ خطا هنگام ثبت لینک هدیه بود؛ موجودی تغییر نکرد.")
            return
        link = f"https://t.me/{me.username}?start=gift_{code}"
        context.user_data.clear()
        await msg.reply_text(
            f"🎁 <b>لینک هدیه ساخته شد!</b>\n\n👥 ظرفیت: {capacity} نفر\n"
            f"💰 هدیه هر نفر: {format_mio(amount)} میو\n💳 مبلغ رزروشده: {format_mio(total)} میو\n\n"
            f"🔗 <a href=\"{esc(link)}\">باز کردن لینک هدیه</a>\n<code>{esc(link)}</code>\n\n"
            "هر کاربر فقط یک بار می‌تواند از این لینک استفاده کند.",
            parse_mode="HTML", disable_web_page_preview=True)
        return

    # ---------------- deposit ----------------
    if action == "deposit":
        amount = parse_amount(text)
        if amount <= 0:
            await msg.reply_text("❌ مبلغ نامعتبر است. مثال: 50000")
            return
        context.user_data["deposit_amount"] = amount
        context.user_data["finance_action"] = "deposit_sender"
        await msg.reply_text(
            f"💳 مبلغ: {format_mio(amount)} میو\n\n"
            "🏦 حساب بانک میویی خودت (۱۲ رقمی) را بفرست؛ همان حسابی که از آن واریز می‌کنی.")
        return

    if action == "deposit_sender":
        amount = int(context.user_data.get("deposit_amount", 0))
        if amount <= 0:
            context.user_data.clear()
            await msg.reply_text("❌ درخواست واریز منقضی شد؛ دوباره واریز را بزن.")
            return
        acct = re.sub(r"[\s\-]", "", text.translate(_DIGITS))
        if not re.fullmatch(r"\d{12}", acct):
            await msg.reply_text("❌ حساب بانک میویی باید دقیقاً ۱۲ رقم باشد.")
            return
        if acct == re.sub(r"\D", "", payment_card()):
            await msg.reply_text("❌ این حساب خودِ مقصد واریز است؛ حساب خودت را بفرست.")
            return
        owner = await pool.fetchval("SELECT user_id FROM bank_accounts WHERE account=$1", acct)
        busy = await pool.fetchval(
            "SELECT user_id FROM transactions WHERE kind='deposit' AND status='pending' AND card=$1 AND user_id<>$2 LIMIT 1",
            acct, u.id)
        if (owner and owner != u.id) or busy:
            await msg.reply_text("❌ این حساب برای کاربر دیگری ثبت شده یا در حال استفاده است.")
            return
        context.user_data["deposit_sender"] = acct
        if auto_ready():
            if await pool.fetchval(
                    "SELECT 1 FROM transactions WHERE user_id=$1 AND kind='deposit' AND status='pending' AND receipt='auto' LIMIT 1", u.id):
                context.user_data.clear()
                await msg.reply_text("⏳ یک واریز در انتظار تأیید خودکار داری؛ اول همان را انجام بده یا منتظر انقضایش بمان.")
                return
            tx = os.urandom(8).hex()
            try:
                await pool.execute(
                    "INSERT INTO transactions(tx_id,user_id,kind,amount,status,receipt,card) "
                    "VALUES($1,$2,'deposit',$3,'pending','auto',$4)", tx, u.id, amount, acct)
            except Exception:
                logger.exception("auto deposit request failed user=%s", u.id)
                context.user_data.clear()
                await msg.reply_text("❌ ثبت درخواست ناموفق بود. دوباره تلاش کن.")
                return
            context.user_data.clear()
            await msg.reply_text(
                f"✅ درخواست ثبت شد.\n\nاز حساب <code>{esc(acct)}</code> مبلغ {format_mio(amount)} میو را به حساب بانک میویی\n"
                f"<code>{esc(payment_card())}</code>\nواریز کن.\n\n"
                f"⏱ تا {s_int('deposit_window_minutes', 60)} دقیقه فرصت داری؛ بعد از واریز، خودکار و چند ثانیه‌ای شارژ می‌شود.",
                parse_mode="HTML")
            return
        context.user_data["finance_action"] = "deposit_receipt"
        await msg.reply_text(
            f"💳 مبلغ: {format_mio(amount)} میو\n🏦 حساب بانک میویی مقصد: <code>{esc(payment_card())}</code>\n\n"
            "بعد از انتقال در بانک میویی، متن رسید یا کد پیگیری را ارسال کن.", parse_mode="HTML")
        return

    if action == "deposit_receipt":
        amount = int(context.user_data.get("deposit_amount", 0))
        sender = context.user_data.get("deposit_sender", "")
        if amount <= 0:
            context.user_data.clear()
            await msg.reply_text("❌ درخواست واریز منقضی شد؛ دوباره واریز را بزن.")
            return
        tx = os.urandom(8).hex()
        try:
            await pool.execute(
                "INSERT INTO transactions(tx_id,user_id,kind,amount,status,receipt,card) "
                "VALUES($1,$2,'deposit',$3,'pending',$4,$5)", tx, u.id, amount, text[:500], sender)
        except Exception:
            logger.exception("deposit_request_failed user=%s", u.id)
            context.user_data.clear()
            await msg.reply_text("❌ ثبت درخواست واریز ناموفق بود. دوباره تلاش کن.")
            return
        context.user_data.clear()
        await msg.reply_text("✅ درخواست واریز ثبت شد و برای مالک ارسال شد.")
        await _notify_admin(
            context.bot,
            f"💳 <b>درخواست واریز</b>\n\n🆔 {u.id}\n👤 {uname(u)}\n💰 {format_mio(amount)} میو\n"
            f"🏦 حساب مبدأ: <code>{esc(sender)}</code>\n🧾 رسید: {esc(text[:500])}\n🔖 {tx}", tx)
        return

    # ---------------- withdraw ----------------
    # The balance is NOT touched until the final step, so abandoning the flow never loses money.
    if action == "withdraw":
        amount = parse_amount(text)
        if amount <= 0:
            await msg.reply_text("❌ مبلغ نامعتبر است. مثال: 50000")
            return
        min_w = s_int("min_withdraw", 100000)
        if amount < min_w:
            await msg.reply_text(f"❌ حداقل برداشت {format_mio(min_w)} میو است.")
            return
        bal = await get_balance(u.id)
        if amount > bal:
            await msg.reply_text(f"❌ موجودی کافی نیست: {format_mio(bal)} میو")
            context.user_data.clear()
            return
        if await pool.fetchval(
                "SELECT 1 FROM transactions WHERE user_id=$1 AND kind='withdraw' AND status='pending' LIMIT 1", u.id):
            await msg.reply_text("⏳ یک درخواست برداشت در انتظار بررسی داری؛ اول منتظر نتیجه‌اش باش.")
            context.user_data.clear()
            return
        context.user_data["withdraw_amount"] = amount
        context.user_data["finance_action"] = "withdraw_card"
        await msg.reply_text(
            "🏦 حساب بانک میویی مقصد را ارسال کن.\nℹ️ مبلغ بعد از ثبت نهایی درخواست از موجودی کم می‌شود.")
        return

    if action == "withdraw_card":
        amount = int(context.user_data.get("withdraw_amount", 0))
        if amount <= 0:
            context.user_data.clear()
            await msg.reply_text("❌ درخواست برداشت منقضی شد؛ دوباره برداشت را بزن.")
            return
        card = re.sub(r"[\s\-]", "", text.translate(_DIGITS))
        if not re.fullmatch(r"\d{12}", card):
            await msg.reply_text("❌ حساب بانک میویی باید دقیقاً ۱۲ رقم باشد.")
            return
        tx = os.urandom(8).hex()
        try:
            async with pool.acquire() as c, c.transaction():
                await adjust(c, u.id, -amount, "withdraw_reserve", tx)
                await c.execute(
                    "INSERT INTO transactions(tx_id,user_id,kind,amount,status,receipt,card) "
                    "VALUES($1,$2,'withdraw',$3,'pending','',$4)", tx, u.id, amount, card)
        except InsufficientFunds:
            context.user_data.clear()
            await msg.reply_text("❌ موجودی کافی نیست.")
            return
        except Exception:
            logger.exception("withdraw_request_failed user=%s", u.id)
            context.user_data.clear()
            await msg.reply_text("❌ ثبت درخواست برداشت ناموفق بود؛ موجودی تغییری نکرد.")
            return
        context.user_data.clear()
        await msg.reply_text("✅ درخواست برداشت ثبت شد و برای مالک ارسال شد.")
        stats = await pool.fetchrow("SELECT games_count,wins_count FROM users WHERE user_id=$1", u.id)
        dep = await pool.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=$1 AND kind='deposit' AND status='approved'", u.id)
        await _notify_admin(
            context.bot,
            f"💸 <b>درخواست برداشت</b>\n\n🆔 {u.id}\n👤 {uname(u)}\n💰 {format_mio(amount)} میو\n"
            f"🏦 حساب بانک میویی مقصد: <code>{esc(card)}</code>\n"
            f"📊 بازی‌ها: {stats['games_count']} | برد: {stats['wins_count']} | واریز تأییدشده: {format_mio(dep)}\n🔖 {tx}", tx)
        return


async def gift_claim(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    u = update.effective_user
    await ensure_user(u)
    err, amount = None, 0
    async with pool.acquire() as c, c.transaction():
        gift = await c.fetchrow("SELECT * FROM gift_links WHERE code=$1 AND active FOR UPDATE", code)
        if not gift or gift["claimed_count"] >= gift["capacity"]:
            err = "❌ این لینک هدیه تمام شده یا نامعتبر است."
        elif gift["owner_id"] == u.id:
            err = "❌ سازنده لینک نمی‌تواند هدیه خودش را دریافت کند."
        elif await c.fetchval("SELECT 1 FROM gift_claims WHERE code=$1 AND user_id=$2", code, u.id):
            err = "❌ تو قبلاً از این لینک هدیه استفاده کرده‌ای."
        else:
            amount = int(gift["amount_per_user"])
            if amount <= 0 or gift["remaining_balance"] < amount:
                err = "❌ این لینک هدیه وضعیت معتبری ندارد."
            else:
                new_count = gift["claimed_count"] + 1
                await c.execute(
                    "UPDATE gift_links SET claimed_count=$2, remaining_balance=remaining_balance-$3, active=$4 WHERE code=$1",
                    code, new_count, amount, new_count < gift["capacity"])
                await c.execute("INSERT INTO gift_claims(code,user_id,amount) VALUES($1,$2,$3)", code, u.id, amount)
                await adjust(c, u.id, amount, "gift_claim", code)
    if err:
        await update.message.reply_text(err)
        return
    await update.message.reply_text(
        f"🎁 <b>هدیه دریافت شد!</b>\n\n💰 +{format_mio(amount)} میو\n💳 موجودی: {format_mio(await get_balance(u.id))} میو",
        parse_mode="HTML")


async def render_pending(q) -> None:
    rows = await pool.fetch("SELECT * FROM transactions WHERE status='pending' ORDER BY created_at DESC LIMIT 20")
    if not rows:
        await safe_edit(q, "💳 درخواست مالی در انتظار وجود ندارد.", reply_markup=back_admin())
        return
    kb, lines = [], ["💳 درخواست‌های مالی", ""]
    for r in rows:
        icon = "➕" if r["kind"] == "deposit" else "➖"
        lines.append(f"{icon} {r['tx_id']} | {r['user_id']} | {format_mio(r['amount'])} میو")
        kb.append([InlineKeyboardButton(f"✅ {r['tx_id']}", callback_data=f"fin:approve:{r['tx_id']}"),
                   InlineKeyboardButton("❌", callback_data=f"fin:reject:{r['tx_id']}")])
    kb.append([InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")])
    await safe_edit(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def finance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        _, action, tx = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    if action not in ("approve", "reject"):
        await q.answer()
        return
    row = None
    async with pool.acquire() as c, c.transaction():
        # FOR UPDATE + status check inside one transaction: a double click can never pay twice.
        row = await c.fetchrow("SELECT * FROM transactions WHERE tx_id=$1 FOR UPDATE", tx)
        if row and row["status"] == "pending":
            status = "approved" if action == "approve" else "rejected"
            await c.execute("UPDATE transactions SET status=$2, admin_id=$3, updated_at=now() WHERE tx_id=$1",
                            tx, status, ADMIN_ID)
            if action == "approve" and row["kind"] == "deposit":
                await adjust(c, row["user_id"], row["amount"], "deposit_approved", tx)
            elif action == "reject" and row["kind"] == "withdraw":
                await adjust(c, row["user_id"], row["amount"], "withdraw_rejected_refund", tx)
        else:
            row = None
    if row is None:
        await q.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
        return
    msg = "تأیید شد" if action == "approve" else "رد شد"
    await q.answer(msg)
    notify(row["user_id"], f"📋 درخواست {'واریز' if row['kind'] == 'deposit' else 'برداشت'} شما {msg}.")
    if (q.message.text or "").startswith("💳 درخواست‌های مالی"):
        await render_pending(q)
    else:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass


async def safe_edit(q, text, **kw) -> None:
    try:
        await q.edit_message_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")]])


# ============================================================
# Groups tracking
# ============================================================
_known_groups: dict = {}


async def remember_group(update: Update) -> None:
    c = update.effective_chat
    if not c or c.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    title, uname_ = c.title or "", getattr(c, "username", "") or ""
    if _known_groups.get(c.id) == (title, uname_):
        return  # no DB write per message
    await pool.execute(
        "INSERT INTO group_chats(chat_id,title,username,enabled) VALUES($1,$2,$3,TRUE) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, username=EXCLUDED.username, "
        "enabled=TRUE, updated_at=now()", c.id, title, uname_)
    _known_groups[c.id] = (title, uname_)


async def track_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_group(update)


# ============================================================
# Shared betting helpers
# ============================================================
async def has_active_game(conn, uid: int) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM dice_bets WHERE user_id=$1) "
        "OR EXISTS(SELECT 1 FROM dart_bets WHERE user_id=$1) "
        "OR EXISTS(SELECT 1 FROM poop_games WHERE user_id=$1)", uid))


async def place_bet(uid: int, amount: int, reason: str, insert: Callable[..., Awaitable]) -> None:
    """Atomically: lock user -> make sure no other game is active -> take the stake -> create the game row.
    Fixes the old race where two quick messages could take the stake twice."""
    async with pool.acquire() as c, c.transaction():
        if not await c.fetchval("SELECT 1 FROM users WHERE user_id=$1 FOR UPDATE", uid):
            raise UserNotFound()
        if await has_active_game(c, uid):
            raise ActiveGame()
        await adjust(c, uid, -amount, reason)
        await insert(c)


async def try_place(update: Update, uid: int, amount: int, reason: str, insert) -> bool:
    try:
        await place_bet(uid, amount, reason, insert)
        return True
    except ActiveGame:
        await update.message.reply_text("⚠️ یک بازی فعال داری؛ اول همان را تمام کن.")
    except InsufficientFunds:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی: {format_mio(await get_balance(uid))} میو")
    except UserNotFound:
        await update.message.reply_text("❌ ابتدا /start را بزن.")
    return False


async def check_amount(update: Update, amount: int) -> bool:
    if amount < MIN_GAME:
        await update.message.reply_text(f"❌ حداقل بازی {format_mio(MIN_GAME)} میو است.")
        return False
    if amount > MAX_GAME:
        await update.message.reply_text(f"❌ حداکثر هر بازی {format_mio(MAX_GAME)} میو است.")
        return False
    return True


# ============================================================
# Dice (even / odd, 3 dice)
# ============================================================
async def start_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update) or not update.message:
        return
    await remember_group(update)
    if not await require_join(update, context):
        return
    m = re.match(r"^#(زوج|فرد)\s+(.+)$", (update.message.text or "").strip())
    if not m:
        return
    u = update.effective_user
    await ensure_user(u)
    target, amount = m.group(1), parse_amount(m.group(2))
    if not await check_amount(update, amount):
        return
    chat_id = update.effective_chat.id

    async def insert(c):
        await c.execute(
            "INSERT INTO dice_bets(user_id,target,amount,dices,chat_id,created_at) VALUES($1,$2,$3,'[]',$4,$5)",
            u.id, target, amount, chat_id, utc_ts())

    if not await try_place(update, u.id, amount, "dice_bet", insert):
        return
    msg = await update.message.reply_text(
        f"🎲 <b>بازی تاس شروع شد</b>\n\n👤 کاربر {uname(u)}\n🎯 شرط: {target}\n💰 مبلغ: {format_mio(amount)} میو\n\n"
        "🎲 دقیقاً ۳ تاس واقعی تلگرام بفرست.\n🚫 تاس فورواردشده حساب نمی‌شود.\n"
        f"⏱️ {DICE_TIMEOUT} ثانیه فرصت داری؛ بعد از آن {int(TIMEOUT_PENALTY * 100)}٪ مبلغ جریمه می‌شود.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو شرط", callback_data=f"cancel_dice:{u.id}")]]))
    await pool.execute("UPDATE dice_bets SET message_id=$2 WHERE user_id=$1", u.id, msg.message_id)


async def handle_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u = update.message, update.effective_user
    if not msg or not msg.dice or msg.dice.emoji != "🎲" or not u or not is_group(update):
        return
    warn, progress, result = None, None, None
    async with pool.acquire() as c, c.transaction():
        bet = await c.fetchrow("SELECT * FROM dice_bets WHERE user_id=$1 FOR UPDATE", u.id)
        if not bet or int(bet["chat_id"]) != update.effective_chat.id:
            return
        if is_forwarded(msg):
            warn = f"⚠️ {uname(u)} این تاس فوروارد شده است و ثبت نمی‌شود."
        else:
            dices = json.loads(bet["dices"])
            if len(dices) >= 3:
                return
            dices.append(int(msg.dice.value))
            if len(dices) < 3:
                await c.execute("UPDATE dice_bets SET dices=$2 WHERE user_id=$1", u.id, json.dumps(dices))
                progress = f"🎲 {len(dices)} از ۳ تاس ثبت شد؛ {3 - len(dices)} تاس دیگر بفرست."
            else:
                total = sum(dices)
                even = total % 2 == 0
                won = (bet["target"] == "زوج" and even) or (bet["target"] == "فرد" and not even)
                mult = s_float("dice_mult", 1.5)
                prize, fee = (apply_fee(bet["amount"], int(bet["amount"] * mult)) if won else (0, 0))
                await c.execute("DELETE FROM dice_bets WHERE user_id=$1", u.id)
                if won:
                    bal = await adjust(c, u.id, prize, "dice_win")
                else:
                    bal = int(await c.fetchval("SELECT balance FROM users WHERE user_id=$1", u.id))
                await log_game(c, u.id, "dice", bet["amount"], "win" if won else "loss", prize, fee)
                result = (won, dices, total, even, bet, mult, prize, bal, fee)
    reply = dict(reply_to_message_id=msg.message_id)
    if warn:
        await msg.reply_text(warn, parse_mode="HTML", **reply)
    elif progress:
        await msg.reply_text(progress, **reply)
    elif result:
        won, dices, total, even, bet, mult, prize, bal, fee = result
        outcome = "زوج" if even else "فرد"
        dice_text = " + ".join(map(str, dices))
        if won:
            text = (f"🎉 <b>بردیییی!</b>\n\n👤 {uname(u)}\n🎲 {dice_text} = {total}\n📌 نتیجه: {outcome}\n"
                    f"🎯 شرط: {bet['target']}\n🪙 جایزه (×{mult:g}): {format_mio(prize)} میو\n{fee_line(fee)}💰 موجودی: {format_mio(bal)} میو")
        else:
            text = (f"💥 <b>باختیییی!</b>\n\n👤 {uname(u)}\n🎲 {dice_text} = {total}\n📌 نتیجه: {outcome}\n"
                    f"🎯 شرط: {bet['target']}\n💰 مبلغ شرط: {format_mio(bet['amount'])} میو از دست رفت.\n💰 موجودی: {format_mio(bal)} میو")
        await msg.reply_text(text, parse_mode="HTML", **reply)


async def cancel_dice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        owner = int(q.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    if owner != uid:
        await q.answer("این شرط مال تو نیست.", show_alert=True)
        return
    bet = None
    async with pool.acquire() as c, c.transaction():
        bet = await c.fetchrow("DELETE FROM dice_bets WHERE user_id=$1 AND dices='[]' RETURNING *", uid)
        if bet:
            await adjust(c, uid, bet["amount"], "dice_cancel")
    if not bet:
        await q.answer("بعد از اولین تاس لغو ممکن نیست.", show_alert=True)
        return
    await q.answer("لغو شد")
    await safe_edit(q, f"❌ شرط لغو شد.\n💰 {format_mio(bet['amount'])} میو برگشت.\n💰 موجودی: {format_mio(await get_balance(uid))} میو")


# ============================================================
# Dart
# ============================================================
async def start_dart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update) or not update.message:
        return
    await remember_group(update)
    if not await require_join(update, context):
        return
    m = re.match(r"^#(سفید|قرمز)\s+(.+)$", (update.message.text or "").strip())
    if not m:
        return
    u = update.effective_user
    await ensure_user(u)
    target, amount = m.group(1), parse_amount(m.group(2))
    if not await check_amount(update, amount):
        return
    chat_id = update.effective_chat.id

    async def insert(c):
        await c.execute("INSERT INTO dart_bets(user_id,target,amount,chat_id,created_at) VALUES($1,$2,$3,$4,$5)",
                        u.id, target, amount, chat_id, utc_ts())

    if not await try_place(update, u.id, amount, "dart_bet", insert):
        return
    await update.message.reply_text(
        f"🎯 <b>شرط دارت ثبت شد</b>\n🎯 هدف: {target}\n💰 مبلغ: {format_mio(amount)} میو\n\n"
        f"یک دارت 🎯 همین‌جا در گروه بفرست.\n⏱️ {DART_TIMEOUT} ثانیه فرصت داری.", parse_mode="HTML")


async def handle_dart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u = update.message, update.effective_user
    if not msg or not msg.dice or msg.dice.emoji != "🎯" or not u or not is_group(update):
        return
    warn, result = None, None
    async with pool.acquire() as c, c.transaction():
        bet = await c.fetchrow("SELECT * FROM dart_bets WHERE user_id=$1 AND chat_id=$2 FOR UPDATE",
                               u.id, update.effective_chat.id)
        if not bet:
            return
        if is_forwarded(msg):
            warn = "⚠️ دارت فوروارد شده ثبت نمی‌شود؛ یک دارت واقعی بفرست."
        else:
            white = set(parse_int_list(setting("dart_white_values"), [1, 3, 5]))
            color = "سفید" if int(msg.dice.value) in white else "قرمز"
            won = color == bet["target"]
            prize, fee = (apply_fee(bet["amount"], int(bet["amount"] * s_float("dart_multiplier", 2.0))) if won else (0, 0))
            await c.execute("DELETE FROM dart_bets WHERE user_id=$1", u.id)
            if won:
                await adjust(c, u.id, prize, "dart_win")
            await log_game(c, u.id, "dart", bet["amount"], "win" if won else "loss", prize, fee)
            result = (won, color, prize, bet["amount"], fee)
    reply = dict(reply_to_message_id=msg.message_id)
    if warn:
        await msg.reply_text(warn, **reply)
    elif result:
        won, color, prize, amount, fee = result
        if won:
            await msg.reply_text(f"🎯🎉 بردی! نتیجه: {color}\n💰 جایزه: {format_mio(prize)} میو\n{fee_line(fee)}".rstrip(), **reply)
        else:
            await msg.reply_text(f"🎯💥 باختی! نتیجه: {color}\n💰 {format_mio(amount)} میو از دست رفت.", **reply)


async def handle_dice_or_dart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.dice:
        return
    emoji = update.message.dice.emoji
    if emoji == "🎯":
        await handle_dart(update, context)
    elif emoji == "🎲":
        await handle_dice(update, context)
    elif emoji == "⚽":
        await handle_football(update, context)


# ============================================================
# Football (no stake)
# ============================================================
def football_config():
    goals = sorted({x for x in parse_int_list(setting("football_goal_values"), [4, 5]) if 1 <= x <= 5}) or [4, 5]
    shots = s_int("football_shots", 3)
    return (shots if 1 <= shots <= 10 else 3), goals


async def start_football(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update) or not update.message:
        return
    await remember_group(update)
    if not await require_join(update, context):
        return
    if not re.fullmatch(r"#فوتبال(?:\u200c)?", (update.message.text or "").strip()):
        return
    u = update.effective_user
    await ensure_user(u)
    if await pool.fetchval("SELECT 1 FROM football_games WHERE user_id=$1", u.id):
        await update.message.reply_text("⚠️ یک بازی فوتبال فعال داری؛ اول شوت‌های باقی‌مانده را بفرست.",
                                        reply_to_message_id=update.message.message_id)
        return
    shots, goal_values = football_config()
    await pool.execute(
        "INSERT INTO football_games(user_id,chat_id,message_id,shots,goals,created_at) VALUES($1,$2,$3,0,0,$4) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, message_id=EXCLUDED.message_id, "
        "shots=0, goals=0, created_at=EXCLUDED.created_at",
        u.id, update.effective_chat.id, update.message.message_id, utc_ts())
    await update.message.reply_text(
        f"⚽ <b>بازی فوتبال شروع شد!</b>\n\n🎯 تعداد شوت: {shots}\n⚽ هر بار یک توپ فوتبال بفرست.\n"
        f"🥅 اعداد گل: {', '.join(map(str, goal_values))}",
        parse_mode="HTML", reply_to_message_id=update.message.message_id)


async def handle_football(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u = update.message, update.effective_user
    if not msg or not msg.dice or msg.dice.emoji != "⚽" or not u or not is_group(update):
        return
    max_shots, goal_values = football_config()
    warn, out = None, None
    async with pool.acquire() as c, c.transaction():
        g = await c.fetchrow("SELECT * FROM football_games WHERE user_id=$1 FOR UPDATE", u.id)
        if not g or int(g["chat_id"]) != update.effective_chat.id:
            return
        if is_forwarded(msg):
            warn = "⚠️ توپ فوروارد شده ثبت نمی‌شود."
        else:
            scored = int(msg.dice.value) in goal_values
            shots, goals = g["shots"] + 1, g["goals"] + (1 if scored else 0)
            finished = shots >= max_shots
            if finished:
                await c.execute("DELETE FROM football_games WHERE user_id=$1", u.id)
            else:
                await c.execute("UPDATE football_games SET shots=$2, goals=$3 WHERE user_id=$1", u.id, shots, goals)
            out = (scored, shots, goals, finished)
    reply = dict(reply_to_message_id=msg.message_id)
    if warn:
        await msg.reply_text(warn, **reply)
    elif out:
        scored, shots, goals, finished = out
        outcome = "🥅 گل!" if scored else "🧤 مهار شد!"
        if finished:
            await msg.reply_text(f"⚽ <b>بازی تمام شد</b>\n\n{outcome}\n🎯 شوت: {shots}/{max_shots}\n🥅 گل‌های ثبت‌شده: {goals}",
                                 parse_mode="HTML", **reply)
        else:
            await msg.reply_text(f"⚽ {outcome}\n🎯 شوت {shots}/{max_shots}\n🥅 گل‌ها: {goals}", **reply)


# ============================================================
# Poop
# ============================================================
def poop_config() -> dict:
    rows = max(1, s_int("poop_rows", POOP_ROWS))
    cols = max(2, s_int("poop_cols", POOP_COLS))
    raw_counts = parse_int_list(setting("poop_counts"), POOP_COUNTS)
    raw_mults = parse_float_list(setting("poop_mults"), POOP_MULTIPLIERS)
    counts = (raw_counts + [0] * rows)[:rows]
    counts = [min(max(0, n), cols - 1) for n in counts]  # always leave at least one safe cell
    mults = (raw_mults + [raw_mults[-1]] * rows)[:rows]
    return {"rows": rows, "cols": cols, "counts": counts, "mults": mults}


def make_poops(cfg: dict):
    return [sorted(random.sample(range(cfg["cols"]), n)) for n in cfg["counts"]]


def game_cfg(game: dict) -> dict:
    """Use the config snapshot stored with the game, so admin changes never break running games."""
    cfg = game.get("config") or {}
    if all(k in cfg for k in ("rows", "cols", "counts", "mults")):
        return cfg
    return poop_config()


def poop_from_row(r) -> dict:
    return {"amount": int(r["amount"]), "step": int(r["step"]), "poops": json.loads(r["poops"]),
            "history": json.loads(r["history"]), "chat_id": int(r["chat_id"]),
            "message_id": r["message_id"], "config": json.loads(r["config"] or "{}")}


async def save_poop(conn, uid: int, game: dict) -> None:
    await conn.execute(
        "INSERT INTO poop_games(user_id,amount,step,poops,history,chat_id,message_id,config,created_at) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(user_id) DO UPDATE SET amount=EXCLUDED.amount, "
        "step=EXCLUDED.step, poops=EXCLUDED.poops, history=EXCLUDED.history, chat_id=EXCLUDED.chat_id, "
        "message_id=EXCLUDED.message_id, config=EXCLUDED.config",
        uid, game["amount"], game["step"], json.dumps(game["poops"]), json.dumps(game["history"]),
        game["chat_id"], game.get("message_id"), json.dumps(game["config"]), utc_ts())


def poop_keyboard(game: dict) -> InlineKeyboardMarkup:
    cfg = game_cfg(game)
    step, buttons = game["step"], []
    for row in range(cfg["rows"]):
        rb = []
        for col in range(cfg["cols"]):
            if row < step:
                rb.append(InlineKeyboardButton(game["history"][row][col], callback_data="poop_noop"))
            elif row == step:
                rb.append(InlineKeyboardButton("⚪️", callback_data=f"poop_play:{row}:{col}"))
            else:
                rb.append(InlineKeyboardButton("🔒", callback_data="poop_noop"))
        buttons.append(rb)
    if step > 0:
        cash = apply_fee(game["amount"], int(game["amount"] * cfg["mults"][step - 1]))[0]
        buttons.append([InlineKeyboardButton(f"💰 برداشت {format_mio(cash)} میو", callback_data="poop_cashout")])
    return InlineKeyboardMarkup(buttons)


def poop_text(game: dict) -> str:
    cfg = game_cfg(game)
    step = game["step"]
    lines = [f"🎮 مرحله {step + 1} از {cfg['rows']}", f"📈 ضریب این مرحله: ×{cfg['mults'][step]:g}",
             f"🪙 شرط: {format_mio(game['amount'])} میو"]
    if step > 0:
        lines.append(f"💰 برداشت فعلی: {format_mio(apply_fee(game['amount'], int(game['amount'] * cfg['mults'][step - 1]))[0])} میو")
    return "💩 <b>بازی پوپ</b>\n\n" + "\n".join(lines)


async def start_poop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update) or not update.message:
        return
    await remember_group(update)
    if not await require_join(update, context):
        return
    m = re.match(r"^#پوپ(?:\u200c)?\s+(.+?)\s*$", (update.message.text or "").strip())
    if not m:
        return
    u = update.effective_user
    await ensure_user(u)
    amount = parse_amount(m.group(1))
    if not await check_amount(update, amount):
        return
    if await has_active_game(pool, u.id):
        await update.message.reply_text("⚠️ یک بازی فعال داری؛ اول همان را تمام کن.")
        return
    bal = await get_balance(u.id)
    if amount > bal:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی: {format_mio(bal)} میو")
        return
    cfg = poop_config()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ شروع بازی", callback_data=f"poop_go:{u.id}:{amount}"),
        InlineKeyboardButton("❌ لغو", callback_data=f"poop_no:{u.id}"),
    ]])
    await update.message.reply_text(
        "💩 <b>تأیید شروع بازی پوپ</b>\n\n"
        f"🪙 مبلغ: {format_mio(amount)} میو\n🎯 {cfg['rows']} ردیف و {cfg['cols']} خانه\n"
        f"💩 تعداد پوپ‌ها: {' / '.join(map(str, cfg['counts']))}\n"
        f"📈 ضرایب: {' / '.join('×' + format(x, 'g') for x in cfg['mults'])}\n\n"
        "مطمئن هستی می‌خواهی بازی را شروع کنی؟", parse_mode="HTML", reply_markup=kb)


async def poop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, data = q.from_user.id, q.data or ""
    if data == "poop_noop":
        await q.answer()
        return

    if data.startswith("poop_no:"):
        if data.split(":")[1] != str(uid):
            await q.answer("این دکمه برای تو نیست.", show_alert=True)
            return
        await q.answer("لغو شد")
        await safe_edit(q, "❌ شروع بازی لغو شد.")
        return

    if data.startswith("poop_go:"):
        try:
            _, owner, amt = data.split(":")
            amount = int(amt)
        except ValueError:
            await q.answer()
            return
        if int(owner) != uid:
            await q.answer("این دکمه برای تو نیست.", show_alert=True)
            return
        if not await require_join(update, context):
            return
        cfg = poop_config()
        game = {"amount": amount, "step": 0, "poops": make_poops(cfg),
                "history": [["⚪️"] * cfg["cols"] for _ in range(cfg["rows"])],
                "chat_id": q.message.chat_id, "message_id": q.message.message_id, "config": cfg}

        async def insert(c):
            await save_poop(c, uid, game)

        try:
            await place_bet(uid, amount, "poop_bet", insert)
        except ActiveGame:
            await q.answer("یک بازی فعال داری.", show_alert=True)
            return
        except InsufficientFunds:
            await q.answer("موجودی کافی نیست.", show_alert=True)
            return
        except UserNotFound:
            await q.answer("ابتدا /start را بزن.", show_alert=True)
            return
        await q.answer("بازی شروع شد!")
        board = "\n".join(("⚪️" * cfg["cols"]) if r == 0 else ("🔒" * cfg["cols"]) for r in range(cfg["rows"]))
        await safe_edit(q, poop_text(game).replace("\n\n", f"\n\n{board}\n\n", 1),
                        parse_mode="HTML", reply_markup=poop_keyboard(game))
        return

    if not await require_join(update, context):
        return

    if data == "poop_cashout":
        alert, out = None, None
        async with pool.acquire() as c, c.transaction():
            r = await c.fetchrow("SELECT * FROM poop_games WHERE user_id=$1 FOR UPDATE", uid)
            if not r:
                alert = "بازی فعالی پیدا نشد."
            else:
                game = poop_from_row(r)
                if game["chat_id"] != q.message.chat_id:
                    alert = "این بازی مربوط به گروه دیگری است."
                elif game["step"] <= 0:
                    alert = "هنوز مرحله‌ای رد نشده."
                else:
                    cfg = game_cfg(game)
                    prize, fee = apply_fee(game["amount"], int(game["amount"] * cfg["mults"][game["step"] - 1]))
                    await c.execute("DELETE FROM poop_games WHERE user_id=$1", uid)
                    bal = await adjust(c, uid, prize, "poop_cashout")
                    await log_game(c, uid, "poop", game["amount"], "cashout", prize, fee)
                    out = (game, cfg, prize, bal)
        if alert:
            await q.answer(alert, show_alert=True)
            return
        game, cfg, prize, bal = out
        await q.answer("برداشت شد")
        await safe_edit(q, f"💰 <b>برداشت موفق</b>\n\n🎮 مرحله {game['step']} از {cfg['rows']}\n"
                           f"💰 دریافتی: {format_mio(prize)} میو\n{fee_line(int(game['amount'] * cfg['mults'][game['step'] - 1]) - prize)}💰 موجودی: {format_mio(bal)} میو", parse_mode="HTML")
        return

    if data.startswith("poop_play:"):
        try:
            _, rs, cs = data.split(":")
            row, col = int(rs), int(cs)
        except ValueError:
            await q.answer()
            return
        alert, out = None, None
        async with pool.acquire() as c, c.transaction():
            r = await c.fetchrow("SELECT * FROM poop_games WHERE user_id=$1 FOR UPDATE", uid)
            if not r:
                alert = "بازی فعالی پیدا نشد."
            else:
                game = poop_from_row(r)
                cfg = game_cfg(game)
                if game["chat_id"] != q.message.chat_id:
                    alert = "این بازی مربوط به گروه دیگری است."
                elif row != game["step"] or not 0 <= col < cfg["cols"]:
                    alert = "این ردیف باز نیست."
                else:
                    for cc in range(cfg["cols"]):
                        game["history"][row][cc] = "💩" if cc in game["poops"][row] else "🟢"
                    if col in game["poops"][row]:
                        await c.execute("DELETE FROM poop_games WHERE user_id=$1", uid)
                        await log_game(c, uid, "poop", game["amount"], "loss", 0)
                        out = ("loss", game, cfg, 0, 0)
                    else:
                        game["step"] += 1
                        if game["step"] >= cfg["rows"]:
                            prize, fee = apply_fee(game["amount"], int(game["amount"] * cfg["mults"][-1]))
                            await c.execute("DELETE FROM poop_games WHERE user_id=$1", uid)
                            bal = await adjust(c, uid, prize, "poop_win")
                            await log_game(c, uid, "poop", game["amount"], "win", prize, fee)
                            out = ("win", game, cfg, prize, bal)
                        else:
                            await c.execute("UPDATE poop_games SET step=$2, history=$3 WHERE user_id=$1",
                                            uid, game["step"], json.dumps(game["history"]))
                            out = ("next", game, cfg, 0, 0)
        if alert:
            await q.answer(alert, show_alert=True)
            return
        kind, game, cfg, prize, bal = out
        if kind == "loss":
            await q.answer("💩 پوپ بود!")
            await safe_edit(q, "💥 <b>متأسفانه باختی 🤦‍♂️</b> 💩\n\nگوهی شدی 💩\n\n" + "".join(game["history"][row]) +
                            f"\n\n🪙 مبلغ شرط: {format_mio(game['amount'])} میو از دست رفت.", parse_mode="HTML")
        elif kind == "win":
            await q.answer("🏆 کامل شد!")
            await safe_edit(q, f"🏆 <b>{cfg['rows']} مرحله رو کامل کردی!</b>\n\n💰 جایزه نهایی: {format_mio(prize)} میو\n{fee_line(int(game['amount'] * cfg['mults'][-1]) - prize)}"
                               f"💰 موجودی: {format_mio(bal)} میو", parse_mode="HTML")
        else:
            await q.answer("🟢 امن! ردیف بعد باز شد.")
            parts = ["".join(x) for x in game["history"][:game["step"]]]
            parts.append("⚪️" * cfg["cols"])
            parts.extend("🔒" * cfg["cols"] for _ in range(cfg["rows"] - game["step"] - 1))
            await safe_edit(q, poop_text(game).replace("\n\n", "\n\n" + "\n".join(parts) + "\n\n", 1),
                            parse_mode="HTML", reply_markup=poop_keyboard(game))


# ============================================================
# Group quiz
# ============================================================
QUIZ_BANK = [
    ("پایتخت ایران چیست؟", "تهران"),
    ("بزرگ‌ترین سیاره منظومه شمسی چیست؟", "مشتری"),
    ("آب در فشار معمولی چند درجه می‌جوشد؟", "100"),
    ("تعداد قاره‌های زمین چند تاست؟", "7"),
    ("سریع‌ترین حیوان خشکی چیست؟", "یوزپلنگ"),
    ("ترکیب آبی و زرد چه رنگی می‌شود؟", "سبز"),
    ("بزرگ‌ترین اقیانوس جهان چیست؟", "اقیانوس آرام"),
    ("سیاره سرخ کدام است؟", "مریخ"),
    ("چند روز در یک هفته وجود دارد؟", "7"),
    ("نماد شیمیایی آب چیست؟", "H2O"),
    ("اولین ماه سال شمسی چیست؟", "فروردین"),
    ("آخرین ماه سال شمسی چیست؟", "اسفند"),
    ("نزدیک‌ترین سیاره به خورشید چیست؟", "عطارد"),
    ("چند ثانیه در یک دقیقه است؟", "60"),
    ("پایتخت فرانسه چیست؟", "پاریس"),
    ("بلندترین قله جهان چیست؟", "اورست"),
]
_quiz_cache: dict = {}


def normalize_answer(text) -> str:
    t = (text or "").strip().lower().translate(_DIGITS).replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", "", t)


def make_question():
    """60% fresh random arithmetic (never repeats), 40% from the general-knowledge bank."""
    if random.random() < 0.6:
        op = random.choice("+-×")
        if op == "×":
            a, b = random.randint(3, 15), random.randint(3, 15)
            ans = a * b
        else:
            a, b = random.randint(12, 99), random.randint(12, 99)
            if op == "-" and b > a:
                a, b = b, a
            ans = a + b if op == "+" else a - b
        return f"ریاضی: {a} {op} {b} چند می‌شود؟", str(ans)
    return random.choice(QUIZ_BANK)


async def load_quiz_cache() -> None:
    rows = await pool.fetch("SELECT * FROM active_questions")
    for r in rows:
        _quiz_cache[int(r["chat_id"])] = {"id": r["question_id"], "answer": normalize_answer(r["answer"]),
                                          "prize": int(r["prize"]), "created_at": float(r["created_at"])}


async def send_group_quizzes(bot) -> None:
    if setting("quiz_enabled", "1") != "1":
        return
    groups = await pool.fetch("SELECT chat_id FROM group_chats WHERE enabled")
    for g in groups:
        cid = int(g["chat_id"])
        if cid in _quiz_cache:
            continue
        q, a = make_question()
        qid, prize, now = os.urandom(6).hex(), s_int("quiz_prize", 50000), utc_ts()
        try:
            await bot.send_message(
                cid, f"🧠 <b>چالش رندوم میوبت</b>\n\n❓ {esc(q)}\n\n💰 جایزه نفر اول: {format_mio(prize)} میو\n"
                     f"⏱️ مهلت: {s_int('quiz_timeout_seconds', 120)} ثانیه\n✍️ جواب را همین‌جا بفرست.", parse_mode="HTML")
        except Forbidden:
            await pool.execute("UPDATE group_chats SET enabled=FALSE WHERE chat_id=$1", cid)
            _known_groups.pop(cid, None)
            continue
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            continue
        except TelegramError:
            continue
        _quiz_cache[cid] = {"id": qid, "answer": normalize_answer(a), "prize": prize, "created_at": now}
        await pool.execute(
            "INSERT INTO active_questions(chat_id,question_id,question,answer,prize,created_at) VALUES($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT(chat_id) DO UPDATE SET question_id=EXCLUDED.question_id, question=EXCLUDED.question, "
            "answer=EXCLUDED.answer, prize=EXCLUDED.prize, created_at=EXCLUDED.created_at", cid, qid, q, a, prize, now)
        await asyncio.sleep(0.05)


async def handle_group_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u = update.message, update.effective_user
    if not msg or not msg.text or not u or u.is_bot or not is_group(update):
        return
    chat_id = update.effective_chat.id
    qd = _quiz_cache.get(chat_id)  # memory only: no DB hit for normal chatter
    if not qd or utc_ts() - qd["created_at"] > s_int("quiz_timeout_seconds", 120):
        return
    if normalize_answer(msg.text) != qd["answer"]:
        return
    await ensure_user(u)
    prize = 0
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow("DELETE FROM active_questions WHERE chat_id=$1 AND question_id=$2 RETURNING prize",
                               chat_id, qd["id"])
        if not row:
            _quiz_cache.pop(chat_id, None)
            return
        prize = int(row["prize"])
        await adjust(c, u.id, prize, "group_quiz", qd["id"])
        await log_game(c, u.id, "quiz", 0, "win", prize)
    _quiz_cache.pop(chat_id, None)
    await msg.reply_text(f"🎉 پاسخ درست بود! +{format_mio(prize)} میو", reply_to_message_id=msg.message_id)


async def quiz_loop(bot) -> None:
    await asyncio.sleep(20)
    while True:
        try:
            interval = max(1, s_int("quiz_interval_minutes", 10)) * 60
            if time.time() - float(setting("quiz_last_ts", "0") or 0) >= interval:
                await set_setting("quiz_last_ts", str(time.time()))  # persisted: restarts don't spam quizzes
                await send_group_quizzes(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("quiz loop error")
        await asyncio.sleep(30)


# ============================================================
# Janitor: everything with a timeout lives in the DB, so a restart/deploy never strands money
# ============================================================
async def expire_bets(bot, table: str, game: str, timeout: int, label: str) -> None:
    cutoff = utc_ts() - timeout
    rows = await pool.fetch(f"SELECT user_id FROM {table} WHERE created_at < $1", cutoff)
    for r in rows:
        uid = int(r["user_id"])
        bet = None
        async with pool.acquire() as c, c.transaction():
            bet = await c.fetchrow(f"DELETE FROM {table} WHERE user_id=$1 AND created_at < $2 RETURNING *", uid, cutoff)
            if bet:
                refund = bet["amount"] - int(bet["amount"] * TIMEOUT_PENALTY)
                bal = await adjust(c, uid, refund, f"{game}_timeout_refund")
                await log_game(c, uid, game, bet["amount"], "timeout", refund)
        if bet:
            try:
                await bot.send_message(
                    bet["chat_id"], f"⚠️ زمان {label} تمام شد.\n💥 جریمه: {format_mio(bet['amount'] - refund)} میو\n"
                                    f"↩️ برگشت: {format_mio(refund)} میو\n💰 موجودی: {format_mio(bal)} میو")
            except TelegramError:
                pass


async def expire_poop(bot) -> None:
    cutoff = utc_ts() - POOP_TIMEOUT
    rows = await pool.fetch("SELECT user_id FROM poop_games WHERE created_at < $1", cutoff)
    for r in rows:
        uid, out = int(r["user_id"]), None
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow("DELETE FROM poop_games WHERE user_id=$1 AND created_at < $2 RETURNING *", uid, cutoff)
            if row:
                game = poop_from_row(row)
                cfg = game_cfg(game)
                if game["step"] == 0:
                    prize, fee = game["amount"], 0
                else:
                    prize, fee = apply_fee(game["amount"], int(game["amount"] * cfg["mults"][game["step"] - 1]))
                await adjust(c, uid, prize, "poop_auto_cashout")
                await log_game(c, uid, "poop", game["amount"], "cashout" if game["step"] else "refund", prize, fee)
                out = (game, prize)
        if out:
            game, prize = out
            try:
                await bot.edit_message_text(
                    f"⌛ بازی پوپ بدون فعالیت ماند و خودکار بسته شد.\n💰 دریافتی: {format_mio(prize)} میو",
                    chat_id=game["chat_id"], message_id=game["message_id"])
            except TelegramError:
                pass
            notify(uid, f"⌛ بازی پوپ شما خودکار بسته شد. +{format_mio(prize)} میو")


async def expire_quiz() -> None:
    timeout, now = s_int("quiz_timeout_seconds", 120), utc_ts()
    dead = [cid for cid, qd in _quiz_cache.items() if now - qd["created_at"] > timeout]
    for cid in dead:
        _quiz_cache.pop(cid, None)
    if dead:
        await pool.execute("DELETE FROM active_questions WHERE chat_id = ANY($1::bigint[])", dead)


async def janitor_loop(bot) -> None:
    tick = 0
    while True:
        try:
            await expire_bets(bot, "dice_bets", "dice", DICE_TIMEOUT, "تاس")
            await expire_bets(bot, "dart_bets", "dart", DART_TIMEOUT, "دارت")
            await expire_quiz()
            await expire_dynamite(bot)
            if tick % 6 == 0:
                await pool.execute("DELETE FROM football_games WHERE created_at < $1", utc_ts() - FOOTBALL_TIMEOUT)
            if tick % 30 == 0:
                await expire_poop(bot)
                await expire_auto_deposits()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("janitor error")
        tick += 1
        await asyncio.sleep(10)


async def notice_loop(bot) -> None:
    while True:
        chat_id, text = await _notice_q.get()
        try:
            await bot.send_message(chat_id, text)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            _notice_q.put_nowait((chat_id, text))
        except TelegramError:
            pass
        await asyncio.sleep(0.03)


# ============================================================
# Admin panel
# ============================================================
def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 داشبورد", callback_data="adm:stats"),
         InlineKeyboardButton("👥 کاربران", callback_data="adm:users:0")],
        [InlineKeyboardButton("🎮 آمار بازی", callback_data="adm:gamestats"),
         InlineKeyboardButton("⚙️ تنظیمات بازی", callback_data="adm:gamesettings")],
        [InlineKeyboardButton("🎁 تنظیمات پاداش", callback_data="adm:rewards"),
         InlineKeyboardButton("🏆 جوایز لیدربورد", callback_data="adm:leaderboard_rewards")],
        [InlineKeyboardButton("🧠 تنظیمات چالش", callback_data="adm:quizsettings")],
        [InlineKeyboardButton("🏦 تأیید خودکار واریز", callback_data="adm:auto")],
        [InlineKeyboardButton("📈 گزارش مالی", callback_data="adm:finance"),
         InlineKeyboardButton("💳 درخواست‌های مالی", callback_data="adm:pending_finance")],
        [InlineKeyboardButton("➕➖ مدیریت میو", callback_data="adm:balance"),
         InlineKeyboardButton("🔎 جستجوی کاربر", callback_data="adm:search")],
        [InlineKeyboardButton("📢 عضویت‌های اجباری", callback_data="adm:channels"),
         InlineKeyboardButton("📣 پیام همگانی", callback_data="adm:broadcast")],
    ])


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not is_private(update):
        await update.message.reply_text("🔐 پنل مدیریت فقط در پیوی ادمین باز می‌شود.")
        return
    context.user_data.clear()
    await update.message.reply_text("🛠 <b>پنل مدیریت میوبت</b>", parse_mode="HTML", reply_markup=admin_menu())


async def pay_leaderboard():
    """Pay the configured rewards to the current top ranks. Ranking is read BEFORE any payout."""
    count, rewards = get_leaderboard_rewards()
    paid = []
    if not rewards:
        return paid
    async with pool.acquire() as c, c.transaction():
        rows = await c.fetch(
            "SELECT user_id,username,first_name FROM users ORDER BY balance DESC, user_id ASC LIMIT $1 FOR UPDATE", count)
        for rank, r in enumerate(rows, 1):
            amt = rewards.get(rank)
            if amt:
                await adjust(c, r["user_id"], amt, "leaderboard_reward", str(rank))
                paid.append((rank, r, amt))
    for rank, r, amt in paid:
        notify(r["user_id"], f"🏆 جایزه رتبه {rank} جدول برترین‌ها: +{format_mio(amt)} میو")
    return paid


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("دسترسی ندارید", show_alert=True)
        return
    d = q.data or ""
    ud = context.user_data

    if d == "adm:home":
        ud.pop("admin_action", None)
        await q.answer()
        await safe_edit(q, "🛠 پنل مدیریت", reply_markup=admin_menu())
        return

    if d == "adm:stats":
        r = await pool.fetchrow(
            "SELECT (SELECT COUNT(*) FROM users) users, (SELECT COALESCE(SUM(balance),0) FROM users) bal, "
            "(SELECT COUNT(*) FROM game_stats) games, (SELECT COALESCE(SUM(amount),0) FROM game_stats) wagered, "
            "(SELECT COALESCE(SUM(payout),0) FROM game_stats) payout, (SELECT COALESCE(SUM(fee),0) FROM game_stats) fees")
        await q.answer()
        await safe_edit(q, f"📊 <b>داشبورد</b>\n\n👥 کاربران: {r['users']}\n💰 مجموع موجودی: {format_mio(r['bal'])}\n"
                           f"🎮 بازی ثبت‌شده: {r['games']}\n🎯 مجموع شرط‌ها: {format_mio(r['wagered'])}\n"
                           f"💸 مجموع پرداخت بازی: {format_mio(r['payout'])}\n🧾 مجموع کارمزد دریافتی: {format_mio(r['fees'])}", parse_mode="HTML", reply_markup=back_admin())
        return

    if d == "adm:gamestats":
        rows = await pool.fetch("SELECT game,COUNT(*) c,SUM(amount) wager,SUM(payout) pay FROM game_stats GROUP BY game")
        lines = ["🎮 آمار بازی‌ها\n"]
        for r in rows:
            lines.append(f"{GAME_LABELS.get(r['game'], r['game'])}: {r['c']} بازی | شرط {format_mio(r['wager'] or 0)} | پرداخت {format_mio(r['pay'] or 0)}")
        await q.answer()
        await safe_edit(q, "\n".join(lines) if len(lines) > 1 else "هنوز بازی‌ای ثبت نشده.", reply_markup=back_admin())
        return

    if d.startswith("adm:users:"):
        page = max(0, int(d.rsplit(":", 1)[1]))
        rows = await pool.fetch("SELECT * FROM users ORDER BY created_at DESC LIMIT $1 OFFSET $2", PAGE_SIZE, page * PAGE_SIZE)
        total = await pool.fetchval("SELECT COUNT(*) FROM users")
        lines = [f"👥 کاربران | صفحه {page + 1}\n"]
        for r in rows:
            lines.append(f"🆔 {r['user_id']} | @{r['username'] or '-'} | 💰 {format_mio(r['balance'])}")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adm:users:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adm:users:{page + 1}"))
        kb = [nav] if nav else []
        kb.append([InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")])
        await q.answer()
        await safe_edit(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if d == "adm:balance":
        ud["admin_action"] = "balance"
        await q.answer()
        await safe_edit(q, "➕➖ تغییر موجودی\n\nفرمت:\nآیدی مقدار\nمثال افزایش:\n123456789 50000\nمثال کاهش:\n123456789 -20000",
                        reply_markup=back_admin())
        return

    if d == "adm:search":
        ud["admin_action"] = "search"
        await q.answer()
        await safe_edit(q, "🔎 آیدی عددی یا username را بفرست.", reply_markup=back_admin())
        return

    if d == "adm:channels":
        invalidate_required()
        rows = await get_required_chats()
        lines = ["📢 <b>عضویت‌های اجباری</b>", "", "هر تعداد کانال یا گروه که لازم داری می‌توانی اضافه کنی."]
        kb = []
        for i, r in enumerate(rows, 1):
            icon = "📢" if r["chat_type"] == "channel" else "👥"
            lines.append(f"{i}. {icon} {esc(r['title'] or r['username'] or r['chat_id'])}")
            kb.append([InlineKeyboardButton(f"❌ حذف {i}", callback_data=f"adm:channel_del:{r['id']}")])
        if not rows:
            lines.append("— هیچ عضویت اجباری فعالی وجود ندارد.")
        kb.append([InlineKeyboardButton("➕ افزودن کانال/گروه", callback_data="adm:channel_add")])
        kb.append([InlineKeyboardButton("🗑 حذف همه", callback_data="adm:channel_clear")])
        kb.append([InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")])
        await q.answer()
        await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return

    if d == "adm:channel_add":
        ud["admin_action"] = "channel_add"
        await q.answer()
        await safe_edit(q, "➕ <b>افزودن کانال یا گروه اجباری</b>\n\nیکی از این‌ها را ارسال کن:\n• @username\n• لینک t.me\n"
                           "• آیدی عددی مثل -1001234567890\n\nربات باید داخل کانال/گروه ادمین باشد.",
                        parse_mode="HTML", reply_markup=back_admin())
        return

    if d.startswith("adm:channel_del:"):
        await pool.execute("DELETE FROM required_chats WHERE id=$1", int(d.rsplit(":", 1)[1]))
        invalidate_required()
        await q.answer("حذف شد ✅")
        await safe_edit(q, "✅ مورد حذف شد.", reply_markup=admin_menu())
        return

    if d == "adm:channel_clear":
        await pool.execute("DELETE FROM required_chats")
        invalidate_required()
        await q.answer("همه حذف شدند")
        await safe_edit(q, "🗑 همه عضویت‌های اجباری حذف شدند.", reply_markup=admin_menu())
        return

    if d == "adm:finance":
        dep = await pool.fetchval("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='deposit' AND status='approved'")
        wd = await pool.fetchval("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='withdraw' AND status='approved'")
        led = await pool.fetchval("SELECT COALESCE(SUM(delta),0) FROM ledger")
        await q.answer()
        await safe_edit(q, f"📈 <b>گزارش مالی</b>\n\n💳 واریز تأییدشده: {format_mio(dep)}\n💸 برداشت تأییدشده: {format_mio(wd)}\n"
                           f"📒 خالص تغییرات دفترکل: {format_mio(led)}", parse_mode="HTML", reply_markup=back_admin())
        return

    if d == "adm:gamesettings":
        ud["admin_action"] = "game_settings"
        txt = ("⚙️ <b>تنظیمات بازی</b>\n\n"
               f"🎲 ضریب تاس: ×{setting('dice_mult')}\n💩 ردیف‌های پوپ: {setting('poop_rows')}\n"
               f"💩 خانه‌های هر ردیف: {setting('poop_cols')}\n💩 پوپ‌ها در ردیف‌ها: {setting('poop_counts')}\n"
               f"📈 ضرایب پوپ: {setting('poop_mults')}\n🎯 دارت: سفید= {setting('dart_white_values')} | قرمز= {setting('dart_red_values')}\n"
               f"⚽ فوتبال: اعداد گل= {setting('football_goal_values')} | تعداد شوت= {setting('football_shots')}\n\n"
               "برای تغییر، یکی از فرمت‌ها را ارسال کن:\ndice 1.7\nrows 5\ncols 4\npoops 1,1,2,2,3\n"
               "mults 1.2,1.5,2,2.5,3\nfootball_goals 4,5\nfootball_shots 3\n\n"
               "ℹ️ بازی‌های پوپِ در حال انجام با تنظیمات قبلی تمام می‌شوند.")
        await q.answer()
        await safe_edit(q, txt, parse_mode="HTML", reply_markup=back_admin())
        return

    if d == "adm:rewards":
        ud["admin_action"] = "reward_settings"
        txt = (f"🎁 <b>تنظیمات پاداش و مالی</b>\n\n👥 دعوت‌کننده: {format_mio(s_int('referrer_bonus'))}\n"
               f"👤 دعوت‌شده: {format_mio(s_int('referred_bonus'))}\n✅ بازی لازم برای فعال شدن پاداش دعوت: {s_int('referral_min_games')}\n"
               f"🎯 ضریب دارت: ×{setting('dart_multiplier')} | سفید: {setting('dart_white_values')}\n"
               f"🧠 جایزه چالش: {format_mio(s_int('quiz_prize'))}\n🎁 جایزه روزانه: {format_mio(s_int('daily_bonus'))}\n"
               f"💸 حداقل برداشت: {format_mio(s_int('min_withdraw'))}\n🧾 کارمزد روی سودِ برد: {setting('win_fee_percent')}٪\n🏦 حساب واریز: <code>{esc(payment_card() or '—')}</code>\n\n"
               "فرمت‌ها:\nreferrer 100000\nreferred 50000\nreferral_games 3\ndart_mult 2\ndart_white 1,3,5\n"
               "quiz_prize 50000\ndaily_bonus 5000\nmin_withdraw 100000\nfee 5\ncard حساب-بانک-میویی")
        await q.answer()
        await safe_edit(q, txt, parse_mode="HTML", reply_markup=back_admin())
        return

    if d == "adm:leaderboard_rewards":
        count, rewards = get_leaderboard_rewards()
        lines = ["🏆 <b>جوایز لیدربورد</b>", "", f"🏆 تعداد رتبه‌های جایزه‌دار: {count}", ""]
        for rank in range(1, count + 1):
            lines.append(f"🏆 رتبه {rank}: {format_mio(rewards[rank])} میو" if rank in rewards else f"▫️ رتبه {rank}: بدون جایزه")
        lines += ["", "فرمت:", "count 3", "rank 1 1000000", "rank 2 500000", "rank 3 250000"]
        ud["admin_action"] = "leaderboard_rewards"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💸 پرداخت جوایز همین حالا", callback_data="adm:lb_pay")],
                                   [InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")]])
        await q.answer()
        await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=kb)
        return

    if d == "adm:lb_pay":
        count, rewards = get_leaderboard_rewards()
        if not rewards:
            await q.answer("هنوز جایزه‌ای تنظیم نشده.", show_alert=True)
            return
        rows = await pool.fetch("SELECT username,first_name,balance FROM users ORDER BY balance DESC, user_id ASC LIMIT $1", count)
        lines = ["💸 <b>تأیید پرداخت جوایز</b>", "", "با وضعیت فعلی جدول، این پرداخت‌ها انجام می‌شود:", ""]
        for rank, r in enumerate(rows, 1):
            if rank in rewards:
                lines.append(f"{rank}. {esc('@' + r['username'] if r['username'] else r['first_name'])} ← {format_mio(rewards[rank])} میو")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، پرداخت کن", callback_data="adm:lb_pay_yes")],
                                   [InlineKeyboardButton("⬅️ انصراف", callback_data="adm:home")]])
        await q.answer()
        await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=kb)
        return

    if d == "adm:lb_pay_yes":
        paid = await pay_leaderboard()
        await q.answer("پرداخت شد ✅" if paid else "چیزی برای پرداخت نبود.")
        await safe_edit(q, f"✅ جوایز {len(paid)} نفر پرداخت شد.", reply_markup=admin_menu())
        return

    if d == "adm:quizsettings":
        ud["admin_action"] = "quiz_settings"
        txt = (f"🧠 <b>تنظیمات چالش گروه</b>\n\n🔘 وضعیت: {'فعال' if setting('quiz_enabled') == '1' else 'خاموش'}\n"
               f"⏱️ فاصله: {setting('quiz_interval_minutes')} دقیقه\n💰 جایزه: {format_mio(s_int('quiz_prize'))}\n"
               f"⌛ مهلت پاسخ: {setting('quiz_timeout_seconds')} ثانیه\n\nفرمت: enable 1 | interval 10 | prize 50000 | timeout 120")
        await q.answer()
        await safe_edit(q, txt, parse_mode="HTML", reply_markup=back_admin())
        return

    if d == "adm:pending_finance":
        await q.answer()
        await render_pending(q)
        return

    if d == "adm:broadcast":
        ud["admin_action"] = "broadcast"
        await q.answer()
        await safe_edit(q, "📣 <b>پیام همگانی</b>\n\nمتن پیام را بفرست.\nپیام برای همه کاربران ثبت‌شده ارسال می‌شود.",
                        parse_mode="HTML", reply_markup=back_admin())
        return

    if d.startswith("adm:auto") and await auto_admin_callbacks(q, d, ud):
        return

    await q.answer()


_bg_tasks: set = set()


async def do_broadcast(bot, text: str) -> None:
    ids = [r["user_id"] for r in await pool.fetch("SELECT user_id FROM users")]
    sent = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                await bot.send_message(uid, text)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramError:
                break
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(ADMIN_ID, f"📣 ارسال همگانی تمام شد.\n✅ ارسال موفق: {sent}\n👥 کل: {len(ids)}")
    except TelegramError:
        pass


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != ADMIN_ID or not is_private(update) or not update.message or not update.message.text:
        return
    action = context.user_data.get("admin_action")
    if not action:
        return
    text = update.message.text.strip()
    reply = update.message.reply_text

    if action == "balance":
        m = re.fullmatch(r"(\d+)\s+([+-]?\d+)", text.translate(_DIGITS))
        if not m:
            await reply("❌ فرمت: آیدی مقدار")
            return
        uid, delta = int(m.group(1)), int(m.group(2))
        if not await get_user(uid):
            await reply("❌ کاربر پیدا نشد.")
            return
        try:
            new = await atomic_balance(uid, delta, "admin_balance_change")
        except InsufficientFunds:
            await reply("❌ موجودی کاربر کمتر از مقدار کسر است.")
            return
        context.user_data.clear()
        await reply(f"✅ انجام شد.\n🆔 {uid}\n💰 تغییر: {delta:+,}\n💰 موجودی: {format_mio(new)}", reply_markup=admin_menu())
        notify(uid, f"🛠 تغییر موجودی توسط ادمین\n💰 تغییر: {delta:+,} میو\n💰 موجودی: {format_mio(new)} میو")

    elif action == "search":
        key = text.lstrip("@")
        r = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1 OR lower(username)=lower($2) LIMIT 1",
                                int(key) if key.isdigit() else -1, key)
        context.user_data.clear()
        if not r:
            await reply("❌ پیدا نشد.", reply_markup=admin_menu())
            return
        await reply(f"👤 پروفایل\n\n🆔 {r['user_id']}\n🔗 @{r['username'] or '-'}\n👤 {r['first_name']}\n"
                    f"💰 {format_mio(r['balance'])} میو\n🎮 بازی: {r['games_count']}\n🏆 برد: {r['wins_count']}",
                    reply_markup=admin_menu())

    elif action == "channel_add":
        raw = text
        if raw.lower() == "لغو":
            context.user_data.clear()
            await reply("❌ لغو شد.", reply_markup=admin_menu())
            return
        chat_id, username, url = raw, "", ""
        if raw.startswith(("https://t.me/", "http://t.me/")):
            username = "@" + raw.rstrip("/").split("/")[-1].split("?")[0]
            chat_id, url = username, f"https://t.me/{username[1:]}"
        elif raw.startswith("@"):
            username, chat_id, url = raw, raw, f"https://t.me/{raw[1:]}"
        try:
            chat = await context.bot.get_chat(chat_id)
        except TelegramError:
            await reply("❌ این کانال/گروه پیدا نشد یا ربات به آن دسترسی ندارد.\nآیدی، @username یا لینک را بررسی کن.")
            return
        title = chat.title or getattr(chat, "first_name", "") or chat_id
        ctype = "channel" if chat.type == "channel" else "group"
        await pool.execute(
            "INSERT INTO required_chats(chat_id,title,username,url,chat_type) VALUES($1,$2,$3,$4,$5) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, username=EXCLUDED.username, "
            "url=EXCLUDED.url, chat_type=EXCLUDED.chat_type, enabled=TRUE",
            str(chat.id), title, ("@" + chat.username) if getattr(chat, "username", None) else username, url, ctype)
        invalidate_required()
        context.user_data.clear()
        await reply(f"✅ {'کانال' if ctype == 'channel' else 'گروه'} اجباری اضافه شد.\n📌 {title}\n🆔 {chat.id}\n"
                    "⚠️ برای بررسی عضویت، ربات باید در آن ادمین باشد.", reply_markup=admin_menu())

    elif action == "game_settings":
        m = re.fullmatch(r"(dice|rows|cols|poops|mults|football_goals|football_shots)\s+(.+)", text, re.I)
        if not m:
            await reply("❌ فرمت نامعتبر.")
            return
        key, val = m.group(1).lower(), m.group(2).strip()
        try:
            if key == "dice":
                x = float(val)
                if not 1.0 <= x <= 10.0:
                    raise ValueError
                await set_setting("dice_mult", str(x))
            elif key == "rows":
                x = int(val)
                if not 1 <= x <= 20:
                    raise ValueError
                await set_setting("poop_rows", str(x))
            elif key == "cols":
                x = int(val)
                if not 2 <= x <= 8:
                    raise ValueError
                await set_setting("poop_cols", str(x))
            elif key == "poops":
                xs = [int(v.strip()) for v in val.split(",")]
                if len(xs) != s_int("poop_rows", POOP_ROWS) or any(x < 0 or x >= s_int("poop_cols", POOP_COLS) for x in xs):
                    raise ValueError
                await set_setting("poop_counts", ",".join(map(str, xs)))
            elif key == "mults":
                xs = [float(v.strip()) for v in val.split(",")]
                if len(xs) != s_int("poop_rows", POOP_ROWS) or any(x <= 0 for x in xs):
                    raise ValueError
                await set_setting("poop_mults", ",".join(map(str, xs)))
            elif key == "football_goals":
                xs = sorted({int(v.strip()) for v in val.split(",") if v.strip()})
                if not xs or any(x < 1 or x > 5 for x in xs):
                    raise ValueError
                await set_setting("football_goal_values", ",".join(map(str, xs)))
            else:
                x = int(val)
                if not 1 <= x <= 10:
                    raise ValueError
                await set_setting("football_shots", str(x))
        except ValueError:
            await reply("❌ مقدار نامعتبر یا خارج از محدوده.")
            return
        context.user_data.clear()
        await reply("✅ تنظیم ذخیره شد.", reply_markup=admin_menu())

    elif action == "reward_settings":
        m = re.fullmatch(r"(referrer|referred|referral_games|dart_mult|dart_white|quiz_prize|daily_bonus|min_withdraw|fee|card)\s+(.+)",
                         text, re.I)
        if not m:
            await reply("❌ فرمت نامعتبر.")
            return
        k, v = m.group(1).lower(), m.group(2).strip()
        try:
            if k == "dart_mult":
                x = float(v)
                if not 0 < x <= 20:
                    raise ValueError
                await set_setting("dart_multiplier", str(x))
            elif k == "dart_white":
                vals = sorted({int(z.strip()) for z in v.split(",") if z.strip()})
                if not vals or any(z < 1 or z > 6 for z in vals) or len(vals) >= 6:
                    raise ValueError
                await set_setting("dart_white_values", ",".join(map(str, vals)))
                # red is always the complement, so every dart value maps to exactly one colour
                await set_setting("dart_red_values", ",".join(str(z) for z in range(1, 7) if z not in vals))
            elif k == "fee":
                x = float(v)
                if not 0 <= x <= 50:
                    raise ValueError
                await set_setting("win_fee_percent", str(x))
            elif k == "referral_games":
                x = int(v)
                if not 0 <= x <= 100:
                    raise ValueError
                await set_setting("referral_min_games", str(x))
            elif k == "card":
                acct = re.sub(r"[\s\-]", "", v.translate(_DIGITS))
                if not re.fullmatch(r"\d{12}", acct):
                    raise ValueError
                await set_setting("payment_card", acct)
            elif k == "min_withdraw":
                x = parse_amount(v)
                if x < 0:
                    raise ValueError
                await set_setting("min_withdraw", str(x))
            else:
                x = parse_amount(v)
                if x <= 0:
                    raise ValueError
                key = {"referrer": "referrer_bonus", "referred": "referred_bonus",
                       "quiz_prize": "quiz_prize", "daily_bonus": "daily_bonus"}[k]
                await set_setting(key, str(x))
        except ValueError:
            await reply("❌ مقدار نامعتبر.")
            return
        context.user_data.clear()
        await reply("✅ تنظیم ذخیره شد.", reply_markup=admin_menu())

    elif action == "leaderboard_rewards":
        m = re.fullmatch(r"(count|rank)\s+(.+)", text, re.I)
        if not m:
            await reply("❌ فرمت نامعتبر. مثال: count 3 یا rank 1 1000000")
            return
        try:
            cmd, args = m.group(1).lower(), m.group(2).strip()
            if cmd == "count":
                count = int(args)
                if not 0 <= count <= 100:
                    raise ValueError
                _, cur = get_leaderboard_rewards()
                raw = {str(k): v for k, v in cur.items() if k <= count}
                await set_setting("leaderboard_reward_count", str(count))
                await set_setting("leaderboard_rewards", json.dumps(raw))
            else:
                parts = args.split()
                if len(parts) != 2:
                    raise ValueError
                rank, amount = int(parts[0]), parse_amount(parts[1])
                count, cur = get_leaderboard_rewards()
                if count < 1 or not 1 <= rank <= count or amount <= 0:
                    raise ValueError
                cur[rank] = amount
                await set_setting("leaderboard_rewards", json.dumps({str(k): v for k, v in cur.items()}))
        except ValueError:
            await reply("❌ مقدار نامعتبر یا خارج از محدوده.")
            return
        context.user_data.clear()
        await reply("✅ تنظیم جوایز لیدربورد ذخیره شد.", reply_markup=admin_menu())

    elif action == "quiz_settings":
        m = re.fullmatch(r"(enable|interval|prize|timeout)\s+(.+)", text, re.I)
        if not m:
            await reply("❌ فرمت نامعتبر.")
            return
        k, v = m.group(1).lower(), m.group(2).strip()
        try:
            if k == "enable":
                x, key, ok = int(v), "quiz_enabled", int(v) in (0, 1)
            elif k == "interval":
                x, key, ok = int(v), "quiz_interval_minutes", 1 <= int(v) <= 1440
            elif k == "prize":
                x, key, ok = parse_amount(v), "quiz_prize", parse_amount(v) > 0
            else:
                x, key, ok = int(v), "quiz_timeout_seconds", 15 <= int(v) <= 3600
            if not ok:
                raise ValueError
            await set_setting(key, str(x))
        except ValueError:
            await reply("❌ مقدار نامعتبر یا خارج از محدوده.")
            return
        context.user_data.clear()
        await reply("✅ تنظیم چالش ذخیره شد.", reply_markup=admin_menu())

    elif action in ("ub_phone", "ub_code", "ub_password", "auto_settings", "auto_test"):
        await auto_admin_text(update, context, action, text)

    elif action == "broadcast":
        context.user_data.clear()
        await reply("📣 ارسال همگانی در پس‌زمینه شروع شد؛ بعد از پایان گزارش می‌فرستم. ربات در این مدت کار می‌کند.")
        task = asyncio.create_task(do_broadcast(context.bot, text))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


# ============================================================
# Error handler
# ============================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    global BUTTON_COLORS
    if isinstance(err, BadRequest) and BUTTON_COLORS and "style" in str(err).lower():
        BUTTON_COLORS = False  # Telegram rejected coloured buttons: fall back to plain glass buttons
        logger.error("Button styles rejected by Telegram, disabled: %s", err)
        try:
            await context.bot.send_message(ADMIN_ID, "⚠️ تلگرام دکمه‌های رنگی رو قبول نکرد؛ خودکار خاموش شدن. دوباره /start بزن.")
        except Exception:
            pass
        return
    if isinstance(err, Conflict):
        logger.error("Polling conflict (another instance running, e.g. during a deploy): %s", err)
        return
    if isinstance(err, RetryAfter):
        logger.warning("RetryAfter: %s", err)
        return
    if isinstance(err, NetworkError):
        logger.warning("Network error: %s", err)
        return
    logger.error("Unhandled bot error: %r", err, exc_info=err)
    now = utc_ts()
    if now - context.application.bot_data.get("last_error_notice", 0.0) < 600:
        return
    context.application.bot_data["last_error_notice"] = now
    try:
        await context.bot.send_message(ADMIN_ID, "⚠️ خطای داخلی ربات ثبت شد. جزئیات در لاگ‌های Render موجود است.")
    except Exception:
        pass


# ============================================================
# Telegram flood-limit protection + ordered concurrency
# ============================================================
class RobustLimiter(BaseRateLimiter):
    """Wraps PTB's AIORateLimiter (proactive throttling) and adds our own safety net:
    a RetryAfter / network error never reaches the handler. The call is simply delayed a few
    seconds and repeated, so the action we wanted still happens instead of being dropped."""

    def __init__(self, inner=None, max_attempts: int = 6):
        self._inner = inner
        self._max = max_attempts

    async def initialize(self) -> None:
        if self._inner is not None:
            await self._inner.initialize()

    async def shutdown(self) -> None:
        if self._inner is not None:
            await self._inner.shutdown()

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        attempt = 0
        while True:
            try:
                if self._inner is not None:
                    return await self._inner.process_request(
                        callback=callback, args=args, kwargs=kwargs,
                        endpoint=endpoint, data=data, rate_limit_args=rate_limit_args)
                return await callback(*args, **kwargs)
            except RetryAfter as e:
                attempt += 1
                if attempt > self._max:
                    raise
                delay = min(float(getattr(e, "retry_after", 1)), 60.0) + 0.5
                logger.warning("Telegram flood limit on %s - waiting %.1fs then retrying (%d/%d)",
                               endpoint, delay, attempt, self._max)
                await asyncio.sleep(delay)
            except NetworkError as e:  # includes TimedOut
                attempt += 1
                if attempt > 3:
                    raise
                logger.warning("Network hiccup on %s (%s) - retry %d/3", endpoint, e, attempt)
                await asyncio.sleep(min(2 ** attempt, 8))


def make_rate_limiter() -> RobustLimiter:
    inner = None
    try:
        from telegram.ext import AIORateLimiter
        inner = AIORateLimiter(overall_max_rate=30, overall_time_period=1,
                               group_max_rate=20, group_time_period=60, max_retries=3)
    except Exception:
        logger.warning("aiolimiter missing (install python-telegram-bot[rate-limiter]) - "
                       "proactive throttling off, retry safety-net still active")
    return RobustLimiter(inner)


class PerUserProcessor(BaseUpdateProcessor):
    """Different users are handled in parallel (a flood-wait for one chat never freezes the rest),
    while updates of the SAME user stay strictly in order."""

    def __init__(self, max_concurrent_updates: int = 32):
        super().__init__(max_concurrent_updates)
        self._locks: dict = {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_process_update(self, update, coroutine) -> None:
        uid = update.effective_user.id if getattr(update, "effective_user", None) else 0
        entry = self._locks.setdefault(uid, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                await coroutine
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(uid, None)


# ============================================================
# Auto-deposit: a Telethon user-session that watches the Meowie-bank bot
# ============================================================
def fee_line(fee: int) -> str:
    return f"🧾 کارمزد: {format_mio(fee)} میو\n" if fee > 0 else ""


def _to_int_amount(raw: str) -> int:
    t = str(raw).translate(_DIGITS).replace(",", "").replace("٬", "").replace("،", "").strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return int(float(t))
    except ValueError:
        return 0


def parse_bank_text(text: str) -> dict:
    """Run the admin-configured regexes on a bank-bot message."""
    norm = (text or "").translate(_DIGITS)
    out = {"ok_filter": False, "amount": 0, "sender": "", "error": ""}
    try:
        flt = setting("bank_filter_re", "")
        out["ok_filter"] = (not flt) or bool(re.search(flt, norm))
        m = re.search(setting("bank_amount_re", ""), norm)
        if m:
            out["amount"] = _to_int_amount(m.group(1))
        m = re.search(setting("bank_sender_re", ""), norm)
        if m:
            out["sender"] = re.sub(r"\D", "", m.group(1))
    except re.error as e:
        out["error"] = f"regex: {e}"
    except IndexError:
        out["error"] = "الگو باید یک گروه پرانتزی ( ) داشته باشد"
    return out


class Userbot:
    def __init__(self):
        self.client = None
        self.connected = False
        self.phone = ""
        self.login: Optional[dict] = None

    @staticmethod
    def available() -> bool:
        return bool(TELETHON_OK and TG_API_ID.isdigit() and TG_API_HASH)

    def _new_client(self, session: str = ""):
        return TelegramClient(StringSession(session), int(TG_API_ID), TG_API_HASH, flood_sleep_threshold=120)

    def _attach(self, client, phone: str) -> None:
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client, self.phone, self.connected = client, phone, True

    async def start_from_db(self) -> bool:
        if not self.available():
            return False
        row = await pool.fetchrow("SELECT session, phone FROM userbot_session WHERE id=1")
        if not row:
            return False
        client = self._new_client(row["session"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await pool.execute("DELETE FROM userbot_session WHERE id=1")
                notify(ADMIN_ID, "⚠️ نشست اکانت تأیید خودکار منقضی شده؛ از پنل دوباره وصلش کن.")
                return False
        except Exception:
            logger.exception("userbot start failed")
            return False
        self._attach(client, row["phone"])
        logger.info("userbot connected (%s)", row["phone"])
        return True

    async def _on_message(self, event) -> None:
        try:
            if not event.is_private:
                return
            bank = setting("bank_bot", "").strip().lstrip("@").lower()
            if not bank:
                return
            sender = await event.get_sender()
            # Only the real bank BOT account is trusted - messages typed by people are never parsed.
            if not sender or not getattr(sender, "bot", False):
                return
            if not (bank == (getattr(sender, "username", "") or "").lower()
                    or (bank.isdigit() and int(bank) == sender.id)):
                return
            await process_bank_message(event.message.id, event.raw_text or "")
        except Exception:
            logger.exception("bank message handling failed")

    # ---- interactive login (phone -> code via inline keypad -> optional 2FA password) ----
    async def begin_login(self, phone: str) -> None:
        await self.cancel_login()
        client = self._new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise
        self.login = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "code": ""}

    async def cancel_login(self) -> None:
        if self.login:
            try:
                await self.login["client"].disconnect()
            except Exception:
                pass
            self.login = None

    async def submit_code(self) -> str:
        lg = self.login
        if not lg:
            return "none"
        try:
            await lg["client"].sign_in(phone=lg["phone"], code=lg["code"], phone_code_hash=lg["hash"])
        except SessionPasswordNeededError:
            return "password"
        except PhoneCodeInvalidError:
            lg["code"] = ""
            return "invalid"
        except PhoneCodeExpiredError:
            await self.cancel_login()
            return "expired"
        await self._finish_login()
        return "ok"

    async def submit_password(self, password: str) -> str:
        lg = self.login
        if not lg:
            return "none"
        try:
            await lg["client"].sign_in(password=password)
        except PasswordHashInvalidError:
            return "invalid"
        await self._finish_login()
        return "ok"

    async def _finish_login(self) -> None:
        lg = self.login
        client, phone = lg["client"], lg["phone"]
        session = client.session.save()
        await pool.execute(
            "INSERT INTO userbot_session(id,session,phone) VALUES(1,$1,$2) "
            "ON CONFLICT(id) DO UPDATE SET session=EXCLUDED.session, phone=EXCLUDED.phone, updated_at=now()",
            session, phone)
        self.login = None
        if self.client is not None and self.client is not client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self._attach(client, phone)

    async def stop(self, logout: bool = False) -> None:
        await self.cancel_login()
        if self.client is not None:
            try:
                if logout:
                    await self.client.log_out()
                else:
                    await self.client.disconnect()
            except Exception:
                logger.exception("userbot stop failed")
        self.client, self.connected, self.phone = None, False, ""
        if logout:
            await pool.execute("DELETE FROM userbot_session WHERE id=1")


userbot = Userbot()


def auto_ready() -> bool:
    return setting("auto_deposit_enabled", "0") == "1" and userbot.connected


async def credit_auto_deposit(sender: str, amount: int, row_id: int):
    """Match a bank transfer to a pending deposit of the same sender account and credit the amount
    that was ACTUALLY received. All in one transaction."""
    since = datetime.now(timezone.utc) - timedelta(minutes=max(1, s_int("deposit_window_minutes", 60)))
    async with pool.acquire() as c, c.transaction():
        tx = await c.fetchrow(
            "SELECT * FROM transactions WHERE kind='deposit' AND status='pending' AND card=$1 AND created_at > $2 "
            "ORDER BY abs(amount-$3), created_at LIMIT 1 FOR UPDATE", sender, since, amount)
        if not tx:
            return None
        await c.execute("UPDATE transactions SET amount=$2, status='approved', note=$3, updated_at=now() WHERE tx_id=$1",
                        tx["tx_id"], amount, f"auto bank_msg={row_id}")
        await adjust(c, tx["user_id"], amount, "deposit_auto", tx["tx_id"])
        await c.execute("INSERT INTO bank_accounts(account,user_id) VALUES($1,$2) ON CONFLICT(account) DO NOTHING",
                        sender, tx["user_id"])
        await c.execute("UPDATE bank_messages SET status='credited', amount=$2, sender_account=$3, tx_id=$4 WHERE id=$1",
                        row_id, amount, sender, tx["tx_id"])
        return tx


async def process_bank_message(msg_id: int, text: str) -> None:
    # UNIQUE(msg_id): a replayed/duplicated update can never credit twice.
    row_id = await pool.fetchval(
        "INSERT INTO bank_messages(msg_id, body) VALUES($1,$2) ON CONFLICT(msg_id) DO NOTHING RETURNING id",
        msg_id, text[:2000])
    if row_id is None:
        return
    p = parse_bank_text(text)

    async def mark(status: str) -> None:
        await pool.execute("UPDATE bank_messages SET status=$2, amount=$3, sender_account=$4 WHERE id=$1",
                           row_id, status, p["amount"] or None, p["sender"] or None)

    if p["error"]:
        await mark("error")
        notify(ADMIN_ID, f"⚠️ خطا در الگوهای بانک: {p['error']}")
        return
    if not p["ok_filter"]:
        await mark("ignored")
        return
    if p["amount"] <= 0 or len(p["sender"]) != 12:
        await mark("unparsed")
        notify(ADMIN_ID, "⚠️ پیامی از بانک میویی آمد ولی مبلغ/حساب فرستنده‌اش تشخیص داده نشد. "
                         "از «تست پیام» برای اصلاح الگوها استفاده کن.\n\n" + text[:300])
        return
    if setting("auto_deposit_enabled", "0") != "1":
        await mark("logged")
        return
    tx = await credit_auto_deposit(p["sender"], p["amount"], row_id)
    if tx is None:
        await mark("unmatched")
        notify(ADMIN_ID, f"💸 واریز بدون درخواست ثبت‌شده!\nمبلغ: {format_mio(p['amount'])}\nاز حساب: {p['sender']}\n"
                         "اگر مال یک کاربر است، از «مدیریت میو» دستی شارژش کن.")
        return
    note = ""
    if int(tx["amount"]) != p["amount"]:
        note = f"\n(مبلغ واقعی دریافت‌شده {format_mio(p['amount'])} بود؛ همان شارژ شد.)"
    notify(tx["user_id"], f"✅ واریز تأیید شد! +{format_mio(p['amount'])} میو به حسابت اضافه شد.{note}")
    notify(ADMIN_ID, f"✅ واریز خودکار: {format_mio(p['amount'])} میو | کاربر {tx['user_id']} | حساب {p['sender']}")


async def expire_auto_deposits() -> None:
    since = datetime.now(timezone.utc) - timedelta(minutes=max(1, s_int("deposit_window_minutes", 60)))
    rows = await pool.fetch(
        "UPDATE transactions SET status='rejected', note='expired', updated_at=now() "
        "WHERE kind='deposit' AND status='pending' AND receipt='auto' AND created_at < $1 RETURNING user_id, amount", since)
    for r in rows:
        notify(r["user_id"], f"⌛ درخواست واریز {format_mio(r['amount'])} میو منقضی شد. "
                             "اگر واریز کرده‌ای به ادمین خبر بده.")


# ---------------- admin panel pieces ----------------
def back_to_auto() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data="adm:auto")]])


def auto_panel():
    on = setting("auto_deposit_enabled", "0") == "1"
    bank = setting("bank_bot", "").strip().lstrip("@")
    bank_txt = "تنظیم نشده" if not bank else (bank if bank.isdigit() else "@" + bank)
    lines = [
        "🏦 <b>تأیید خودکار واریز</b>", "",
        f"👤 اکانت: {'✅ متصل ' + esc(userbot.phone) if userbot.connected else '⛔ متصل نیست'}",
        f"🤖 بات بانک میویی: {esc(bank_txt)}",
        f"🔘 تأیید خودکار: {'🟢 روشن' if on else '🔴 خاموش'}",
        f"⏱ مهلت واریز: {s_int('deposit_window_minutes', 60)} دقیقه",
    ]
    if not userbot.available():
        lines += ["", "⚠️ اتصال اکانت فعلاً ممکن نیست: متغیرهای TG_API_ID و TG_API_HASH (از my.telegram.org) "
                      "را در Render بذار و مطمئن شو telethon نصب است."]
    lines += ["", "ترتیب کار: ۱) اتصال اکانت ۲) تنظیم بات بانک و الگوها ۳) تست با یک پیام واقعی ۴) روشن کردن",
              "🔒 نشست اکانت در دیتابیس ذخیره می‌شود؛ بهتر است اکانتِ اختصاصیِ دریافت‌کننده باشد."]
    kb = [
        [InlineKeyboardButton("🔌 قطع و خروج از اکانت" if userbot.connected else "🔗 اتصال اکانت",
                              callback_data="adm:auto_disconnect" if userbot.connected else "adm:auto_connect")],
        [InlineKeyboardButton("🔴 خاموش کردن تأیید خودکار" if on else "🟢 روشن کردن تأیید خودکار",
                              callback_data="adm:auto_toggle")],
        [InlineKeyboardButton("⚙️ بات و الگوها", callback_data="adm:auto_settings"),
         InlineKeyboardButton("🧪 تست پیام", callback_data="adm:auto_test")],
        [InlineKeyboardButton("📨 پیام‌های اخیر بانک", callback_data="adm:auto_log")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="adm:home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def code_keyboard() -> InlineKeyboardMarkup:
    def b(label, data):
        return InlineKeyboardButton(label, callback_data=data)
    return InlineKeyboardMarkup([
        [b("1", "ub:d:1"), b("2", "ub:d:2"), b("3", "ub:d:3")],
        [b("4", "ub:d:4"), b("5", "ub:d:5"), b("6", "ub:d:6")],
        [b("7", "ub:d:7"), b("8", "ub:d:8"), b("9", "ub:d:9")],
        [b("⌫", "ub:del"), b("0", "ub:d:0"), b("✅ تأیید", "ub:ok")],
        [b("❌ لغو", "ub:cancel")],
    ])


def code_prompt(code: str) -> str:
    shown = " ".join(code) if code else "—"
    return ("🔐 <b>کد ورود تلگرام</b>\n\nکدی که تلگرام به اکانت فرستاده را با دکمه‌ها وارد کن، بعد «تأیید».\n"
            "⚠️ کد را داخل چت تایپ نکن؛ تلگرام کدِ ارسال‌شده در چت را باطل می‌کند.\n\n"
            f"<code>{shown}</code>")


async def ub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("دسترسی ندارید", show_alert=True)
        return
    d, lg = q.data or "", userbot.login
    if not lg:
        await q.answer("نشست ورود منقضی شده؛ دوباره اتصال را شروع کن.", show_alert=True)
        return
    if d == "ub:cancel":
        await userbot.cancel_login()
        context.user_data.pop("admin_action", None)
        await q.answer("لغو شد")
        await safe_edit(q, "❌ اتصال اکانت لغو شد.", reply_markup=back_to_auto())
        return
    if d.startswith("ub:d:"):
        if len(lg["code"]) < 8:
            lg["code"] += d.split(":")[2][:1]
    elif d == "ub:del":
        lg["code"] = lg["code"][:-1]
    elif d == "ub:ok":
        if len(lg["code"]) < 4:
            await q.answer("کد کامل نیست.", show_alert=True)
            return
        try:
            res = await userbot.submit_code()
        except FloodWaitError as e:
            await q.answer(f"محدودیت تلگرام؛ {e.seconds} ثانیه بعد دوباره امتحان کن.", show_alert=True)
            return
        except Exception:
            logger.exception("userbot code submit failed")
            await q.answer("خطا در ورود؛ دوباره تلاش کن.", show_alert=True)
            return
        if res == "ok":
            context.user_data.pop("admin_action", None)
            await q.answer("متصل شد ✅")
            text, kb = auto_panel()
            await safe_edit(q, "✅ اکانت با موفقیت متصل شد.\n\n" + text, parse_mode="HTML", reply_markup=kb)
        elif res == "password":
            context.user_data["admin_action"] = "ub_password"
            await q.answer()
            await safe_edit(q, "🔐 این اکانت رمز تأیید دو مرحله‌ای دارد.\nرمز را همین‌جا بفرست؛ پیامت بعد از خواندن پاک می‌شود.",
                            reply_markup=back_to_auto())
        elif res == "invalid":
            await q.answer("کد اشتباه است.", show_alert=True)
            await safe_edit(q, "❌ کد اشتباه بود.\n\n" + code_prompt(""), parse_mode="HTML", reply_markup=code_keyboard())
        else:
            context.user_data.pop("admin_action", None)
            await q.answer("کد منقضی شد.", show_alert=True)
            await safe_edit(q, "⌛ کد منقضی شد؛ اتصال را دوباره شروع کن.", reply_markup=back_to_auto())
        return
    await q.answer()
    await safe_edit(q, code_prompt(lg["code"]), parse_mode="HTML", reply_markup=code_keyboard())


async def auto_admin_callbacks(q, d: str, ud: dict) -> bool:
    """Handles the adm:auto* callbacks. Returns True if handled."""
    if d == "adm:auto":
        ud.pop("admin_action", None)
        text, kb = auto_panel()
        await q.answer()
        await safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
        return True
    if d == "adm:auto_connect":
        if not userbot.available():
            await q.answer("TG_API_ID / TG_API_HASH یا telethon تنظیم نشده.", show_alert=True)
            return True
        ud["admin_action"] = "ub_phone"
        await q.answer()
        await safe_edit(q, "📱 شماره اکانتی که باید واریزها را دریافت کند (با کد کشور) بفرست.\nمثال: +989123456789",
                        reply_markup=back_to_auto())
        return True
    if d == "adm:auto_disconnect":
        await userbot.stop(logout=True)
        await set_setting("auto_deposit_enabled", "0")
        text, kb = auto_panel()
        await q.answer("قطع شد")
        await safe_edit(q, "🔌 اکانت قطع شد و تأیید خودکار خاموش شد.\n\n" + text, parse_mode="HTML", reply_markup=kb)
        return True
    if d == "adm:auto_toggle":
        if setting("auto_deposit_enabled", "0") == "1":
            await set_setting("auto_deposit_enabled", "0")
        else:
            if not userbot.connected:
                await q.answer("اول اکانت را وصل کن.", show_alert=True)
                return True
            if not setting("bank_bot", "").strip():
                await q.answer("اول بات بانک میویی را در «بات و الگوها» تنظیم کن.", show_alert=True)
                return True
            await set_setting("auto_deposit_enabled", "1")
        text, kb = auto_panel()
        await q.answer("ذخیره شد")
        await safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
        return True
    if d == "adm:auto_settings":
        ud["admin_action"] = "auto_settings"
        txt = ("⚙️ <b>بات و الگوهای بانک میویی</b>\n\n"
               f"🤖 بات: <code>{esc(setting('bank_bot') or '—')}</code>\n"
               f"🔎 فیلتر پیام واریز: <code>{esc(setting('bank_filter_re'))}</code>\n"
               f"💰 الگوی مبلغ: <code>{esc(setting('bank_amount_re'))}</code>\n"
               f"👤 الگوی حساب فرستنده: <code>{esc(setting('bank_sender_re'))}</code>\n"
               f"⏱ مهلت: {s_int('deposit_window_minutes', 60)} دقیقه\n\n"
               "فرمت‌ها:\nbank @username_bot\nbank_in regex\nbank_amount regex\nbank_sender regex\nwindow 60\n\n"
               "الگوهای مبلغ و حساب باید دقیقاً یک گروه پرانتزی ( ) داشته باشند. "
               "الگوهای پیش‌فرض فقط حدس هستند؛ حتماً با «تست پیام» روی پیام واقعی امتحانشان کن.")
        await q.answer()
        await safe_edit(q, txt, parse_mode="HTML", reply_markup=back_to_auto())
        return True
    if d == "adm:auto_test":
        ud["admin_action"] = "auto_test"
        await q.answer()
        await safe_edit(q, "🧪 متن یک پیام واقعی واریز بانک میویی را همین‌جا بفرست تا ببینم الگوها چه چیزی در می‌آورند.\n"
                           "(می‌توانی چند بار تست کنی.)", reply_markup=back_to_auto())
        return True
    if d == "adm:auto_log":
        rows = await pool.fetch("SELECT * FROM bank_messages ORDER BY id DESC LIMIT 8")
        lines = ["📨 <b>آخرین پیام‌های بانک</b>", ""]
        for r in rows:
            lines.append(f"#{r['id']} | {esc(r['status'])} | مبلغ: {format_mio(r['amount']) if r['amount'] else '-'} | "
                         f"حساب: {esc(r['sender_account'] or '-')}\n<code>{esc((r['body'] or '')[:90])}</code>")
        if not rows:
            lines.append("هنوز پیامی از بات بانک دریافت نشده.")
        await q.answer()
        await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=back_to_auto())
        return True
    return False


async def auto_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, text: str) -> bool:
    """Admin text states for the auto-deposit section. Returns True if handled."""
    reply = update.message.reply_text
    if action == "ub_phone":
        phone = re.sub(r"[\s\-()]", "", text.translate(_DIGITS))
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            await reply("❌ شماره نامعتبر است. مثال: +989123456789")
            return True
        if not phone.startswith("+"):
            phone = "+" + phone
        try:
            await userbot.begin_login(phone)
        except FloodWaitError as e:
            context.user_data.clear()
            await reply(f"⏳ تلگرام موقتاً محدود کرده؛ {e.seconds} ثانیه بعد دوباره امتحان کن.")
            return True
        except PhoneNumberInvalidError:
            await reply("❌ این شماره از نظر تلگرام نامعتبر است.")
            return True
        except Exception:
            logger.exception("userbot send_code failed")
            context.user_data.clear()
            await reply("❌ ارسال کد ناموفق بود. شماره و TG_API_ID/TG_API_HASH را بررسی کن.")
            return True
        context.user_data["admin_action"] = "ub_code"
        await reply(code_prompt(""), parse_mode="HTML", reply_markup=code_keyboard())
        return True

    if action == "ub_code":
        await reply("🔐 کد را فقط با دکمه‌های شیشه‌ایِ پیام بالا وارد کن؛ تایپ کردنش در چت آن را باطل می‌کند.")
        return True

    if action == "ub_password":
        try:
            await update.message.delete()  # never leave the 2FA password sitting in the chat
        except TelegramError:
            pass
        try:
            res = await userbot.submit_password(text)
        except FloodWaitError as e:
            await context.bot.send_message(ADMIN_ID, f"⏳ محدودیت تلگرام؛ {e.seconds} ثانیه بعد دوباره امتحان کن.")
            return True
        except Exception:
            logger.exception("userbot password failed")
            context.user_data.clear()
            await context.bot.send_message(ADMIN_ID, "❌ ورود ناموفق بود؛ از اول شروع کن.", reply_markup=admin_menu())
            return True
        if res == "invalid":
            await context.bot.send_message(ADMIN_ID, "❌ رمز اشتباه است؛ دوباره بفرست.")
            return True
        context.user_data.clear()
        t, kb = auto_panel()
        await context.bot.send_message(ADMIN_ID, "✅ اکانت با موفقیت متصل شد.\n\n" + t, parse_mode="HTML", reply_markup=kb)
        return True

    if action == "auto_settings":
        m = re.fullmatch(r"(bank|bank_in|bank_amount|bank_sender|window)\s+(.+)", text, re.S)
        if not m:
            await reply("❌ فرمت نامعتبر.")
            return True
        k, v = m.group(1).lower(), m.group(2).strip()
        try:
            if k == "bank":
                v = v.lstrip("@")
                if not re.fullmatch(r"[A-Za-z0-9_]{4,64}|\d{5,15}", v):
                    raise ValueError
                await set_setting("bank_bot", v)
            elif k == "window":
                x = int(v)
                if not 5 <= x <= 1440:
                    raise ValueError
                await set_setting("deposit_window_minutes", str(x))
            else:
                rx = re.compile(v)
                if k in ("bank_amount", "bank_sender") and rx.groups != 1:
                    raise ValueError
                await set_setting({"bank_in": "bank_filter_re", "bank_amount": "bank_amount_re",
                                   "bank_sender": "bank_sender_re"}[k], v)
        except (ValueError, re.error):
            await reply("❌ مقدار نامعتبر (الگوها باید regex درست با دقیقاً یک گروه ( ) باشند).")
            return True
        context.user_data.clear()
        t, kb = auto_panel()
        await reply("✅ ذخیره شد.\n\n" + t, parse_mode="HTML", reply_markup=kb)
        return True

    if action == "auto_test":
        p = parse_bank_text(text)
        if p["error"]:
            await reply(f"❌ خطا در الگو: {p['error']}")
            return True
        ok = p["ok_filter"] and p["amount"] > 0 and len(p["sender"]) == 12
        await reply(
            "🧪 <b>نتیجه تست</b>\n\n"
            f"🔎 فیلتر واریز: {'✅' if p['ok_filter'] else '❌ (این پیام نادیده گرفته می‌شد)'}\n"
            f"💰 مبلغ: {format_mio(p['amount']) if p['amount'] else '❌ پیدا نشد'}\n"
            f"👤 حساب فرستنده: {esc(p['sender']) if len(p['sender']) == 12 else '❌ پیدا نشد / ۱۲ رقم نیست'}\n\n"
            + ("✅ همه‌چیز درست تشخیص داده شد." if ok else "⚠️ الگوها را اصلاح کن (⚙️ بات و الگوها)."),
            parse_mode="HTML")
        return True
    return False


# ============================================================
# Glass UI (v7): inline panels, premium emoji, bank + games panels, dynamite
# ============================================================
BOT_USERNAME = ""
BUTTON_COLORS = os.getenv("BUTTON_COLORS", "1") != "0"   # coloured glass buttons, ON by default (auto-disables itself if Telegram rejects them)
_PE: dict = {}                                            # plain emoji -> custom (premium) emoji id

# Put premium emoji ids here, or set them live with /pe (see below), or via env PREMIUM_EMOJI_JSON.
# Example: {"🎲": "5368324170671202286"}.  Anything missing simply falls back to the normal emoji.
DEFAULT_PE: dict = {
    "💰": "5418261098291815736", "🪙": "5823445637231814311", "✅": "5938109560249127910",
    "❌": "5819154526816444042", "👇": "5820903824046432799", "👤": "5843973755545590553",
    "🎁": "5927029738626359516", "🏦": "5832240029446971049", "💳": "5888625992196431593",
    "🏆": "5818913299978263825", "🎲": "5818704981179505821", "🎮": "5818704981179505821",
    "➕": "5938558852482994666", "➖": "5936151695112278046", "📜": "5888937012253171131",
    "🔙": "5832364184066614283", "👥": "5859432880442187580", "🎯": "5857454223368657979",
    "🧨": "5929177175029718803", "💥": "5839270298205035832", "⏳": "5938348905891632091",
    "⏰": "5825656411517886523", "🛡": "5938098930205069821", "🏁": "5888937012253171131",
    "🥈": "5856946687083290613", "⚠️": "5938167138580741203", "🎉": "5832433801191498078",
    "❓": "6012741643583622516", "🎰": "6012438204144165493",
}

VIEW_KINDS = {"home", "games", "bank", "daily", "top", "hist", "ref", "help"}
GAME_AMOUNTS = [20_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
SIDE_NAMES = {"e": "زوج", "o": "فرد", "w": "سفید", "r": "قرمز"}

DYNAMITE_DDL = """
CREATE TABLE IF NOT EXISTS dynamite_duels(
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    creator_id BIGINT NOT NULL,
    creator_name TEXT NOT NULL DEFAULT '',
    opponent_id BIGINT,
    opponent_name TEXT,
    amount BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    bomb_index INT,
    revealed TEXT NOT NULL DEFAULT '[]',
    safe_remaining INT NOT NULL DEFAULT 8,
    turn_id BIGINT,
    turn_deadline DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (chat_id, message_id)
)
"""


async def init_dynamite() -> None:
    async with pool.acquire() as c:
        await c.execute(DYNAMITE_DDL)
    # v7 first drop had a solo dynamite table: refund any open stake once, then drop it.
    try:
        if await pool.fetchval("SELECT 1 FROM information_schema.tables WHERE table_name='dynamite_games'"):
            async with pool.acquire() as c, c.transaction():
                rows = await c.fetch("DELETE FROM dynamite_games RETURNING user_id, amount")
                for r in rows:
                    await adjust(c, int(r["user_id"]), int(r["amount"]), "dynamite_v7_refund")
                await c.execute("DROP TABLE dynamite_games")
    except Exception:
        logger.exception("old dynamite table migration failed (ignored)")


def set_bot_username(name: str) -> None:
    global BOT_USERNAME
    BOT_USERNAME = name or ""


def load_premium_emoji() -> None:
    _PE.clear()
    _PE.update({str(k): str(v) for k, v in DEFAULT_PE.items()})
    for raw in (os.getenv("PREMIUM_EMOJI_JSON", ""), setting("premium_emoji", "{}")):
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        if isinstance(d, dict):
            _PE.update({str(k): str(v) for k, v in d.items() if re.fullmatch(r"\d{5,25}", str(v))})


def pe(e: str) -> str:
    """Emoji for HTML messages: premium (custom) emoji when an id is known, otherwise the normal one."""
    cid = _PE.get(e)
    return f'<tg-emoji emoji-id="{cid}">{e}</tg-emoji>' if cid else e


def gbtn(emoji: str, label: str, data: Optional[str] = None, url: Optional[str] = None,
         style: Optional[str] = None) -> InlineKeyboardButton:
    """One glass button. With a premium emoji id the emoji becomes the button icon."""
    cid = _PE.get(emoji) if emoji else None
    text = label if (cid or not emoji) else f"{emoji} {label}"
    api = {}
    if cid:
        api["icon_custom_emoji_id"] = cid
    if style and BUTTON_COLORS:
        api["style"] = style      # "primary" | "success" | "danger"
    kw = {"api_kwargs": api} if api else {}
    if url:
        return InlineKeyboardButton(text, url=url, **kw)
    return InlineKeyboardButton(text, callback_data=data, **kw)


def short_amt(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1_000 and n % 1_000 == 0:
        return f"{n // 1_000}K"
    return str(n)


def chunk(items: list, n: int) -> list:
    return [items[i:i + n] for i in range(0, len(items), n)]


def bot_link(param: str = "") -> str:
    if not BOT_USERNAME:
        return ""
    return f"https://t.me/{BOT_USERNAME}" + (f"?start={param}" if param else "")


# ---------------- main (home) menu ----------------
def main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    uid = user_id or 0
    rows = [
        [gbtn("🎮", "بازی‌ها", f"ui:games:{uid}", style="primary"), gbtn("🏦", "بانک", f"ui:bank:{uid}", style="primary")],
        [gbtn("🎁", "جایزه روزانه", f"ui:daily:{uid}", style="success"), gbtn("🎁", "لینک هدیه", f"ui:gift:{uid}")],
        [gbtn("👥", "دعوت دوستان", f"ui:ref:{uid}"), gbtn("🏆", "برترین‌ها", f"ui:top:{uid}")],
        [gbtn("📜", "تاریخچه", f"ui:hist:{uid}"), gbtn("❓", "راهنما", f"ui:help:{uid}")],
    ]
    link = bot_link()
    if link:
        rows.append([gbtn("➕", "افزودن ربات به گروه", url=f"{link}?startgroup=true")])
    if user_id == ADMIN_ID:
        rows.append([gbtn("🛠", "پنل مدیریت", f"ui:admin:{uid}")])
    return InlineKeyboardMarkup(rows)


def back_kb(uid: int, private: bool, to: Optional[str] = None) -> InlineKeyboardMarkup:
    dest = to or ("home" if private else "bank")
    label = {"home": "منوی اصلی", "bank": "بانک", "games": "پنل بازی‌ها"}.get(dest, "برگشت")
    return InlineKeyboardMarkup([[gbtn("🔙", label, f"ui:{dest}:{uid}")]])


async def claim_daily(u) -> str:
    now, remain, reward = utc_ts(), 0, 0
    async with pool.acquire() as c, c.transaction():
        last = float(await c.fetchval("SELECT last_daily FROM users WHERE user_id=$1 FOR UPDATE", u.id) or 0)
        if now - last < 86400:
            remain = int(86400 - (now - last))
        else:
            reward = max(0, s_int("daily_bonus", 5000))
            await c.execute("UPDATE users SET last_daily=$2 WHERE user_id=$1", u.id, now)
            if reward:
                await adjust(c, u.id, reward, "daily_bonus")
    if remain:
        h, rem = divmod(remain, 3600)
        return f"⏳ جایزه روزانه‌ات قبلاً دریافت شده.\nحدود <b>{h}</b> ساعت و <b>{rem // 60}</b> دقیقه دیگه دوباره بیا."
    return (f"{pe('🎁')} <b>جایزه روزانه دریافت شد!</b>\n\n+{format_mio(reward)} میو\n"
            f"{pe('💰')} موجودی: {format_mio(await get_balance(u.id))} میو")


async def build_view(kind: str, u, private: bool, back: Optional[str] = None):
    """Returns (html_text, InlineKeyboardMarkup) for every glass screen."""
    uid = u.id

    if kind == "home":
        bal = await get_balance(uid)
        text = (f"{pe('🎰')} <b>MEOWBET | میوبت</b>\n\n"
                f"{pe('🪙')} موجودی: <b>{format_mio(bal)}</b> میو\n\n"
                f"{pe('🎮')} بازی‌ها داخل گروه انجام می‌شن؛ توی گروه بنویس:\n"
                "«میو بت پنل بازی ها»\n\n👇 از دکمه‌ها استفاده کن")
        return text, main_keyboard(uid)

    if kind == "bank":
        bal = await get_balance(uid)
        st = await pool.fetchrow("SELECT games_count, wins_count FROM users WHERE user_id=$1", uid)
        pending = await pool.fetchval("SELECT COUNT(*) FROM transactions WHERE user_id=$1 AND status='pending'", uid)
        text = (f"{pe('🏦')} <b>بانک MEOWBET</b>\n━━━━━━━━━━━━━━\n"
                f"{pe('👤')} {uname(u)}\n"
                f"{pe('🪙')} موجودی: <b>{format_mio(bal)}</b> میو\n"
                f"{pe('🎮')} بازی‌ها: {st['games_count'] if st else 0}  |  {pe('🏆')} برد: {st['wins_count'] if st else 0}")
        if pending:
            text += f"\n⏳ درخواست در انتظار: {pending}"
        if private:
            top = [gbtn("➕", "واریز", f"ui:dep:{uid}", style="success"), gbtn("➖", "برداشت", f"ui:wd:{uid}", style="danger")]
            gift = gbtn("🎁", "لینک هدیه", f"ui:gift:{uid}")
        else:
            top = [gbtn("➕", "واریز", url=bot_link("deposit"), style="success") if BOT_USERNAME else gbtn("➕", "واریز", f"ui:dep:{uid}"),
                   gbtn("➖", "برداشت", url=bot_link("withdraw"), style="danger") if BOT_USERNAME else gbtn("➖", "برداشت", f"ui:wd:{uid}")]
            gift = gbtn("🎁", "لینک هدیه", url=bot_link("newgift")) if BOT_USERNAME else gbtn("🎁", "لینک هدیه", f"ui:gift:{uid}")
        rows = [top,
                [gbtn("🎁", "جایزه روزانه", f"ui:daily:{uid}", style="success"), gift],
                [gbtn("📜", "تاریخچه", f"ui:hist:{uid}"), gbtn("🏆", "برترین‌ها", f"ui:top:{uid}")],
                [gbtn("🔄", "بروزرسانی", f"ui:bank:{uid}"), gbtn("🎮", "بازی‌ها", f"ui:games:{uid}")]]
        if private:
            rows.append([gbtn("🔙", "منوی اصلی", f"ui:home:{uid}")])
        return text, InlineKeyboardMarkup(rows)

    if kind == "games":
        if private:
            text = (f"{pe('🎮')} <b>پنل بازی‌ها</b>\n\nبازی‌ها داخل گروه انجام می‌شن.\n"
                    "ربات رو به گروهت اضافه کن و اونجا بنویس:\n«میو بت پنل بازی ها»")
            rows = []
            if BOT_USERNAME:
                rows.append([gbtn("➕", "افزودن ربات به گروه", url=f"{bot_link()}?startgroup=true")])
            rows.append([gbtn("❓", "راهنما", f"ui:help:{uid}:games")])
            rows.append([gbtn("🔙", "منوی اصلی", f"ui:home:{uid}")])
            return text, InlineKeyboardMarkup(rows)
        text = f"{pe('🎮')} <b>پنل بازی‌های MEOWBET</b>\n\nبازی مدنظرت رو انتخاب کن 👇"
        rows = [
            [gbtn("🎲", "تاس", f"ui:g:{uid}:dice", style="primary"), gbtn("🎯", "دارت", f"ui:g:{uid}:dart", style="primary")],
            [gbtn("💩", "پوپ", f"ui:g:{uid}:poop", style="primary"), gbtn("🧨", "دینامیت", f"ui:g:{uid}:dyn", style="danger")],
            [gbtn("⚽", "فوتبال", f"ui:g:{uid}:foot", style="success")],
            [gbtn("🏦", "بانک", f"ui:bank:{uid}"), gbtn("❓", "راهنما", f"ui:help:{uid}:games")],
        ]
        return text, InlineKeyboardMarkup(rows)

    if kind == "daily":
        return await claim_daily(u), back_kb(uid, private, back)

    if kind == "top":
        count, rewards = get_leaderboard_rewards()
        rows = await pool.fetch(
            "SELECT username,first_name,balance FROM users ORDER BY balance DESC, user_id ASC LIMIT $1", max(10, count))
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{pe('🏆')} <b>جدول برترین‌ها</b>", ""]
        for i, r in enumerate(rows, 1):
            name = esc(f"@{r['username']}" if r["username"] else r["first_name"])
            extra = f" | 🎁 {format_mio(rewards[i])}" if i in rewards else ""
            lines.append(f"{medals.get(i, str(i) + '.')} {name} — {format_mio(r['balance'])} میو{extra}")
        if not rows:
            lines.append("هنوز کاربری ثبت نشده است.")
        return "\n".join(lines), back_kb(uid, private, back)

    if kind == "hist":
        rows = await pool.fetch(
            "SELECT game,amount,result,payout FROM game_stats WHERE user_id=$1 ORDER BY id DESC LIMIT 10", uid)
        if not rows:
            return f"{pe('📜')} هنوز بازی ثبت‌شده‌ای نداری.", back_kb(uid, private, back)
        lines = [f"{pe('📜')} <b>آخرین بازی‌های شما</b>", ""]
        for r in rows:
            lines.append(f"{GAME_LABELS.get(r['game'], r['game'])} | {format_mio(r['amount'])} | "
                         f"{RESULT_LABELS.get(r['result'], r['result'])} | {format_mio(r['payout'])}")
        return "\n".join(lines), back_kb(uid, private, back)

    if kind == "ref":
        invited = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referral_by=$1", uid)
        link = f"{bot_link()}?start=ref_{uid}" if BOT_USERNAME else ""
        text = (f"{pe('👥')} <b>دعوت دوستان</b>\n\n🔗 لینک شما:\n<code>{esc(link)}</code>\n\n"
                f"🎁 دعوت‌کننده: {format_mio(s_int('referrer_bonus', 100000))} میو\n"
                f"🎁 دعوت‌شده: {format_mio(s_int('referred_bonus', 50000))} میو\n"
                f"✅ پاداش بعد از انجام {s_int('referral_min_games', 3)} بازی توسط دعوت‌شده فعال می‌شود.\n"
                f"👥 تعداد دعوت‌ها: {invited}")
        return text, back_kb(uid, private, back)

    # help
    return esc(help_text()), back_kb(uid, private, back)


def view_cmd(kind: str):
    """Text-trigger handler (e.g. «بانک», «میو بت پنل بازی ها») -> sends the glass panel."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u, m = update.effective_user, update.effective_message
        if not u or not m:
            return
        await ensure_user(u)
        private = is_private(update)
        text, kb = await build_view(kind, u, private)
        kw = {} if private else {"reply_to_message_id": m.message_id}
        await m.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True, **kw)
    handler.__name__ = f"view_{kind}"
    return handler


# ---------------- amount / preset keyboards for finance + gift flows ----------------
def deposit_presets(uid: int) -> InlineKeyboardMarkup:
    amounts = [50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000]
    rows = [[gbtn("", short_amt(a), f"ui:dp:{uid}:{a}") for a in r] for r in chunk(amounts, 3)]
    rows.append([gbtn("❌", "لغو", f"ui:cx:{uid}", style="danger")])
    return InlineKeyboardMarkup(rows)


def withdraw_presets(uid: int) -> InlineKeyboardMarkup:
    amounts = [100_000, 250_000, 500_000, 1_000_000, 5_000_000]
    btns = [gbtn("", short_amt(a), f"ui:wp:{uid}:{a}") for a in amounts] + [gbtn("💯", "همه موجودی", f"ui:wp:{uid}:all")]
    rows = chunk(btns, 3)
    rows.append([gbtn("❌", "لغو", f"ui:cx:{uid}", style="danger")])
    return InlineKeyboardMarkup(rows)


def gift_capacity_presets(uid: int) -> InlineKeyboardMarkup:
    caps = [5, 10, 25, 50, 100, 500]
    rows = [[gbtn("👥", str(c), f"ui:gc:{uid}:{c}") for c in r] for r in chunk(caps, 3)]
    rows.append([gbtn("❌", "لغو", f"ui:cx:{uid}", style="danger")])
    return InlineKeyboardMarkup(rows)


def gift_amount_presets(uid: int) -> InlineKeyboardMarkup:
    amounts = [10_000, 20_000, 50_000, 100_000, 250_000, 500_000]
    rows = [[gbtn("", short_amt(a), f"ui:ga:{uid}:{a}") for a in r] for r in chunk(amounts, 3)]
    rows.append([gbtn("❌", "لغو", f"ui:cx:{uid}", style="danger")])
    return InlineKeyboardMarkup(rows)


# ---------------- games panel (group) ----------------
GAME_META = {
    "dice": ("🎲", "تاس"), "dart": ("🎯", "دارت"), "poop": ("💩", "پوپ"),
    "dyn": ("🧨", "دینامیت"), "foot": ("⚽", "فوتبال"),
}


def game_rules(game: str) -> str:
    if game == "dice":
        return f"۳ تاس 🎲 بفرست؛ اگه مجموع با انتخابت بخونه برنده‌ای (×{s_float('dice_mult', 1.5):g})"
    if game == "dart":
        return f"یه دارت 🎯 بفرست؛ اگه رنگش با انتخابت بخونه برنده‌ای (×{s_float('dart_multiplier', 2.0):g})"
    if game == "poop":
        cfg = poop_config()
        return f"{cfg['rows']} مرحله؛ هر مرحله یه خونه امن انتخاب کن و بین مراحل برداشت کن"
    if game == "dyn":
        return ("۲ نفره ۳×۳؛ یه نفر با «پیوستن» میاد و نوبتی خونه باز می‌کنید. "
                "هرکی بمب رو باز کنه می‌بازه و برنده کل مبلغ رو می‌بره")
    return ""


def amount_screen(uid: int, game: str, side: str, bal: int):
    icon, title = GAME_META[game]
    side_line = f"\n🎯 انتخاب تو: <b>{SIDE_NAMES[side]}</b>" if side in SIDE_NAMES else ""
    text = (f"{pe(icon)} <b>{title}</b>{side_line}\n\n{esc(game_rules(game))}\n\n"
            f"{pe('🪙')} موجودی: <b>{format_mio(bal)}</b> میو\nمبلغ شرط رو انتخاب کن" +
            (" یا بنویس" if game == "dyn" else "") + " 👇")
    amounts = [a for a in GAME_AMOUNTS if MIN_GAME <= a <= MAX_GAME]
    rows = [[gbtn("", short_amt(a), f"ui:a:{uid}:{game}:{side}:{a}") for a in r] for r in chunk(amounts, 3)]
    back = f"ui:g:{uid}:{game}" if game in ("dice", "dart") else f"ui:games:{uid}"
    rows.append([gbtn("🔙", "برگشت", back)])
    return text, InlineKeyboardMarkup(rows)


async def place_or_error(uid: int, amount: int, reason: str, insert) -> Optional[str]:
    try:
        await place_bet(uid, amount, reason, insert)
        return None
    except ActiveGame:
        return "⚠️ یک بازی فعال داری؛ اول همان را تمام کن."
    except InsufficientFunds:
        return f"❌ موجودی کافی نیست. موجودی: {format_mio(await get_balance(uid))} میو"
    except UserNotFound:
        return "❌ ابتدا /start را بزن."


async def games_panel_step(update: Update, context: ContextTypes.DEFAULT_TYPE, act: str, args: list) -> None:
    q = update.callback_query
    u = q.from_user
    uid = u.id

    # ---- act "g": a game was chosen on the main panel ----
    if act == "g":
        game = args[0] if args else ""
        if game not in GAME_META:
            await q.answer()
            return
        if game == "foot":
            await start_football_panel(update, context)
            return
        icon, title = GAME_META[game]
        if game in ("dice", "dart"):
            opts = [("e", "🟢", "زوج"), ("o", "🟣", "فرد")] if game == "dice" else [("w", "⚪️", "سفید"), ("r", "🔴", "قرمز")]
            text = f"{pe(icon)} <b>{title}</b>\n\n{esc(game_rules(game))}\n\nکدوم رو انتخاب می‌کنی؟"
            rows = [[gbtn(e, n, f"ui:s:{uid}:{game}:{k}") for k, e, n in opts],
                    [gbtn("🔙", "پنل بازی‌ها", f"ui:games:{uid}")]]
            await q.answer()
            await safe_edit(q, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
            return
        text, kb = amount_screen(uid, game, "-", await get_balance(uid))
        await q.answer()
        await safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
        return

    # ---- act "s": side chosen (dice/dart) -> amount screen ----
    if act == "s":
        try:
            game, side = args[0], args[1]
        except IndexError:
            await q.answer()
            return
        if game not in ("dice", "dart") or side not in SIDE_NAMES:
            await q.answer()
            return
        text, kb = amount_screen(uid, game, side, await get_balance(uid))
        await q.answer()
        await safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
        return

    # ---- act "a": amount chosen -> start the game ----
    try:
        game, side, amount = args[0], args[1], int(args[2])
    except (IndexError, ValueError):
        await q.answer()
        return
    if game not in GAME_META or game == "foot":
        await q.answer()
        return
    if not MIN_GAME <= amount <= MAX_GAME:
        await q.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    if not await require_join(update, context):
        return
    chat_id, message_id = q.message.chat_id, q.message.message_id

    if game == "dice":
        if side not in ("e", "o"):
            await q.answer()
            return
        target = SIDE_NAMES[side]

        async def insert(c):
            await c.execute(
                "INSERT INTO dice_bets(user_id,target,amount,dices,chat_id,created_at,message_id) "
                "VALUES($1,$2,$3,'[]',$4,$5,$6)", uid, target, amount, chat_id, utc_ts(), message_id)

        err = await place_or_error(uid, amount, "dice_bet", insert)
        if err:
            await q.answer(err, show_alert=True)
            return
        await q.answer("بازی شروع شد 🎲")
        await safe_edit(
            q, f"{pe('🎲')} <b>بازی تاس شروع شد</b>\n\n👤 کاربر {uname(u)}\n🎯 شرط: {target}\n💰 مبلغ: {format_mio(amount)} میو\n\n"
               "🎲 دقیقاً ۳ تاس واقعی تلگرام بفرست.\n🚫 تاس فورواردشده حساب نمی‌شود.\n"
               f"⏱️ {DICE_TIMEOUT} ثانیه فرصت داری؛ بعد از آن {int(TIMEOUT_PENALTY * 100)}٪ مبلغ جریمه می‌شود.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[gbtn("❌", "لغو شرط", f"cancel_dice:{uid}", style="danger")]]))
        return

    if game == "dart":
        if side not in ("w", "r"):
            await q.answer()
            return
        target = SIDE_NAMES[side]

        async def insert(c):
            await c.execute("INSERT INTO dart_bets(user_id,target,amount,chat_id,created_at) VALUES($1,$2,$3,$4,$5)",
                            uid, target, amount, chat_id, utc_ts())

        err = await place_or_error(uid, amount, "dart_bet", insert)
        if err:
            await q.answer(err, show_alert=True)
            return
        await q.answer("بازی شروع شد 🎯")
        await safe_edit(
            q, f"{pe('🎯')} <b>شرط دارت ثبت شد</b>\n\n👤 {uname(u)}\n🎯 هدف: {target}\n💰 مبلغ: {format_mio(amount)} میو\n\n"
               f"یک دارت 🎯 همین‌جا در گروه بفرست.\n⏱️ {DART_TIMEOUT} ثانیه فرصت داری.", parse_mode="HTML")
        return

    if game == "poop":
        if await has_active_game(pool, uid):
            await q.answer("⚠️ یک بازی فعال داری؛ اول همان را تمام کن.", show_alert=True)
            return
        bal = await get_balance(uid)
        if amount > bal:
            await q.answer(f"❌ موجودی کافی نیست: {format_mio(bal)} میو", show_alert=True)
            return
        cfg = poop_config()
        kb = InlineKeyboardMarkup([[gbtn("✅", "شروع بازی", f"poop_go:{uid}:{amount}", style="success"),
                                    gbtn("❌", "لغو", f"poop_no:{uid}", style="danger")]])
        await q.answer()
        await safe_edit(
            q, f"{pe('💩')} <b>تأیید شروع بازی پوپ</b>\n\n"
               f"🪙 مبلغ: {format_mio(amount)} میو\n🎯 {cfg['rows']} ردیف و {cfg['cols']} خانه\n"
               f"💩 تعداد پوپ‌ها: {' / '.join(map(str, cfg['counts']))}\n"
               f"📈 ضرایب: {' / '.join('×' + format(x, 'g') for x in cfg['mults'])}\n\nمطمئنی؟",
            parse_mode="HTML", reply_markup=kb)
        return

    if game == "dyn":
        err = await dyn_open_table(u, chat_id, amount, q=q)
        if err:
            await q.answer(err, show_alert=True)
            return
        await q.answer("🧨 میز ساخته شد!")

async def start_football_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    if not await require_join(update, context):
        return
    if await pool.fetchval("SELECT 1 FROM football_games WHERE user_id=$1", uid):
        await q.answer("⚠️ یک بازی فوتبال فعال داری؛ اول شوت‌های باقی‌مانده را بفرست.", show_alert=True)
        return
    shots, goal_values = football_config()
    await pool.execute(
        "INSERT INTO football_games(user_id,chat_id,message_id,shots,goals,created_at) VALUES($1,$2,$3,0,0,$4) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, message_id=EXCLUDED.message_id, "
        "shots=0, goals=0, created_at=EXCLUDED.created_at",
        uid, q.message.chat_id, q.message.message_id, utc_ts())
    await q.answer("⚽ بازی شروع شد!")
    await safe_edit(
        q, f"{pe('⚽')} <b>بازی فوتبال شروع شد!</b>\n\n🎯 تعداد شوت: {shots}\n⚽ هر بار یک توپ فوتبال بفرست.\n"
           f"🥅 اعداد گل: {', '.join(map(str, goal_values))}", parse_mode="HTML")


async def ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) < 3 or not q.message:
        await q.answer()
        return
    act, owner, args = parts[1], parts[2], parts[3:]
    u = q.from_user
    uid = u.id
    if owner != str(uid):
        await q.answer("این پنل مال تو نیست 🙂 خودت بنویس «بانک» یا «میو بت پنل بازی ها»", show_alert=True)
        return
    await ensure_user(u)
    private = q.message.chat.type == ChatType.PRIVATE

    if act in VIEW_KINDS:
        await q.answer()
        text, kb = await build_view(act, u, private, back=args[0] if args else None)
        await safe_edit(q, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return

    if act == "admin":
        if uid != ADMIN_ID or not private:
            await q.answer("دسترسی ندارید", show_alert=True)
            return
        context.user_data.clear()
        await q.answer()
        await safe_edit(q, "🛠 <b>پنل مدیریت میوبت</b>", parse_mode="HTML", reply_markup=admin_menu())
        return

    if act in ("dep", "wd", "gift"):
        if not private:
            await q.answer("این بخش فقط توی پیوی ربات کار می‌کنه.", show_alert=True)
            return
        await q.answer()
        await {"dep": deposit_start, "wd": withdraw_start, "gift": gift_start}[act](update, context)
        return

    if act == "cx":
        context.user_data.clear()
        await q.answer("لغو شد")
        await safe_edit(q, "✅ عملیات لغو شد.", reply_markup=back_kb(uid, private, "home" if private else "bank"))
        return

    if act in ("dp", "wp", "gc", "ga"):
        ud = context.user_data
        ok = {"dp": ud.get("finance_action") == "deposit",
              "wp": ud.get("finance_action") == "withdraw",
              "gc": ud.get("flow") == "gift" and ud.get("gift_step") == "capacity",
              "ga": ud.get("flow") == "gift" and ud.get("gift_step") == "amount"}[act]
        if not private or not ok:
            await q.answer("این درخواست منقضی شده؛ دوباره شروع کن.", show_alert=True)
            return
        try:
            value = await get_balance(uid) if args[0] == "all" else int(args[0])
        except (IndexError, ValueError):
            await q.answer()
            return
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        await finance_and_gift_text(update, context, override=str(value))
        return

    if act in ("g", "s", "a"):
        if private:
            await q.answer("بازی‌ها داخل گروه انجام می‌شن؛ ربات رو به گروه اضافه کن 👥", show_alert=True)
            return
        await games_panel_step(update, context, act, args)
        return

    await q.answer()


# ---------------- Dynamite (2 players, 3x3 board; ported from bot18) ----------------
DYN_JOIN_TIMEOUT = 60
DYN_TURN_TIMEOUT = 60
DYN_GRID = 9
DYN_DIAMOND_ID = "5940725397195853882"
DYN_BOMB_ID = "5929177175029718803"
BLANK = "\u2800"
_AMT_ONLY = r"^\s*[0-9۰-۹٠-٩][0-9۰-۹٠-٩,٬،.]*\s*(?:k|K|m|M|ک|کی|کا|هزار|میلیون|مگ)?\s*$"
_DYN_RE = r"^\s*(?:میو\s?بت\s+)?دینامیت(?:\s+\S+)?\s*$"


def cbtn(text: str, data: str, icon: Optional[str] = None, style: Optional[str] = None) -> InlineKeyboardButton:
    api = {}
    if icon:
        api["icon_custom_emoji_id"] = str(icon)
    if style and BUTTON_COLORS:
        api["style"] = style
    return InlineKeyboardButton(text, callback_data=data, **({"api_kwargs": api} if api else {}))


def raw_name(u) -> str:
    return f"@{u.username}" if u.username else (u.first_name or "کاربر")


def dyn_title() -> str:
    return f"{pe('🎲')} دینامیت {pe('🧨')}"


def dyn_pending_text(amount: int, name: str) -> str:
    return (f"{dyn_title()}\n\n{pe('🪙')} مقدار میو : {format_mio(amount)}\n"
            f"{pe('👤')} سازنده: {esc(name)}")


def dyn_pending_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[cbtn("پیوستن", "dynj", _PE.get("✅"), "success"),
                                  cbtn("لغو", "dync", _PE.get("❌"), "danger")]])


def dyn_active_text(r) -> str:
    turn = r["creator_name"] if r["turn_id"] == r["creator_id"] else r["opponent_name"]
    return (f"{dyn_title()}\n\n{pe('🪙')} مقدار میو : {format_mio(r['amount'])}\n"
            f"{pe('👤')} سازنده : {esc(r['creator_name'])}\n"
            f"{pe('👥')} شرکت کننده : {esc(r['opponent_name'])}\n\n"
            f"{pe('⏳')} نوبت: {esc(turn)}\n"
            f"{pe('🛡')} جاهای امن باقی‌مانده: {r['safe_remaining']}{pe('✅')}\n\n"
            f"{pe('👇')} روی دکمه‌های زیر کلیک کنید.")


def dyn_board_kb(revealed: list) -> InlineKeyboardMarkup:
    rows = []
    for row in range(3):
        rows.append([cbtn(BLANK, f"dynx:{row * 3 + col}", DYN_DIAMOND_ID if row * 3 + col in revealed else None, "primary")
                     for col in range(3)])
    return InlineKeyboardMarkup(rows)


def dyn_result_kb(revealed: list, bomb: Optional[int], payout: int, fee: int) -> InlineKeyboardMarkup:
    rows = []
    for row in range(3):
        cells = []
        for col in range(3):
            i = row * 3 + col
            if bomb is not None and i == bomb:
                cells.append(cbtn(BLANK, "dynn", DYN_BOMB_ID, "danger"))
            elif i in revealed:
                cells.append(cbtn(BLANK, "dynn", DYN_DIAMOND_ID, "primary"))
            else:
                cells.append(cbtn(BLANK, "dynn", None, "primary"))
        rows.append(cells)
    rows.append([cbtn(f"مبلغ برد: {format_mio(payout)}", "dynn", _PE.get("🪙")),
                 cbtn(f"مالیات: {format_mio(fee)}", "dynn", "5889002570633977838")])
    return InlineKeyboardMarkup(rows)


def dyn_result_text(middle: str, winner: str, loser: str) -> str:
    return (f"{pe('🎲')} نتیجه بازی {pe('🏁')}\n\n{middle}\n"
            f"{pe('🏆')} برنده: {esc(winner)}\n{pe('🥈')} بازنده: {esc(loser)}")


async def dyn_open_table(u, chat_id: int, amount: int, q=None, msg=None) -> Optional[str]:
    """Take the creator's stake and publish the table. q = edit that callback message, msg = reply to that message.
    Returns an error text (nothing charged) or None."""
    if not MIN_GAME <= amount <= MAX_GAME:
        return f"❌ مبلغ باید بین {format_mio(MIN_GAME)} و {format_mio(MAX_GAME)} میو باشه."
    try:
        await atomic_balance(u.id, -amount, "dynamite_bet")
    except InsufficientFunds:
        return f"❌ موجودی کافی نیست. موجودی: {format_mio(await get_balance(u.id))} میو"
    except UserNotFound:
        return "❌ ابتدا /start را بزن."
    name = raw_name(u)
    text, kb = dyn_pending_text(amount, name), dyn_pending_kb()
    try:
        if q is not None:
            sent = await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            sent = await msg.reply_text(text, parse_mode="HTML", reply_markup=kb, reply_to_message_id=msg.message_id)
        await pool.execute(
            "INSERT INTO dynamite_duels(chat_id,message_id,creator_id,creator_name,amount,status,created_at) "
            "VALUES($1,$2,$3,$4,$5,'pending',$6)", chat_id, sent.message_id, u.id, name, amount, utc_ts())
    except Exception:
        logger.exception("dynamite table creation failed; refunding user=%s", u.id)
        await atomic_balance(u.id, amount, "dynamite_refund")
        return "❌ ساخت میز ناموفق بود؛ مبلغ برگشت داده شد."
    return None


async def dyn_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«میو بت دینامیت» / «دینامیت 100k» typed in a group."""
    u, m = update.effective_user, update.effective_message
    if not u or not m:
        return
    if not is_group(update):
        await m.reply_text("🧨 دینامیت داخل گروه انجام می‌شه.")
        return
    await remember_group(update)
    if not await require_join(update, context):
        return
    await ensure_user(u)
    parts = (m.text or "").split()
    arg = parts[-1] if parts and parts[-1] != "دینامیت" else ""
    chat_id = update.effective_chat.id
    if arg:
        amount = parse_amount(arg)
        if amount <= 0:
            await m.reply_text("❌ مبلغ نامعتبره. مثال: میو بت دینامیت 50k")
            return
        err = await dyn_open_table(u, chat_id, amount, msg=m)
        if err:
            await m.reply_text(err)
        return
    text, kb = amount_screen(u.id, "dyn", "-", await get_balance(u.id))
    prompt = await m.reply_text(text, parse_mode="HTML", reply_markup=kb, reply_to_message_id=m.message_id)
    context.chat_data[f"dynwait:{u.id}"] = {"exp": time.time() + 90, "msg": prompt.message_id}


async def dyn_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The amount typed after a bare «میو بت دینامیت» creates the table."""
    u, m = update.effective_user, update.effective_message
    if not u or not m:
        return
    key = f"dynwait:{u.id}"
    w = context.chat_data.get(key)
    if not w:
        return
    if w["exp"] < time.time():
        context.chat_data.pop(key, None)
        return
    amount = parse_amount(m.text or "")
    if amount <= 0:
        return
    context.chat_data.pop(key, None)
    err = await dyn_open_table(u, update.effective_chat.id, amount, msg=m)
    if err:
        await m.reply_text(err)
    else:
        try:
            await context.bot.delete_message(update.effective_chat.id, w["msg"])
        except TelegramError:
            pass
    raise ApplicationHandlerStop


async def dyn_settle(c, r, loser_id: int):
    """Inside a transaction: pay the winner (stake x2 minus house fee), log both players, remove the table."""
    amount = int(r["amount"])
    winner_id = int(r["opponent_id"] if loser_id == r["creator_id"] else r["creator_id"])
    await c.fetch("SELECT user_id FROM users WHERE user_id = ANY($1::bigint[]) ORDER BY user_id FOR UPDATE",
                  [winner_id, int(loser_id)])
    prize, fee = apply_fee(amount, amount * 2)
    await c.execute("DELETE FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2", r["chat_id"], r["message_id"])
    await adjust(c, winner_id, prize, "dynamite_win")
    await log_game(c, winner_id, "dynamite", amount, "win", prize, fee)
    await log_game(c, int(loser_id), "dynamite", amount, "loss", 0, 0)
    wn, ln = ((r["creator_name"], r["opponent_name"]) if winner_id == r["creator_id"]
              else (r["opponent_name"], r["creator_name"]))
    return wn, ln, prize, fee


async def dyn_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, data = q.from_user.id, q.data or ""
    chat_id, mid = q.message.chat_id, q.message.message_id
    if data == "dynn":
        await q.answer()
        return
    if data in ("dynj", "dync") or data.startswith("dynx:"):
        if data == "dynj" and not await require_join(update, context):
            return
        if data == "dynj":
            await ensure_user(q.from_user)
    else:
        await q.answer()
        return

    alert, out = None, None
    async with pool.acquire() as c, c.transaction():
        r = await c.fetchrow("SELECT * FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2 FOR UPDATE", chat_id, mid)
        if not r:
            alert = "این بازی دیگه فعال نیست."
        elif data == "dync":
            if r["status"] != "pending":
                alert = "بازی شروع شده و قابل لغو نیست."
            elif uid != r["creator_id"]:
                alert = "فقط سازنده می‌تونه لغو کنه."
            else:
                await c.execute("DELETE FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2", chat_id, mid)
                await adjust(c, uid, int(r["amount"]), "dynamite_cancel")
                out = ("cancel", r)
        elif data == "dynj":
            if r["status"] != "pending":
                alert = "این بازی دیگه فعال نیست."
            elif uid == r["creator_id"]:
                alert = "سازنده نمی‌تونه به بازی خودش بپیونده."
            else:
                try:
                    await adjust(c, uid, -int(r["amount"]), "dynamite_bet")
                except InsufficientFunds:
                    alert = "❌ موجودی کافی نداری."
                except UserNotFound:
                    alert = "❌ ابتدا /start را بزن."
                else:
                    name = raw_name(q.from_user)
                    bomb = random.randrange(DYN_GRID)
                    await c.execute(
                        "UPDATE dynamite_duels SET status='active', opponent_id=$3, opponent_name=$4, bomb_index=$5, "
                        "revealed='[]', safe_remaining=$6, turn_id=creator_id, turn_deadline=$7 "
                        "WHERE chat_id=$1 AND message_id=$2", chat_id, mid, uid, name, bomb, DYN_GRID - 1,
                        utc_ts() + DYN_TURN_TIMEOUT)
                    out = ("join", await c.fetchrow(
                        "SELECT * FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2", chat_id, mid))
        else:  # dynx:<idx>
            if r["status"] != "active":
                alert = "بازی هنوز شروع نشده."
            elif uid not in (r["creator_id"], r["opponent_id"]):
                alert = "تو توی این بازی نیستی."
            elif uid != r["turn_id"]:
                alert = "نوبت تو نیست ⏳"
            else:
                try:
                    idx = int(data.split(":")[1])
                except (IndexError, ValueError):
                    idx = -1
                revealed = json.loads(r["revealed"])
                if not 0 <= idx < DYN_GRID or idx in revealed:
                    alert = "این خونه قبلاً باز شده."
                elif idx == r["bomb_index"]:
                    wn, ln, prize, fee = await dyn_settle(c, r, uid)
                    loser = r["creator_name"] if uid == r["creator_id"] else r["opponent_name"]
                    out = ("boom", r, revealed, wn, ln, prize, fee, loser)
                else:
                    revealed.append(idx)
                    nxt = r["opponent_id"] if uid == r["creator_id"] else r["creator_id"]
                    await c.execute(
                        "UPDATE dynamite_duels SET revealed=$3, safe_remaining=safe_remaining-1, turn_id=$4, turn_deadline=$5 "
                        "WHERE chat_id=$1 AND message_id=$2", chat_id, mid, json.dumps(revealed), nxt,
                        utc_ts() + DYN_TURN_TIMEOUT)
                    out = ("next", await c.fetchrow(
                        "SELECT * FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2", chat_id, mid), revealed)
    if alert:
        await q.answer(alert, show_alert=True)
        return
    kind = out[0]
    if kind == "cancel":
        await q.answer("لغو شد")
        await safe_edit(q, f"{pe('🎲')} بازی لغو شد.{pe('❌')}\n{pe('💰')} {format_mio(out[1]['amount'])} میو برگردونده شد.",
                        parse_mode="HTML")
    elif kind == "join":
        await q.answer()
        await safe_edit(q, dyn_active_text(out[1]), parse_mode="HTML", reply_markup=dyn_board_kb([]))
    elif kind == "next":
        await q.answer()
        await safe_edit(q, dyn_active_text(out[1]), parse_mode="HTML", reply_markup=dyn_board_kb(out[2]))
    else:
        _, r, revealed, wn, ln, prize, fee, loser = out
        await q.answer("💥 بووم!")
        middle = f"{pe('💥')} بازیکن {esc(loser)} منفجر شد{pe('🧨')}\u200f"
        await safe_edit(q, dyn_result_text(middle, wn, ln), parse_mode="HTML",
                        reply_markup=dyn_result_kb(revealed, int(r["bomb_index"]), prize, fee))


async def expire_dynamite(bot) -> None:
    """DB-driven timeouts (restart-safe): unjoined tables refund, a player who misses his turn loses."""
    now = utc_ts()
    for r in await pool.fetch("SELECT chat_id,message_id FROM dynamite_duels WHERE status='pending' AND created_at < $1",
                              now - DYN_JOIN_TIMEOUT):
        row = None
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                "DELETE FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2 AND status='pending' AND created_at < $3 RETURNING *",
                r["chat_id"], r["message_id"], now - DYN_JOIN_TIMEOUT)
            if row:
                await adjust(c, int(row["creator_id"]), int(row["amount"]), "dynamite_timeout_refund")
        if row:
            try:
                await bot.edit_message_text(
                    f"{pe('⏰')} زمان تموم شد\n{pe('💰')} {format_mio(row['amount'])} میو به سازنده برگردونده شد.",
                    chat_id=row["chat_id"], message_id=row["message_id"], parse_mode="HTML")
            except TelegramError:
                pass
    for r in await pool.fetch("SELECT chat_id,message_id FROM dynamite_duels WHERE status='active' AND turn_deadline < $1", now):
        out = None
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                "SELECT * FROM dynamite_duels WHERE chat_id=$1 AND message_id=$2 FOR UPDATE", r["chat_id"], r["message_id"])
            if row and row["status"] == "active" and row["turn_deadline"] < now:
                wn, ln, prize, fee = await dyn_settle(c, row, int(row["turn_id"]))
                out = (row, wn, ln, prize, fee)
        if out:
            row, wn, ln, prize, fee = out
            middle = (f"{pe('⏰')} زمان تموم شد\n{pe('⚠️')} بازنده به دلیل انتخاب نکردن در تایم تعیین شده باخت.{pe('❌')}")
            try:
                await bot.edit_message_text(
                    dyn_result_text(middle, wn, ln), chat_id=row["chat_id"], message_id=row["message_id"],
                    parse_mode="HTML", reply_markup=dyn_result_kb(json.loads(row["revealed"]), None, prize, fee))
            except TelegramError:
                pass


# ---------------- premium emoji helpers (admin only) ----------------
async def emoji_id_helper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends a message containing premium emoji in private -> bot replies with their ids."""
    u, m = update.effective_user, update.effective_message
    if not u or u.id != ADMIN_ID or not m or not is_private(update):
        return
    if context.user_data.get("admin_action"):
        return  # admin is typing a broadcast / setting: never interfere
    ents = [e for e in (m.entities or []) if e.type == "custom_emoji"]
    if not ents:
        return
    lines = [f"{m.parse_entity(e)}  <code>{e.custom_emoji_id}</code>" for e in ents]
    await m.reply_text("🆔 آیدی ایموجی‌ها:\n\n" + "\n".join(lines) +
                       "\n\nبرای ست کردن روی ایموجی معمولی:\n<code>/pe 🎲 آیدی</code>", parse_mode="HTML")


async def pe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u, m = update.effective_user, update.effective_message
    if not u or u.id != ADMIN_ID or not m:
        return
    args = context.args or []
    try:
        cur = json.loads(setting("premium_emoji", "{}") or "{}")
        cur = cur if isinstance(cur, dict) else {}
    except ValueError:
        cur = {}
    if args and args[0].lower() == "reset":
        await set_setting("premium_emoji", "{}")
        load_premium_emoji()
        await m.reply_text("✅ همه ایموجی‌های پریمیوم پاک شدند.")
        return
    if len(args) == 2 and re.fullmatch(r"\d{5,25}", args[1]):
        cur[args[0]] = args[1]
        await set_setting("premium_emoji", json.dumps(cur, ensure_ascii=False))
        load_premium_emoji()
        await m.reply_text(f"✅ {args[0]} ← {args[1]}")
        return
    listing = "\n".join(f"{k} ← {v}" for k, v in _PE.items()) or "(خالی)"
    await m.reply_text("استفاده: /pe 🎲 آیدی\nپاک کردن همه: /pe reset\n\nفعلی‌ها:\n" + listing)


# ============================================================
# Application wiring, web server, startup
# ============================================================
def build_application() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(make_rate_limiter())
        .concurrent_updates(PerUserProcessor(32))
        .request(HTTPXRequest(connection_pool_size=64, read_timeout=20, write_timeout=20,
                              connect_timeout=10, pool_timeout=20))
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, track_group_message), group=-10)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_flow))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pe", pe_cmd))
    app.add_handler(MessageHandler(filters.Entity("custom_emoji") & filters.ChatType.PRIVATE, emoji_id_helper), group=-3)
    app.add_handler(CommandHandler("help", terminal(show_help)), group=-2)

    def menu(pattern, fn):
        app.add_handler(MessageHandler(filters.Regex(pattern), terminal(fn)), group=-2)

    menu(r"^🛠 پنل مدیریت$", admin_cmd)
    menu(r"^➕ افزودن ربات به گروه$", add_group_button)
    menu(r"^➕ واریز$", deposit_start)
    menu(r"^➖ برداشت$", withdraw_start)
    menu(r"^(🎁 ?لینک هدیه|لینک هدیه 🎁)$", gift_start)
    menu(r"^👥 دعوت دوستان$", view_cmd("ref"))
    # glass panels: typed triggers (work in groups and in private)
    menu(r"^\s*میو\s?بت(?:\s+پنل\s+بازی[\u200c\s]*ها)?\s*$", view_cmd("games"))
    menu(r"^\s*پنل\s+بازی[\u200c\s]*ها\s*$", view_cmd("games"))
    menu(r"^\s*(?:🏦\s*)?(?:بانک|موجودی|💰 موجودی)\s*$", view_cmd("bank"))
    menu(_DYN_RE, dyn_text_cmd)
    app.add_handler(MessageHandler(filters.Regex(_AMT_ONLY) & filters.ChatType.GROUPS, dyn_amount_text), group=-4)
    # legacy text-keyboard buttons (old clients that still show the old keyboard)
    menu(r"^🎮 بازی‌ها$", view_cmd("games"))
    menu(r"^🎁 جایزه روزانه$", view_cmd("daily"))
    menu(r"^🏆 جدول برترین‌ها$", view_cmd("top"))
    menu(r"^📜 تاریخچه$", view_cmd("hist"))
    menu(r"^(راهنما|❓ راهنما)$", view_cmd("help"))

    def game_cmd(pattern, fn):
        app.add_handler(MessageHandler(filters.Regex(pattern), terminal(fn)), group=-1)

    game_cmd(r"^#(سفید|قرمز)\s+", start_dart)
    game_cmd(r"^#(زوج|فرد)\s+", start_dice)
    game_cmd(r"^#پوپ(?:\u200c)?\s+", start_poop)
    game_cmd(r"^#فوتبال(?:\u200c)?$", start_football)
    app.add_handler(MessageHandler(filters.Dice.ALL, handle_dice_or_dart), group=-1)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_quiz_answer), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, finance_and_gift_text), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_text), group=2)

    app.add_handler(CallbackQueryHandler(admin_router, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(finance_callback, pattern=r"^fin:"))
    app.add_handler(CallbackQueryHandler(ub_callback, pattern=r"^ub:"))
    app.add_handler(CallbackQueryHandler(check_join, pattern=r"^check_join$"))
    app.add_handler(CallbackQueryHandler(cancel_dice, pattern=r"^cancel_dice:"))
    app.add_handler(CallbackQueryHandler(poop_callback, pattern=r"^poop_"))
    app.add_handler(CallbackQueryHandler(dyn_router, pattern=r"^dyn[jcxn]"))
    app.add_handler(CallbackQueryHandler(ui_callback, pattern=r"^ui:"))

    app.add_error_handler(on_error)
    return app


async def health(request: web.Request) -> web.Response:
    return web.Response(text="MEOWBET is running")


async def keepalive_loop() -> None:
    """Render's free web services sleep after ~15 min without inbound traffic.
    Pinging our own public URL every 10 min keeps the polling bot awake.
    (For something sturdier, also add an UptimeRobot monitor on /health, or use a paid plan.)"""
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url or os.getenv("KEEPALIVE", "1") != "1":
        return
    async with ClientSession(timeout=ClientTimeout(total=20)) as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(url.rstrip("/") + "/health") as r:
                    await r.read()
            except Exception as e:
                logger.warning("keepalive ping failed: %s", e)


async def amain() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Open the HTTP port first so Render's health check passes while the DB connects.
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("HTTP server listening on %s", PORT)

    await init_pool()
    await init_db()
    await load_settings()
    await load_quiz_cache()
    load_premium_emoji()
    try:
        await userbot.start_from_db()
    except Exception:
        logger.exception("userbot startup failed (bot keeps running)")
    if not payment_card():
        logger.warning("PAYMENT_CARD (Meowie-bank account) is empty - deposits stay disabled until you set it (env or admin panel).")

    app = build_application()
    await app.initialize()
    set_bot_username(app.bot.username or "")
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    tasks = [asyncio.create_task(c) for c in (
        janitor_loop(app.bot), quiz_loop(app.bot), notice_loop(app.bot), keepalive_loop())]
    logger.info("MEOWBET v7 started")

    await stop.wait()
    logger.info("shutting down...")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await runner.cleanup()
    await userbot.stop(logout=False)
    await pool.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

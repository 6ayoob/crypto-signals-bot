# bot.py — V2.2 with Referral System, Marketing Copy v2, and safer flows
# Compatible with aiogram v3.x
# Split-friendly (with database.py providing referral helpers)
#
# Key additions:
# - Deep-link referral: /start ref_<CODE>  (auto-link on first run)
# - /ref command: shows personal link + basic stats
# - Admin approval path auto-applies +2 days bonus on the first paid activation for referred users
# - Optional manual linking: /use_ref <CODE>
# - Improved welcome & signal copy, micro-CTAs, and gentle nudges
# - Defensive imports for referral helpers (no-crash if database.py is old)
#
# ENV (optional):
#   REF_BONUS_DAYS=2
#   BRAND_NAME="عالم الفرص"
#   BOT_USERNAME_OVERRIDE="YourBotUsername"  # fallback if get_me fails
#   FEATURE_TAGLINE="3 مستويات أهداف، وقف R-based، تقرير يومي"
#   START_BANNER_EMOJI="🚀"
#   SHOW_REF_IN_START=1                        # add "دعوة صديق" button in /start
#   OKX_PUBLIC_RATE_MAX / OKX_PUBLIC_RATE_WINDOW as before
#
# NOTE: Requires database.py >= 2.0 (with referral helpers).
import asyncio
import json
import hashlib
import logging
import os
import sys
import time
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, List
from collections import deque
import random

import ccxt
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ====== Single-instance local lock ======
LOCKFILE_PATH = os.getenv("BOT_INSTANCE_LOCK") or ("/tmp/mk1_ai_bot.lock" if os.name != "nt" else "mk1_ai_bot.lock")
_LOCK_FP = None

def _acquire_single_instance_lock():
    global _LOCK_FP
    try:
        _LOCK_FP = open(LOCKFILE_PATH, "w")
        _LOCK_FP.write(str(os.getpid())); _LOCK_FP.flush()
        if os.name == "nt":
            import msvcrt; msvcrt.locking(_LOCK_FP.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(_LOCK_FP, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        print(f"Another bot instance is already running (lock: {LOCKFILE_PATH}). Exiting.")
        try: _LOCK_FP and _LOCK_FP.close()
        except Exception: pass
        sys.exit(1)
_acquire_single_instance_lock()

# ============================================
# Telegram Conflict (older aiogram variants)
try:
    from aiogram.exceptions import TelegramConflictError
except Exception:
    class TelegramConflictError(Exception): ...

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD,
    SUB_DURATION_2W, SUB_DURATION_4W,
    USDT_TRC20_WALLET
)

# Database
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, close_trade, add_trade_sig,
    has_open_trade_on_symbol, get_stats_24h, get_stats_7d,
    User, Trade
)

# Optional referral helpers (defensive import)
try:
    from database import (
        ensure_referral_code, link_referred_by, apply_referral_bonus_if_eligible,
        get_ref_stats, list_active_user_ids as db_list_active_uids
    )
    REFERRAL_ENABLED = True
except Exception:
    REFERRAL_ENABLED = False
    def ensure_referral_code(s, uid: int) -> str: return ""
    def link_referred_by(s, uid: int, code: str) -> tuple[bool, str]: return (False, "unsupported")
    def apply_referral_bonus_if_eligible(s, uid: int, bonus_days: int = 2):
        return False
    def get_ref_stats(s, uid: int) -> dict:
        return {"referred_count": 0, "paid_count": 0, "total_bonus_days": 0}
    def db_list_active_uids(s): return []

# ---------- Leader Lock ----------
ENABLE_DB_LOCK = os.getenv("ENABLE_DB_LOCK", "1") != "0"
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL = int(os.getenv("LEADER_TTL", "300"))  # seconds

acquire_or_steal_leader_lock = heartbeat_leader_lock = release_leader_lock = None
if ENABLE_DB_LOCK:
    try:
        from database import acquire_or_steal_leader_lock as _acq
        from database import heartbeat_leader_lock as _hb
        from database import release_leader_lock as _rel
        acquire_or_steal_leader_lock, heartbeat_leader_lock, release_leader_lock = _acq, _hb, _rel
    except Exception:
        try:
            from database import try_acquire_leader_lock as _try_acq
            def acquire_or_steal_leader_lock(name, holder, ttl_seconds=300):
                return _try_acq(name, holder)
            def heartbeat_leader_lock(name, holder):
                return True
            def release_leader_lock(name, holder):
                pass
        except Exception:
            ENABLE_DB_LOCK = False

# Strategy & Symbols
from strategy import check_signal  # NOTE: your new stronger strategy
from symbols import SYMBOLS

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ---------------------------
# Globals & Config
# ---------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Branding & copy
BRAND_NAME = os.getenv("BRAND_NAME", "عالم الفرص")
FEATURE_TAGLINE = os.getenv("FEATURE_TAGLINE", "3 مستويات أهداف، وقف R-based، تقرير يومي")
START_BANNER_EMOJI = os.getenv("START_BANNER_EMOJI", "🚀")
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "2"))
SHOW_REF_IN_START = os.getenv("SHOW_REF_IN_START", "1") == "1"

# OKX
exchange = ccxt.okx({"enableRateLimit": True})
AVAILABLE_SYMBOLS: List[str] = []

# Rate limiter for OKX public
OKX_PUBLIC_MAX = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))
OKX_PUBLIC_WIN = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))

class SlidingRateLimiter:
    def __init__(self, max_calls: int, window_sec: float):
        self.max_calls = max_calls; self.window = window_sec
        self.calls = deque(); self._lock = asyncio.Lock()
    async def wait(self):
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                while self.calls and (now - self.calls[0]) > self.window:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now); return
                sleep_for = self.window - (now - self.calls[0]) + 0.05
            await asyncio.sleep(max(sleep_for, 0.05))

RATE = SlidingRateLimiter(OKX_PUBLIC_MAX, OKX_PUBLIC_WIN)

# Scan/Monitor intervals
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))
MONITOR_INTERVAL_SEC = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")

SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))

# Risk V2
RISK_STATE_FILE = Path("risk_state.json")
MAX_DAILY_LOSS_R = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "6"))

AUDIT_IDS: Dict[int, str] = {}

# Dedupe window for signals
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))
_last_signal_at: Dict[str, float] = {}

# Messages cache per trade
MESSAGES_CACHE: Dict[int, Dict[str, str]] = {}
HIT_TP1: Dict[int, bool] = {}

# Support DM
SUPPORT_CHAT_ID: Optional[int] = int(os.getenv("SUPPORT_CHAT_ID")) if os.getenv("SUPPORT_CHAT_ID") else None
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME")

# Channel invite links
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")  # static fallback

# Trial/reminders
TRIAL_INVITE_HOURS = int(os.getenv("TRIAL_INVITE_HOURS", "24"))
KICK_CHECK_INTERVAL_SEC = int(os.getenv("KICK_CHECK_INTERVAL_SEC", "3600"))
REMINDER_BEFORE_HOURS = int(os.getenv("REMINDER_BEFORE_HOURS", "4"))
_SENT_SOON_REM: set[int] = set()

# Gift 1-day (admin only)
GIFT_ONE_DAY_HOURS = int(os.getenv("GIFT_ONE_DAY_HOURS", "24"))

# Bot username cache
_BOT_USERNAME: Optional[str] = os.getenv("BOT_USERNAME_OVERRIDE") or None

# ===== Helpers =====

def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def _get_bot_username() -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    try:
        me = await bot.get_me()
        _BOT_USERNAME = me.username
    except Exception:
        _BOT_USERNAME = os.getenv("BOT_USERNAME_OVERRIDE") or "YourBot"
    return _BOT_USERNAME

def _make_audit_id(symbol: str, entry: float, score: int) -> str:
    base = f"{datetime.utcnow().strftime('%Y-%m-%d')}_{symbol}_{round(float(entry), 4)}_{int(score or 0)}"
    h = hashlib.md5(base.encode()).hexdigest()[:6]
    return f"{base}_{h}"

async def send_channel(text: str):
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def send_admins(text: str, reply_markup: InlineKeyboardMarkup | None = None):
    targets = list(ADMIN_USER_IDS)
    if SUPPORT_CHAT_ID:
        targets.append(SUPPORT_CHAT_ID)
    for admin_id in targets:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"ADMIN NOTIFY ERROR: {e}")

def list_active_user_ids() -> list[int]:
    # Prefer DB helper if available (faster)
    try:
        with get_session() as s:
            if REFERRAL_ENABLED:
                return db_list_active_uids(s)
            # fallback generic
            now = datetime.now(timezone.utc)
            rows = s.query(User.tg_user_id).filter(User.end_at != None, User.end_at > now).all()  # noqa
            return [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.warning(f"list_active_user_ids warn: {e}")
        return []

async def notify_subscribers(text: str):
    await send_channel(text)
    uids = list_active_user_ids()
    for uid in uids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.02)
        except Exception:
            pass

def _contact_line() -> str:
    parts = []
    if SUPPORT_USERNAME:
        parts.append(f"🔗 <a href='https://t.me/{SUPPORT_USERNAME}'>مراسلة الأدمن (خاص)</a>")
    if SUPPORT_CHAT_ID:
        parts.append(f"🆔 معرّف الأدمن: <code>{SUPPORT_CHAT_ID}</code>")
    if SUPPORT_CHAT_ID and not SUPPORT_USERNAME:
        parts.append(f"⚡️ افتح الخاص: <a href='tg://user?id={SUPPORT_CHAT_ID}'>اضغط هنا</a>")
    
    return "\n".join(parts) if parts else "—"

# ===== Referrals =====

async def _build_ref_link(uid: int, session) -> tuple[str, str]:
    """Returns (code, link)"""
    code = ensure_referral_code(session, uid) if REFERRAL_ENABLED else ""
    uname = await _get_bot_username()
    link = f"https://t.me/{uname}?start=ref_{code}" if code else ""
    return code, link

def _parse_start_payload(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if payload.lower().startswith("ref_"):
        return payload[4:].strip()
    return payload if payload else None

def invite_kb(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 ادخل القناة الآن", url=url)
    return kb.as_markup()

def support_dm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        kb.button(text="💬 مراسلة الأدمن (خاص)", url=f"https://t.me/{SUPPORT_USERNAME}")
    elif SUPPORT_CHAT_ID:
        kb.button(text="💬 مراسلة الأدمن (خاص)", url=f"tg://user?id={SUPPORT_CHAT_ID}")
    return kb.as_markup()

async def welcome_text(user_id: Optional[int] = None) -> str:
    price_line = ""
    try:
        price_line = f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b> | • 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
    except Exception:
        pass

    wallet_line = ""
    try:
        if USDT_TRC20_WALLET:
            wallet_line = f"💳 USDT (TRC20): <code>{_h(USDT_TRC20_WALLET)}</code>\n"
    except Exception:
        pass

    ref_hint = ""
    if REFERRAL_ENABLED and SHOW_REF_IN_START and user_id:
        try:
            with get_session() as s:
                code, link = await _build_ref_link(user_id, s)
                ref_hint = (
                    f"\n🎁 <b>برنامج الإحالة:</b> شارك رابطك واحصل على <b>{REF_BONUS_DAYS} يوم</b> هدية عند أول اشتراك مدفوع لصديقك.\n"
                    f"رابطك: <a href='{link}'>اضغط هنا</a>\n"
                )
        except Exception:
            pass

    tagline = os.getenv("FEATURE_TAGLINE", FEATURE_TAGLINE)

    return (
        f"{START_BANNER_EMOJI} أهلاً بك في <b>{BRAND_NAME}</b>\n"
        f"✨ {tagline}\n\n"
        "🔔 إشارات لحظية بتصفية صارمة + إدارة مخاطرة R-based + تقرير أداء يومي.\n"
        f"🕘 التقرير اليومي: <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)\n\n"
        "الخطط:\n"
        f"{price_line}"
        "للاشتراك: اضغط <b>«🔑 طلب اشتراك»</b> وسيقوم الأدمن بالتفعيل.\n"
        f"✨ جرّب الإصدار الكامل مجانًا لمدة <b>يوم واحد</b>.\n\n"
        f"{wallet_line}"
        f"{ref_hint}"
        "📞 تواصل مباشر:\n" + _contact_line()
    )

# ===== Signal / close message formatting (with subtle improvements) =====

def format_signal_text_basic(sig: dict) -> str:
    extra = ""
    if "score" in sig or "regime" in sig:
        extra += (
            f"\n📊 Score: <b>{sig.get('score','-')}</b> | Regime: <b>{_h(sig.get('regime','-'))}</b>"
        )
        if sig.get("reasons"):
            extra += f"\n🧠 كونفلوينس: <i>{_h(', '.join(sig['reasons'][:6]))}</i>"

    tp3_line = f"\n🏁 الهدف 3: <code>{sig.get('tp3')}</code>" if sig.get("tp3") is not None else ""
    strat_line = (
        f"\n🧭 النمط: <b>{_h(sig.get('strategy_code','-'))}</b> | ملف: <i>{_h(sig.get('profile','-'))}</i>"
        if sig.get("strategy_code") else ""
    )

    return (
        "🚀 <b>إشارة شراء</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(sig['symbol'])}</b>\n"
        f"💵 الدخول: <code>{sig['entry']}</code>\n"
        f"📉 الوقف: <code>{sig['sl']}</code>\n"
        f"🎯 الهدف 1: <code>{sig['tp1']}</code>\n"
        f"🏁 الهدف 2: <code>{sig['tp2']}</code>"
        f"{tp3_line}{strat_line}\n"
        f"⏰ (UTC): <code>{_h(sig['timestamp'])}</code>"
        f"{extra}\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ <i>قاعدة المخاطرة: أقصى 1% لكل صفقة، وبدون مطاردة للسعر.</i>"
    )


def format_close_text(t: Trade, r_multiple: float | None = None) -> str:
    emoji = {"tp1": "🎯", "tp2": "🏆", "sl": "🛑"}.get(getattr(t, "result", "") or "", "ℹ️")
    result_label = {
        "tp1": "تحقق الهدف 1 — خطوة ممتازة!",
        "tp2": "تحقق الهدف 2 — إنجاز رائع!",
        "sl": "وقف الخسارة — حماية رأس المال",
    }.get(getattr(t, "result", "") or "", "إغلاق")

    r_line = f"\n📐 R: <b>{round(r_multiple, 3)}</b>" if r_multiple is not None else ""
    tip = (
        "🔁 نبحث عن فرصة أقوى تالية… الصبر مكسب."
        if getattr(t, "result", "") == "sl"
        else "🎯 إدارة الربح أهم من كثرة الصفقات."
    )

    return (
        f"{emoji} <b>حالة صفقة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(str(t.symbol))}</b>\n"
        f"💵 الدخول: <code>{t.entry}</code>\n"
        f"📉 الوقف: <code>{t.sl}</code>\n"
        f"🎯 TP1: <code>{t.tp1}</code> | 🏁 TP2: <code>{t.tp2}</code>\n"
        f"📌 الحالة: <b>{result_label}</b>{r_line}\n"
        f"⏰ (UTC): <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{tip}"
    )

# ---------------------------
# Risk State helpers
# ---------------------------

def _load_risk_state() -> dict:
    try:
        if RISK_STATE_FILE.exists():
            return json.loads(RISK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"RISK_STATE load warn: {e}")
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "r_today": 0.0,
        "loss_streak": 0,
        "cooldown_until": None
    }


def _save_risk_state(state: dict):
    try:
        RISK_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"RISK_STATE save warn: {e}")


def _reset_if_new_day(state: dict) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("date") != today:
        state.update({
            "date": today,
            "r_today": 0.0,
            "loss_streak": 0,
            "cooldown_until": None
        })
    return state


def can_open_new_trade(s) -> Tuple[bool, str]:
    state = _reset_if_new_day(_load_risk_state())
    if state.get("cooldown_until"):
        try:
            until = datetime.fromisoformat(state["cooldown_until"])
            if datetime.now(timezone.utc) < until:
                return False, f"Cooldown حتى {until.isoformat()}"
        except Exception:
            pass
    if float(state.get("r_today", 0.0)) <= -MAX_DAILY_LOSS_R:
        return False, f"بلوغ حد الخسارة اليومي −{MAX_DAILY_LOSS_R}R"
    if count_open_trades(s) >= MAX_OPEN_TRADES:
        return False, "بلوغ حد الصفقات المفتوحة"
    return True, "OK"


def on_trade_closed_update_risk(t: Trade, result: str, exit_price: float) -> float:
    try:
        R = float(t.entry) - float(t.sl)
        r_multiple = 0.0 if R <= 0 else (float(exit_price) - float(t.entry)) / R
    except Exception:
        r_multiple = 0.0

    state = _reset_if_new_day(_load_risk_state())
    state["r_today"] = round(float(state.get("r_today", 0.0)) + r_multiple, 6)
    state["loss_streak"] = int(state.get("loss_streak", 0)) + 1 if r_multiple < 0 else 0

    cooldown_reason = None
    if float(state["r_today"]) <= -MAX_DAILY_LOSS_R:
        cooldown_reason = f"حد الخسارة اليومي −{MAX_DAILY_LOSS_R}R"
    if int(state["loss_streak"]) >= MAX_LOSSES_STREAK:
        if cooldown_reason:
            cooldown_reason += f" + {MAX_LOSSES_STREAK} خسائر متتالية"
        else:
            cooldown_reason = f"{MAX_LOSSES_STREAK} خسائر متتالية"

    if cooldown_reason:
        until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
        state["cooldown_until"] = until.isoformat()
        _save_risk_state(state)
        asyncio.create_task(send_channel(
            f"⏸️ <b>إيقاف مؤقت لفتح صفقات جديدة</b>\n"
            f"السبب: {cooldown_reason}\n"
            f"حتى: <code>{until.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
            "💡 <i>نحافظ على الذخيرة لفرص أعلى جودة.</i>"
        ))
        asyncio.create_task(send_admins(
            f"⚠️ Cooldown مُفعل — {cooldown_reason}. حتى {until.isoformat()}"
        ))
    else:
        _save_risk_state(state)

    return r_multiple

# ---------------------------
# OKX
# ---------------------------

async def load_okx_markets_and_filter():
    global AVAILABLE_SYMBOLS
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, exchange.load_markets)
        all_markets = set(exchange.markets.keys())
        filtered = [s for s in SYMBOLS if s in all_markets]
        skipped = [s for s in SYMBOLS if s not in all_markets]
        AVAILABLE_SYMBOLS = filtered
        logger.info(f"✅ OKX: Loaded {len(filtered)} symbols. Skipped {len(skipped)}: {skipped}")
    except Exception as e:
        logger.exception(f"❌ load_okx_markets error: {e}")
        AVAILABLE_SYMBOLS = []

# ---------------------------
# Data fetchers
# ---------------------------

async def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=300) -> list:
    for attempt in range(4):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            )
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.6 * (attempt + 1) + random.uniform(0.1, 0.4))
        except Exception as e:
            logger.warning(f"❌ FETCH_OHLCV ERROR [{symbol}]: {e}")
            return []
    return []

async def fetch_ticker_price(symbol: str) -> Optional[float]:
    for attempt in range(3):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, lambda: exchange.fetch_ticker(symbol))
            price = ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("last")
            return float(price) if price is not None else None
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.5 * (attempt + 1) + random.uniform(0.05, 0.2))
        except Exception as e:
            logger.warning(f"❌ FETCH_TICKER ERROR [{symbol}]: {e}")
            return None
    return None
# ---------------------------
# Dedupe signals
# ---------------------------

def _should_skip_duplicate(sig: dict) -> bool:
    sym = sig.get("symbol")
    if not sym:
        return False
    now = time.time()
    last_ts = _last_signal_at.get(sym, 0)
    if now - last_ts < DEDUPE_WINDOW_MIN * 60:
        return True
    _last_signal_at[sym] = now
    return False

# ---------------------------
# Scan & Dispatch
# ---------------------------

SCAN_LOCK = asyncio.Lock()

async def _send_signal_to_channel(sig: dict, audit_id: Optional[str]) -> None:
    await send_channel(format_signal_text_basic(sig))

async def _scan_one_symbol(sym: str) -> Optional[dict]:
    data = await fetch_ohlcv(sym)
    if not data:
        return None
    sig = check_signal(sym, data)
    return sig if sig else None

async def scan_and_dispatch():
    if not AVAILABLE_SYMBOLS:
        return

    async with SCAN_LOCK:
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _guarded_scan(sym: str) -> Optional[dict]:
            async with sem:
                try:
                    return await _scan_one_symbol(sym)
                except Exception as e:
                    logger.warning(f"⚠️ Scan error [{sym}]: {e}")
                    return None

        for i in range(0, len(AVAILABLE_SYMBOLS), SCAN_BATCH_SIZE):
            batch = AVAILABLE_SYMBOLS[i:i + SCAN_BATCH_SIZE]
            sigs = await asyncio.gather(*[_guarded_scan(s) for s in batch])

            for sig in filter(None, sigs):
                if _should_skip_duplicate(sig):
                    logger.info(f"⏱️ DEDUPE SKIP {sig['symbol']}")
                    continue

                with get_session() as s:
                    allowed, reason = can_open_new_trade(s)
                    if not allowed:
                        logger.info(f"❌ SKIP SIGNAL {sig['symbol']}: {reason}")
                        continue

                    if has_open_trade_on_symbol(s, sig["symbol"]):
                        logger.info(f"🔁 SKIP {sig['symbol']}: already open")
                        continue

                    audit_id = _make_audit_id(sig["symbol"], sig["entry"], sig.get("score", 0))

                    try:
                        trade_id = add_trade_sig(s, sig, audit_id=audit_id, qty=None)
                    except Exception as e:
                        logger.exception(f"⚠️ add_trade_sig failed, fallback: {e}")
                        trade_id = add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

                    AUDIT_IDS[trade_id] = audit_id

                    if sig.get("messages"):
                        try:
                            MESSAGES_CACHE[trade_id] = dict(sig["messages"])
                        except Exception:
                            pass

                    try:
                        await _send_signal_to_channel(sig, audit_id)

                        entry_msg = (sig.get("messages") or {}).get("entry")
                        if entry_msg:
                            await notify_subscribers(entry_msg)

                        note = (
                            "🚀 <b>إشارة جديدة وصلت!</b>\n"
                            "🔔 الهدوء أفضل من مطاردة الشمعة — التزم بالخطة."
                        )
                        for uid in list_active_user_ids():
                            try:
                                await bot.send_message(uid, note, parse_mode="HTML", disable_web_page_preview=True)
                                await asyncio.sleep(0.02)
                            except Exception:
                                pass

                        logger.info(f"✅ SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']} audit={audit_id}")
                    except Exception as e:
                        logger.exception(f"❌ SEND SIGNAL ERROR: {e}")

            await asyncio.sleep(0.1)

async def loop_signals():
    while True:
        started = time.time()
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"🔥 SCAN_LOOP ERROR: {e}")
        elapsed = time.time() - started
        await asyncio.sleep(max(1.0, SIGNAL_SCAN_INTERVAL_SEC - elapsed))
# ---------------------------
# Monitor open trades
# ---------------------------

async def monitor_open_trades():
    from types import SimpleNamespace
    while True:
        try:
            with get_session() as s:
                open_trades = s.query(Trade).filter(Trade.status == "open").all()
                for t in open_trades:
                    price = await fetch_ticker_price(t.symbol)
                    if price is None:
                        continue

                    hit_tp2 = price >= t.tp2
                    hit_tp1 = price >= t.tp1
                    hit_sl = price <= t.sl

                    result, exit_px = None, None
                    if hit_sl:
                        result, exit_px = "sl", float(t.sl)
                    elif hit_tp2:
                        result, exit_px = "tp2", float(t.tp2)

                    if result:
                        r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                        try:
                            close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                        except Exception as e:
                            logger.warning(f"⚠️ close_trade warn: {e}")

                        msg = format_close_text(t, r_multiple)
                        extra = (MESSAGES_CACHE.get(t.id, {}) or {}).get(result)
                        if extra:
                            msg += "\n\n" + extra

                        await notify_subscribers(msg)
                        await asyncio.sleep(0.05)
                        continue

                    # Hit TP1 but not yet TP2
                    if hit_tp1 and not HIT_TP1.get(t.id):
                        HIT_TP1[t.id] = True
                        tmp = SimpleNamespace(symbol=t.symbol, entry=t.entry, sl=t.sl, tp1=t.tp1, tp2=t.tp2, result="tp1")
                        msg = format_close_text(tmp, None)

                        extra = (MESSAGES_CACHE.get(t.id, {}) or {}).get("tp1")
                        if extra:
                            msg += "\n\n" + extra
                        msg += "\n\n🔒 اقتراح: انقل وقفك لنقطة الدخول لحماية الربح."

                        await notify_subscribers(msg)
                        await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"MONITOR ERROR: {e}")
        await asyncio.sleep(MONITOR_INTERVAL_SEC)

# ---------------------------
# Membership housekeeping
# ---------------------------

async def kick_expired_members_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            with get_session() as s:
                expired = s.query(User).filter(User.end_at != None, User.end_at <= now).all()
                for u in expired:
                    try:
                        member = await bot.get_chat_member(TELEGRAM_CHANNEL_ID, u.tg_user_id)
                        status = getattr(member, "status", None)
                        if status in ("member", "administrator", "creator"):
                            await bot.ban_chat_member(TELEGRAM_CHANNEL_ID, u.tg_user_id)
                            await asyncio.sleep(0.3)
                            await bot.unban_chat_member(TELEGRAM_CHANNEL_ID, u.tg_user_id)

                        try:
                            await bot.send_message(
                                u.tg_user_id,
                                "⏳ انتهت صلاحية الوصول.\n"
                                "✨ فعّل اشتراكك الآن للاستمرار باستلام الإشارات والتقرير اليومي.\n"
                                "استخدم /start لطلب التفعيل أو مراسلة الأدمن.",
                            )
                        except Exception:
                            pass

                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.debug(f"kick_expired: {e}")
        except Exception as e:
            logger.exception(f"KICK_EXPIRED_LOOP ERROR: {e}")
        await asyncio.sleep(max(60, KICK_CHECK_INTERVAL_SEC))

async def notify_trial_expiring_soon_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            soon = now + timedelta(hours=REMINDER_BEFORE_HOURS)
            with get_session() as s:
                rows = s.query(User).filter(User.end_at != None, User.end_at > now, User.end_at <= soon).all()
                for u in rows:
                    if u.tg_user_id in _SENT_SOON_REM:
                        continue
                    try:
                        left_min = max(1, int((u.end_at - now).total_seconds() // 60))
                        await bot.send_message(
                            u.tg_user_id,
                            f"⏰ تبقّى حوالي {left_min} دقيقة على نهاية صلاحيتك.\n"
                            "✅ فعّل اشتراكك الآن لتستمر الإشارات بدون انقطاع. استخدم /start.",
                            parse_mode="HTML",
                        )
                        _SENT_SOON_REM.add(u.tg_user_id)
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
        except Exception as e:
            logger.debug(f"notify_expiring_soon: {e}")
        await asyncio.sleep(900)

# ---------------------------
# Reports
# ---------------------------

def render_daily_report(stats: dict) -> str:
    total = stats.get("total", 0)
    win_rate = stats.get("win_rate", 0.0)
    best_symbol = stats.get("best_symbol", "-")
    best_gain = stats.get("best_gain", 0.0)
    worst_symbol = stats.get("worst_symbol", "-")
    worst_gain = stats.get("worst_gain", 0.0)

    msg = (
        "📊 <b>التقرير اليومي — لقطة أداء مركّزة</b>\n"
        f"• إجمالي الصفقات: <b>{total}</b>\n"
        f"• نسبة الربح: <b>{win_rate:.1f}%</b>\n"
        "—\n"
        f"🔹 أفضل رمز: <code>{best_symbol}</code> (+{best_gain:.2f}%)\n"
        f"🔸 أضعف رمز: <code>{worst_symbol}</code> ({worst_gain:.2f}%)\n"
    )
    return msg



async def daily_report_once():
    with get_session() as s:
        stats_24 = get_stats_24h(s)
        stats_7d = get_stats_7d(s)
        await send_channel(_report_card(stats_24, stats_7d))
        logger.info("Daily report sent.")


async def daily_report_loop():
    tz = pytz.timezone(TIMEZONE)
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info(f"Next daily report at {target.isoformat()} ({TIMEZONE})")
        await asyncio.sleep(delay)
        try:
            await daily_report_once()
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e} — retrying in 60s")
            await asyncio.sleep(60)
            try:
                await daily_report_once()
            except Exception as e2:
                logger.exception(f"DAILY_REPORT RETRY FAILED: {e2}")

# ---------------------------
# User Commands
# ---------------------------

@dp.message(Command("start"))
async def cmd_start(m: Message):
    # Try to link referral from deep link
    payload_code = _parse_start_payload(m.text)
    if REFERRAL_ENABLED and payload_code:
        try:
            with get_session() as s:
                linked, reason = link_referred_by(s, m.from_user.id, payload_code)
            if linked:
                await m.answer("🤝 تم تسجيل كود الإحالة بنجاح. عند أول تفعيل مدفوع ستحصل على هدية الأيام تلقائيًا 🎁", parse_mode="HTML")
            else:
                await m.answer(f"ℹ️ لم يتم ربط الإحالة: {reason}", parse_mode="HTML")
        except Exception as e:
            logger.warning(f"link_referred_by error: {e}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 طلب اشتراك", callback_data="req_sub")
    kb.button(text="✨ ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="🧾 حالة اشتراكي", callback_data="status_btn")
    if REFERRAL_ENABLED and SHOW_REF_IN_START:
        kb.button(text="🎁 رابط دعوة صديق", callback_data="show_ref_link")
    kb.adjust(1)
    await m.answer(await welcome_text(m.from_user.id), parse_mode="HTML", reply_markup=kb.as_markup())
    if SUPPORT_USERNAME or SUPPORT_CHAT_ID:
        await m.answer("تحتاج مساعدة؟ راسل الأدمن مباشرة:", reply_markup=support_dm_kb())


@dp.callback_query(F.data == "show_ref_link")
async def cb_show_ref(q: CallbackQuery):
    if not REFERRAL_ENABLED:
        await q.answer("برنامج الإحالة غير مفعل حاليًا.", show_alert=True); return
    try:
        with get_session() as s:
            code, link = await _build_ref_link(q.from_user.id, s)
        if not link:
            return await q.answer("تعذر إنشاء الرابط حالياً.", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.button(text="🔗 افتح رابط دعوتي", url=link)
        kb.adjust(1)
        await q.message.answer(
referral_msg = (
    "🤝 <b>برنامج الإحالة</b>\n"
    f"🎁 شارك رابطك واحصل على <b>{REF_BONUS_DAYS} يوم</b> هدية عند أول اشتراك مدفوع لصديقك.\n"
    "—\n"
    "1) انسخ رابط الدعوة الخاص بك من /ref\n"
    "2) أرسله لصديقك وسجِّل عبره\n"
    "3) بعد الدفع، تصلك الهدية تلقائيًا\n"
)

    except Exception as e:
        logger.warning(f"show_ref_link error: {e}")
        await q.answer("خطأ غير متوقع.", show_alert=True)


@dp.message(Command("ref"))
async def cmd_ref(m: Message):
    if not REFERRAL_ENABLED:
        return await m.answer("ℹ️ برنامج الإحالة غير مفعل حالياً.", parse_mode="HTML")
    with get_session() as s:
        code, link = await _build_ref_link(m.from_user.id, s)
        stats = {}
        try:
            stats = get_ref_stats(s, m.from_user.id) or {}
        except Exception:
            stats = {}
    txt = (
        "🎁 <b>برنامج الإحالة</b>
"
        f"• رابطك: <code>{link or '—'}</code>
"
        f"• مدعوون: <b>{stats.get('referred_count',0)}</b>
"
        f"• اشتركات مدفوعة عبرك: <b>{stats.get('paid_count',0)}</b>
"
        f"• أيام هدية: <b>{stats.get('total_bonus_days',0)}</b>
"
        "ارسل لأصدقائك هذا الرابط. عند أول اشتراك مدفوع لصديقك تحصلون على الهدية حسب سياسة البونص."
    )
    await m.answer(txt, parse_mode="HTML")


@dp.message(Command("use_ref"))
async def cmd_use_ref(m: Message):
    if not REFERRAL_ENABLED:
        return await m.answer("ℹ️ برنامج الإحالة غير مفعل حالياً.", parse_mode="HTML")
    parts = (m.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("استخدم: <code>/use_ref CODE</code>", parse_mode="HTML")
    code = parts[1].strip()
    with get_session() as s:
        linked, reason = link_referred_by(s, m.from_user.id, code)
    if linked:
        await m.answer("🤝 تم ربط الإحالة بنجاح. عند أول تفعيل مدفوع ستحصل على الهدية 🎁", parse_mode="HTML")
    else:
        await m.answer(f"ℹ️ لم يتم الربط: {reason}", parse_mode="HTML")


@dp.callback_query(F.data == "status_btn")
async def cb_status_btn(q: CallbackQuery):
    with get_session() as s:
        ok = is_active(s, q.from_user.id)
    await q.message.answer(
        "✅ <b>اشتراكك نشط.</b>
🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
        if ok else
        "❌ <b>لا تملك اشتراكًا نشطًا.</b>
✨ اطلب التفعيل وسيقوم الأدمن بإتمامه.",
        parse_mode="HTML"
    )
    await q.answer()


@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)
    if ok:
        await q.message.answer(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁
"
            "🚀 استمتع بالإشارات والتقرير اليومي.",
            parse_mode="HTML"
        )
        invite = await get_trial_invite_link(q.from_user.id)
        if invite:
            try:
                await bot.send_message(q.from_user.id, "📣 ادخل القناة الآن:", reply_markup=invite_kb(invite))
            except Exception as e:
                logger.warning(f"SEND INVITE(TRIAL) ERROR: {e}")
    else:
        await q.message.answer(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.
"
            "✨ يمكنك طلب الاشتراك الآن وسيقوم الأدمن بالتفعيل.",
            parse_mode="HTML"
        )
    await q.answer()


@dp.message(Command("trial"))
async def cmd_trial(m: Message):
    with get_session() as s:
        ok = start_trial(s, m.from_user.id)
    if ok:
        await m.answer("✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁", parse_mode="HTML")
        invite = await get_trial_invite_link(m.from_user.id)
        if invite:
            try:
                await bot.send_message(m.from_user.id, "📣 ادخل القناة الآن:", reply_markup=invite_kb(invite))
            except Exception as e:
                logger.warning(f"SEND INVITE(TRIAL CMD) ERROR: {e}")
    else:
        await m.answer("ℹ️ لقد استخدمت التجربة المجانية مسبقًا.", parse_mode="HTML")

# Invite link helpers

async def get_channel_invite_link() -> Optional[str]:
    if CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    try:
        inv = await bot.create_chat_invite_link(TELEGRAM_CHANNEL_ID, creates_join_request=False)
        return inv.invite_link
    except Exception as e:
        logger.warning(f"INVITE_LINK create failed: {e}")
        return None


async def get_trial_invite_link(user_id: int) -> Optional[str]:
    if CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    try:
        expires_at = datetime.utcnow() + timedelta(hours=TRIAL_INVITE_HOURS)
        inv = await bot.create_chat_invite_link(
            TELEGRAM_CHANNEL_ID,
            name=f"trial_{user_id}",
            expire_date=int(expires_at.replace(tzinfo=timezone.utc).timestamp()),
            member_limit=1,
            creates_join_request=False,
        )
        return inv.invite_link
    except Exception as e:
        logger.warning(f"INVITE_LINK(TRIAL) create failed: {e}")
        return None


async def get_paid_invite_link(user_id: int) -> Optional[str]:
    if CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    try:
        inv = await bot.create_chat_invite_link(
            TELEGRAM_CHANNEL_ID,
            name=f"paid_{user_id}",
            creates_join_request=False,
        )
        return inv.invite_link
    except Exception as e:
        logger.warning(f"INVITE_LINK(PAID) create failed: {e}")
        return None

# === User purchase request flow ===

@dp.callback_query(F.data == "req_sub")
async def cb_req_sub(q: CallbackQuery):
    u = q.from_user
    uid = u.id
    uname = (u.username and f"@{u.username}") or (u.full_name or "")
    user_line = f"{_h(uname)} (ID: <code>{uid}</code>)"

    kb_admin = InlineKeyboardBuilder()
    kb_admin.button(text="✅ تفعيل 2 أسابيع (2w)", callback_data=f"approve_inline:{uid}:2w")
    kb_admin.button(text="✅ تفعيل 4 أسابيع (4w)", callback_data=f"approve_inline:{uid}:4w")
    kb_admin.button(text="🎁 تفعيل يوم مجاني (gift1d)", callback_data=f"approve_inline:{uid}:gift1d")
    kb_admin.button(text="❌ رفض", callback_data=f"reject_inline:{uid}")
    kb_admin.button(text="👤 مراسلة المستخدم", url=f"tg://user?id={uid}")
    kb_admin.adjust(1)

    await send_admins(
        "🔔 <b>طلب اشتراك جديد</b>
" f"المستخدم: {user_line}
" "اختر نوع التفعيل:",
        reply_markup=kb_admin.as_markup(),
    )

    price_line = ""
    try:
        price_line = f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b> | • 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>
"
    except Exception:
        pass
    wallet_line = ""
    try:
        if USDT_TRC20_WALLET:
            wallet_line = f"💳 محفظة USDT (TRC20):
<code>{_h(USDT_TRC20_WALLET)}</code>

"
    except Exception:
        pass
    await q.message.answer(
        "📩 تم إرسال طلبك للأدمن.
"
        "يرجى التحويل ثم مراسلة الأدمن لتأكيد التفعيل.

"
        "الخطط:
"
        f"{price_line}"
        f"{wallet_line}"
        "بعد التأكيد ستستلم رابط الدخول للقناة تلقائيًا.",
        parse_mode="HTML",
        reply_markup=support_dm_kb() if (SUPPORT_USERNAME or SUPPORT_CHAT_ID) else None,
    )
    await q.answer()

# === Admin Approvals (inline) ===

def _bonus_applied_text(applied) -> str:
    return "
🎁 <i>تم إضافة هدية الإحالة (+{} يوم) تلقائيًا.</i>".format(REF_BONUS_DAYS) if applied else ""


@dp.callback_query(F.data.startswith("approve_inline:"))
async def cb_approve_inline(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    try:
        _, uid_str, plan = q.data.split(":")
        uid = int(uid_str)
        if plan not in ("2w", "4w", "gift1d"):
            return await q.answer("خطة غير صالحة.", show_alert=True)

        # duration
        if plan == "2w":
            dur = SUB_DURATION_2W
        elif plan == "4w":
            dur = SUB_DURATION_4W
        else:
            dur = timedelta(hours=GIFT_ONE_DAY_HOURS)

        with get_session() as s:
            end_at = approve_paid(s, uid, plan, dur, tx_hash=None)
            bonus_applied = False
            if REFERRAL_ENABLED and plan in ("2w","4w"):
                try:
                    res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                    bonus_applied = bool(res)
                except Exception as e:
                    logger.warning(f"apply_referral_bonus_if_eligible error: {e}")

        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
            f"
صالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>"
            f"{_bonus_applied_text(bonus_applied)}",
            parse_mode="HTML",
        )

        # Send invite
        invite = await get_paid_invite_link(uid)
        try:
            if invite:
                title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                msg = title
                if bonus_applied:
                    msg += f"

🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)."
                await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
            else:
                await bot.send_message(
                    uid,
                    "✅ تم التفعيل. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning(f"USER DM/INVITE ERROR: {e}")
        await q.answer("تم.")
    except Exception as e:
        logger.exception(f"APPROVE_INLINE ERROR: {e}")
        await q.answer("خطأ أثناء التفعيل.", show_alert=True)


@dp.callback_query(F.data.startswith("reject_inline:"))
async def cb_reject_inline(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    try:
        _, uid_str = q.data.split(":")
        uid = int(uid_str)
        await q.message.answer(f"❌ تم رفض طلب الاشتراك للمستخدم <code>{uid}</code>.", parse_mode="HTML")
        try:
            await bot.send_message(uid, "ℹ️ تم رفض طلب الاشتراك. تواصل مع الأدمن للتفاصيل.", parse_mode="HTML")
        except Exception:
            pass
        await q.answer("تم.")
    except Exception as e:
        logger.exception(f"REJECT_INLINE ERROR: {e}")
        await q.answer("خطأ أثناء الرفض.", show_alert=True)

# ---------------------------
# General user commands
# ---------------------------

@dp.message(Command("help"))
async def cmd_help(m: Message):
    txt = (
        "🤖 <b>أوامر المستخدم</b>
"
        "• <code>/start</code> – البداية والقائمة الرئيسية
"
        "• <code>/trial</code> – تجربة مجانية ليوم
"
        "• <code>/status</code> – حالة الاشتراك
"
        "• <code>/ref</code> – رابط الإحالة وإحصاءاتك
"
        "• <code>/use_ref CODE</code> – ربط كود إحالة يدويًا
"
        "• (زر) 🔑 طلب اشتراك — لإرسال طلب للأدمن

"
        "📞 <b>تواصل خاص مع الأدمن</b>:
" + _contact_line()
    )
    await m.answer(txt, parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer(
        "✅ <b>اشتراكك نشط.</b>
🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
        if ok else
        "❌ <b>لا تملك اشتراكًا نشطًا.</b>
✨ اطلب التفعيل وسيقوم الأدمن بإتمامه.",
        parse_mode="HTML",
    )

# ---------------------------
# Admin commands
# ---------------------------

@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    txt = (
        "🛠️ <b>أوامر الأدمن</b>
"
        "• <code>/admin</code> – لوحة الأزرار
"
        "• <code>/approve &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [reference]</code>
"
        "• <code>/activate &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [reference]</code>
"
        "• <code>/broadcast &lt;text&gt;</code> – رسالة جماعية
"
        "• <code>/force_report</code> – إرسال التقرير اليومي الآن
"
        "• <code>/gift1d &lt;user_id&gt;</code> – تفعيل يوم مجاني فوري
"
        "• <code>/refstats &lt;user_id&gt;</code> – إحصاءات الإحالة للمستخدم
"
    )
    await m.answer(txt, parse_mode="HTML")


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ تفعيل اشتراك يدوي (إدخال user_id)", callback_data="admin_manual")
    kb.button(text="ℹ️ أوامر الأدمن", callback_data="admin_help_btn")
    kb.adjust(1)
    await m.answer("لوحة الأدمن:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "admin_help_btn")
async def cb_admin_help_btn(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer()
    await cmd_admin_help(q.message)
    await q.answer()


@dp.callback_query(F.data == "admin_manual")
async def cb_admin_manual(q: CallbackQuery):
    aid = q.from_user.id
    if aid not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    ADMIN_FLOW[aid] = {"stage": "await_user"}
    await q.message.answer("أرسل الآن <code>user_id</code> للمستخدم الذي تريد تفعيله:", parse_mode="HTML")
    await q.answer()


ADMIN_FLOW: Dict[int, Dict[str, Any]] = {}


@dp.message(F.text)
async def admin_manual_router(m: Message):
    aid = m.from_user.id
    flow = ADMIN_FLOW.get(aid)
    if not flow or aid not in ADMIN_USER_IDS:
        return
    stage = flow.get("stage")

    if stage == "await_user":
        try:
            uid = int(m.text.strip())
            flow["uid"] = uid
            flow["stage"] = "await_plan"
            kb = InlineKeyboardBuilder()
            kb.button(text="تفعيل 2 أسابيع (2w)", callback_data="admin_plan:2w")
            kb.button(text="تفعيل 4 أسابيع (4w)", callback_data="admin_plan:4w")
            kb.button(text="🎁 تفعيل يوم مجاني (gift1d)", callback_data="admin_plan:gift1d")
            kb.button(text="إلغاء", callback_data="admin_cancel")
            kb.adjust(1)
            await m.answer(f"تم استلام user_id: <code>{uid}</code>
اختر الخطة:", parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await m.answer("الرجاء إرسال رقم user_id صحيح (أرقام فقط).")
        return

    if stage == "await_ref":
        ref = m.text.strip()
        if ref.lower() in ("/skip", "skip", "تخطي", "تخطى"):
            ref = None
        uid = flow.get("uid"); plan = flow.get("plan")
        if plan == "2w":
            dur = SUB_DURATION_2W
        elif plan == "4w":
            dur = SUB_DURATION_4W
        else:
            dur = timedelta(hours=GIFT_ONE_DAY_HOURS)
        try:
            with get_session() as s:
                end_at = approve_paid(s, uid, plan, dur, tx_hash=ref)
                bonus_applied = False
                if REFERRAL_ENABLED and plan in ("2w","4w"):
                    try:
                        res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                        bonus_applied = bool(res)
                    except Exception as e:
                        logger.warning(f"apply_referral_bonus_if_eligible error: {e}")
            ADMIN_FLOW.pop(aid, None)
            extra = f"{_bonus_applied_text(bonus_applied)}"
            await m.answer(
                f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
                f"
صالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>{extra}",
                parse_mode="HTML",
            )
            invite = await get_paid_invite_link(uid)
            try:
                if invite:
                    title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                    msg = title + (f"

🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)." if bonus_applied else "")
                    await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
                else:
                    await bot.send_message(uid, "✅ تم التفعيل. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.", parse_mode="HTML")
            except Exception as e:
                logger.warning(f"USER DM ERROR: {e}")
        except Exception as e:
            ADMIN_FLOW.pop(aid, None)
            await m.answer(f"❌ فشل التفعيل: {e}")
        return


@dp.callback_query(F.data.startswith("admin_plan:"))
async def cb_admin_plan(q: CallbackQuery):
    aid = q.from_user.id
    if aid not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    flow = ADMIN_FLOW.get(aid)
    if not flow or flow.get("stage") != "await_plan":
        return await q.answer("انتهت الجلسة أو غير صالحة.", show_alert=True)
    plan = q.data.split(":", 1)[1]
    if plan not in ("2w", "4w", "gift1d"):
        return await q.answer("خطة غير صالحة.", show_alert=True)
    flow["plan"] = plan
    flow["stage"] = "await_ref"
    kb = InlineKeyboardBuilder()
    kb.button(text="تخطي المرجع", callback_data="admin_skip_ref")
    kb.button(text="إلغاء", callback_data="admin_cancel")
    kb.adjust(1)
    await q.message.answer(
        "أرسل رقم مرجع (اختياري) لإرفاقه بالإيصال.
أو اضغط «تخطي المرجع».",
        reply_markup=kb.as_markup(),
    )
    await q.answer()


@dp.callback_query(F.data == "admin_skip_ref")
async def cb_admin_skip_ref(q: CallbackQuery):
    aid = q.from_user.id
    if aid not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    flow = ADMIN_FLOW.get(aid)
    if not flow or flow.get("stage") != "await_ref":
        return await q.answer("انتهت الجلسة أو غير صالحة.", show_alert=True)
    uid = flow.get("uid"); plan = flow.get("plan")
    if plan == "2w":
        dur = SUB_DURATION_2W
    elif plan == "4w":
        dur = SUB_DURATION_4W
    else:
        dur = timedelta(hours=GIFT_ONE_DAY_HOURS)
    try:
        with get_session() as s:
            end_at = approve_paid(s, uid, plan, dur, tx_hash=None)
            bonus_applied = False
            if REFERRAL_ENABLED and plan in ("2w","4w"):
                try:
                    res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                    bonus_applied = bool(res)
                except Exception as e:
                    logger.warning(f"apply_referral_bonus_if_eligible error: {e}")
        ADMIN_FLOW.pop(aid, None)
        extra = f"{_bonus_applied_text(bonus_applied)}"
        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
            f"
صالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>{extra}",
            parse_mode="HTML",
        )
        invite = await get_paid_invite_link(uid)
        try:
            if invite:
                title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                msg = title + (f"

🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)." if bonus_applied else "")
                await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
            else:
                await bot.send_message(uid, "✅ تم التفعيل. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.", parse_mode="HTML")
        except Exception as e:
            logger.warning(f"USER DM ERROR: {e}")
    except Exception as e:
        ADMIN_FLOW.pop(aid, None)
        await q.message.answer(f"❌ فشل التفعيل: {e}")
    await q.answer("تم.")


@dp.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(q: CallbackQuery):
    aid = q.from_user.id
    if aid in ADMIN_FLOW:
        ADMIN_FLOW.pop(aid, None)
    await q.message.answer("تم إلغاء جلسة التفعيل اليدوي.")
    await q.answer("تم.")


@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = (m.text or "").strip().split()
    if len(parts) not in (3, 4) or parts[2] not in ("2w", "4w", "gift1d"):
        return await m.answer("استخدم: /approve <user_id> <2w|4w|gift1d> [reference]")
    uid = int(parts[1]); plan = parts[2]
    txh = parts[3] if len(parts) == 4 else None
    if plan == "2w":
        dur = SUB_DURATION_2W
    elif plan == "4w":
        dur = SUB_DURATION_4W
    else:
        dur = timedelta(hours=GIFT_ONE_DAY_HOURS)
    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
        bonus_applied = False
        if REFERRAL_ENABLED and plan in ("2w","4w"):
            try:
                res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                bonus_applied = bool(res)
            except Exception as e:
                logger.warning(f"apply_referral_bonus_if_eligible error: {e}")
    await m.answer(
        f"تم التفعيل للمستخدم {uid}. صالح حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}."
        f"{_bonus_applied_text(bonus_applied)}"
    )
    invite = await get_paid_invite_link(uid)
    if invite:
        try:
            msg = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
            if bonus_applied: msg += f"

🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)."
            await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
        except Exception as e:
            logger.warning(f"SEND INVITE ERROR (/approve): {e}")


@dp.message(Command("activate"))
async def cmd_activate(m: Message):
    await cmd_approve(m)


@dp.message(Command("gift1d"))
async def cmd_gift1d(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = (m.text or "").strip().split()
    if len(parts) != 2:
        return await m.answer("استخدم: /gift1d <user_id>")
    uid = int(parts[1])
    with get_session() as s:
        end_at = approve_paid(s, uid, "gift1d", timedelta(hours=GIFT_ONE_DAY_HOURS))
    await m.answer(f"🎁 تم منح يوم مجاني للمستخدم {uid}. صالح حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}.")
    invite = await get_paid_invite_link(uid)
    if invite:
        try:
            await bot.send_message(uid, "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:", reply_markup=invite_kb(invite))
        except Exception as e:
            logger.warning(f"SEND INVITE ERROR (/gift1d): {e}")


@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    txt = m.text.partition(" ")[2].strip()
    if not txt:
        return await m.answer("استخدم: /broadcast <text>")
    uids = list_active_user_ids(); sent = 0
    for uid in uids:
        try:
            await bot.send_message(uid, txt, parse_mode="HTML", disable_web_page_preview=True)
            sent += 1; await asyncio.sleep(0.02)
        except Exception:
            pass
    await m.answer(f"تم الإرسال إلى {sent} مشترك.")


@dp.message(Command("force_report"))
async def cmd_force_report(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    with get_session() as s:
        stats_24 = get_stats_24h(s); stats_7d = get_stats_7d(s)
    await send_channel(_report_card(stats_24, stats_7d))
    await m.answer("تم إرسال التقرير للقناة.")


@dp.message(Command("refstats"))
async def cmd_refstats(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = (m.text or "").strip().split()
    if len(parts) != 2:
        return await m.answer("استخدم: /refstats <user_id>")
    uid = int(parts[1])
    if not REFERRAL_ENABLED:
        return await m.answer("ميزة الإحالات غير مفعلة.")
    with get_session() as s:
        try:
            st = get_ref_stats(s, uid)
        except Exception as e:
            return await m.answer(f"فشل قراءة الإحصاءات: {e}")
    await m.answer(
        f"📈 Referral stats for {uid}
"
        f"- referred_count: {st.get('referred_count')}
"
        f"- paid_count: {st.get('paid_count')}
"
        f"- total_bonus_days: {st.get('total_bonus_days')}",
        parse_mode="HTML"
    )

# ---------------------------
# Startup checks & polling
# ---------------------------

async def check_channel_and_admin_dm():
    ok = True
    try:
        chat = await bot.get_chat(TELEGRAM_CHANNEL_ID)
        logger.info(f"CHANNEL OK: {chat.id} / {chat.title or chat.username or 'channel'}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")
        ok = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت يعمل الآن. 🚀", parse_mode="HTML")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e}")
    return ok


async def resilient_polling():
    delay = 5
    while True:
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Polling failed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 5
            await asyncio.sleep(3)

# ---------------------------
# Main
# ---------------------------

async def main():
    init_db()
    hb_task = None
    holder = f"{SERVICE_NAME}:{os.getpid()}"

    def _on_sigterm(*_):
        try:
            logger.info("SIGTERM received → releasing leader lock and shutting down…")
            if ENABLE_DB_LOCK and release_leader_lock:
                release_leader_lock(LEADER_LOCK_NAME, holder)
        except Exception as e:
            logger.warning(f"release_leader_lock on SIGTERM warn: {e}")
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass

    if ENABLE_DB_LOCK and acquire_or_steal_leader_lock:
        got = False
        for attempt in range(20):
            ok = acquire_or_steal_leader_lock(LEADER_LOCK_NAME, holder, ttl_seconds=LEADER_TTL)
            if ok:
                got = True; break
            wait_s = 15
            logger.error(f"Another instance holds the leader DB lock. Retrying in {wait_s}s… (try {attempt+1})")
            await asyncio.sleep(wait_s)
        if not got:
            logger.error("Timeout waiting for leader lock. Exiting.")
            return

        async def _leader_heartbeat_task(name: str, holder: str):
            while True:
                try:
                    ok = heartbeat_leader_lock(name, holder)
                    if not ok:
                        logger.error("Leader lock lost! Exiting worker loop.")
                        os._exit(1)
                except Exception as e:
                    logger.warning(f"Heartbeat error: {e}")
                await asyncio.sleep(max(10, LEADER_TTL // 2))
        hb_task = asyncio.create_task(_leader_heartbeat_task(LEADER_LOCK_NAME, holder))

    await load_okx_markets_and_filter()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    await check_channel_and_admin_dm()

    t1 = asyncio.create_task(resilient_polling())
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(monitor_open_trades())
    t5 = asyncio.create_task(kick_expired_members_loop())
    t6 = asyncio.create_task(notify_trial_expiring_soon_loop())

    try:
        await asyncio.gather(t1, t2, t3, t4, t5, t6)
    except TelegramConflictError:
        logger.error("❌ Conflict: يبدو أن نسخة أخرى من البوت تعمل وتستخدم getUpdates. أوقف النسخة الأخرى أو غيّر التوكن.")
        return
    except Exception as e:
        logger.exception(f"FATAL ERROR: {e}")
        try:
            await send_admins(f"❌ تعطل البوت: <code>{_h(str(e))}</code>")
        except Exception:
            pass
        raise
    finally:
        if ENABLE_DB_LOCK and release_leader_lock:
            try:
                release_leader_lock(LEADER_LOCK_NAME, holder)
                logger.info("Leader lock released.")
            except Exception:
                pass
        if hb_task:
            hb_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

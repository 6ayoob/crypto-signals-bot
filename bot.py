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
from aiogram.filters import Command, CommandObject
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

# === Force pricing/durations override (optional, keeps DB keys)
# يفضّل تعديلها من config.py، لكن هذا يضمن تطبيق الأسعار الآن دون كسر التوافق.
if True:
    try:
        PRICE_2_WEEKS_USD = 15     # 1w ظاهرًا للمستخدم
        PRICE_4_WEEKS_USD = 40     # 4w
        SUB_DURATION_2W = timedelta(days=7)    # 1 أسبوع فعليًا
        SUB_DURATION_4W = timedelta(days=28)   # 4 أسابيع
    except Exception:
        pass

# Database
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, close_trade, add_trade_sig,
    has_open_trade_on_symbol, get_stats_24h, get_stats_7d,
    User, Trade,
    # NEW imports for multi-targets flow
    trade_targets_list, trade_entries_list, update_last_hit_idx
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

# Strategy & Symbols
from strategy import check_signal  # NOTE: strategy applies Auto-Relax + scoring
from symbols import list_symbols, INST_TYPE, TARGET_SYMBOLS_COUNT, MIN_24H_USD_VOL
import symbols as symbols_mod  # لاستخدام SYMBOLS_META و _prepare_symbols()

SYMBOLS = list_symbols(INST_TYPE, TARGET_SYMBOLS_COUNT, MIN_24H_USD_VOL)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ===== OKX market helpers (safe loader & symbol adapter) =====
OKX_MARKET_TYPES = [t.strip() for t in os.getenv("OKX_MARKET_TYPES", "spot,swap").split(",") if t.strip()]

def safe_load_okx_markets(exchange, logger=None):
    """
    يجلب الأسواق نوعًا بنوع مع فلترة المعطوبين (base/quote/symbol).
    يحقن النتائج في exchange.markets و exchange.markets_by_id.
    يعيد set بالـ symbols الصحيحة.
    """
    all_markets = []
    total_bad = 0
    for t in OKX_MARKET_TYPES:
        try:
            raw = exchange.fetch_markets_by_type(t, {})
        except Exception as e:
            if logger: logger.warning(f"[okx] skip type {t}: {e}")
            continue
        good = [m for m in raw if m.get("base") and m.get("quote") and m.get("symbol")]
        bad  = len(raw) - len(good)
        total_bad += bad
        if bad and logger:
            logger.info(f"[okx] filtered {bad} malformed {t} markets")
        all_markets.extend(good)

    exchange.markets = exchange.index_by(all_markets, "symbol") if all_markets else {}
    exchange.markets_by_id = exchange.index_by(all_markets, "id") if all_markets else {}
    if logger:
        logger.info(f"[okx] loaded {len(all_markets)} markets (filtered={total_bad}) from types={OKX_MARKET_TYPES}")
    return set(exchange.markets.keys())

def prefer_swap_symbol(sym: str, markets: set) -> str | None:
    """
    يفضّل عقد السواب USDT لو موجود، ثم السبوت.
    جرّب بالترتيب: exact → :USDT → بدون :USDT → إضافة :USDT عند الحاجة.
    """
    if sym in markets:
        return sym

    cand_swap = f"{sym}:USDT" if ":USDT" not in sym else sym
    if cand_swap in markets:
        return cand_swap

    no_colon = sym.replace(":USDT", "").replace(":USGT", "")
    if no_colon in markets:
        return no_colon
    cand_swap2 = f"{no_colon}:USDT"
    if cand_swap2 in markets:
        return cand_swap2

    return None

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

# وضع أدمن مختصر لإخفاء أوامر غير مفيدة
ADMIN_MINIMAL = os.getenv("ADMIN_MINIMAL", "1") == "1"  # 1=إظهار أوامر أساسية فقط

# ==== Plans normalization (user-facing 1w/4w -> internal 2w/4w) ====
PLAN_ALIASES = {
    "1w": "2w", "7d": "2w", "1week": "2w",
    "4w": "4w", "28d": "4w", "4weeks": "4w",
}
def normalize_plan_token(p: str | None) -> str | None:
    if not p: return None
    p = p.strip().lower()
    if p in ("2w", "4w"):
        return p
    return PLAN_ALIASES.get(p)

# === New safety gates (tunable) ===
STRICT_MTF_GATE = os.getenv("STRICT_MTF_GATE", "1") == "1"  # يرفض أي إشارة لا تنال نقاط MTF كاملة
SPREAD_MAX_PCT  = float(os.getenv("SPREAD_MAX_PCT", "0.0025"))  # أقصى سبريد 0.25%
TIME_EXIT_ENABLED = os.getenv("TIME_EXIT_ENABLED", "1") == "1"
TIME_EXIT_DEFAULT_BARS = int(os.getenv("TIME_EXIT_DEFAULT_BARS", "8"))
TIME_EXIT_GRACE_SEC = int(os.getenv("TIME_EXIT_GRACE_SEC", "45"))
HTF_FETCH_PARALLEL = os.getenv("HTF_FETCH_PARALLEL", "0") == "1"  # لتقليل ضغط الاتصالات

# OKX
exchange = ccxt.okx({"enableRateLimit": True})
AVAILABLE_SYMBOLS: List[str] = []
AVAILABLE_SYMBOLS_LOCK = asyncio.Lock()

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

# === فواصل الفحص والتحديث ===
SYMBOLS_REFRESH_HOURS = int(os.getenv("SYMBOLS_REFRESH_HOURS", "4"))  # تحديث الرموز كل 4 ساعات
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "60"))  # 60=دقيقة | 300=5 دقائق

# مراقبة الصفقات
MONITOR_INTERVAL_SEC = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")

# ⚠️ لتقليل ضغط اتصالات urllib3 (pool=10/host)، خفّضنا التوازي الافتراضي
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "8"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "4"))

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
HIT_TP1: Dict[int, bool] = {}  # kept for backward compatibility (no longer essential)

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

def _fmt_price(x: Any) -> str:
    try:
        v = float(x)
        # adaptive formatting
        if v == 0: return "0"
        abs_v = abs(v)
        if abs_v >= 100: return f"{v:.2f}"
        if abs_v >= 1: return f"{v:.4f}"
        return f"{v:.6f}"
    except Exception:
        return str(x)

# --- tuple/list safety helpers for symbols.py interop ---

def _ensure_symbols_list(obj) -> List[str]:
    """
    يقبل list أو (list, meta) ويرجّع List[str] مسطّحة.
    """
    if isinstance(obj, tuple):
        obj = obj[0]
    if not isinstance(obj, (list, tuple)):
        try:
            obj = list(obj)
        except Exception:
            return []
    return [s for s in obj if isinstance(s, str)]

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
        price_line = (
            f"• 1 أسبوع: <b>{PRICE_2_WEEKS_USD}$</b> | "
            f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
        )
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
        "✨ جرّب الإصدار الكامل مجانًا لمدة <b>يوم واحد</b>.\n\n"
        f"{wallet_line}"
        f"{ref_hint}"
        "📞 تواصل مباشر:\n" + _contact_line()
    )

# ===== Signal / close message formatting =====

def _humanize_stop_rule(sr: Optional[dict]) -> str:
    if not sr or not isinstance(sr, dict):
        return "ثابت عند مستوى الوقف المحدد."
    t = (sr.get("type") or "").lower()
    if t in ("breakeven_after", "be_after", "move_to_entry_on_tp1"):
        idx = sr.get("at_idx", 0)
        return f"نقل الوقف لنقطة الدخول بعد الهدف {idx+1}."
    if t == "fixed":
        return "وقف ثابت."
    # fallback
    try:
        return json.dumps(sr, ensure_ascii=False)
    except Exception:
        return "قاعدة وقف مخصّصة."

def format_signal_text_basic(sig: dict) -> str:
    side = (sig.get("side") or "buy").lower()
    title = "إشارة شراء" if side != "sell" else "إشارة بيع"
    # entries / targets
    entries = sig.get("entries")
    targets = sig.get("targets")
    stop_rule = sig.get("stop_rule")

    entries_line = f"💵 الدخول: <code>{_fmt_price(sig['entry'])}</code>"
    if entries and isinstance(entries, list) and len(entries) > 1:
        entries_line = "💵 الدخول (متعدد): " + ", ".join(f"<code>{_fmt_price(x)}</code>" for x in entries)

    targets_block = f"🎯 الأهداف: <code>{_fmt_price(sig['tp1'])}</code>, <code>{_fmt_price(sig['tp2'])}</code>"
    if targets and isinstance(targets, list) and len(targets) >= 1:
        targets_block = "🎯 الأهداف: " + ", ".join(f"<code>{_fmt_price(x)}</code>" for x in targets)

    extra = ""
    if "score" in sig or "regime" in sig:
        extra += f"\n📊 Score: <b>{sig.get('score','-')}</b> | Regime: <b>{_h(sig.get('regime','-'))}</b>"
        if sig.get("reasons"):
            try:
                extra += f"\n🧠 كونفلوينس: <i>{_h(', '.join(sig['reasons'][:6]))}</i>"
            except Exception:
                pass
    # عرض حالة الـ Auto-Relax والعتبات الحالية إن توفرت
    try:
        f = sig.get("features", {}) or {}
        lvl = f.get("relax_level")
        thr = f.get("thresholds") or {}
        if lvl is not None:
            badge = "🟢" if lvl == 0 else ("🟡" if lvl == 1 else "🟠")
            atr_band = thr.get("ATR_BAND") or (None, None)
            extra += (
                f"\n{badge} Auto-Relax: L{lvl} | "
                f"SCORE_MIN={thr.get('SCORE_MIN','-')} | "
                f"RVOL_MIN={thr.get('RVOL_MIN','-')} | "
                f"ATR%∈[{_fmt_price(atr_band[0])}, {_fmt_price(atr_band[1])}]"
            )
            if 'MIN_T1_ABOVE_ENTRY' in thr:
                try:
                    extra += f" | T1≥{float(thr['MIN_T1_ABOVE_ENTRY'])*100:.1f}%"
                except Exception:
                    pass
    except Exception:
        pass

    strat_line = (
        f"\n🧭 النمط: <b>{_h(sig.get('strategy_code','-'))}</b> | ملف: <i>{_h(sig.get('profile','-'))}</i>"
        if sig.get("strategy_code") else ""
    )

    stop_line = f"\n📏 قاعدة الوقف: <i>{_humanize_stop_rule(stop_rule)}</i>"

    return (
        f"🚀 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(sig['symbol'])}</b>\n"
        f"{entries_line}\n"
        f"📉 الوقف: <code>{_fmt_price(sig['sl'])}</code>\n"
        f"{targets_block}"
        f"{strat_line}\n"
        f"⏰ (UTC): <code>{_h(sig.get('timestamp') or datetime.utcnow().strftime('%Y-%m-%d %H:%M'))}</code>"
        f"{extra}{stop_line}\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ <i>قاعدة المخاطرة: أقصى 1% لكل صفقة، وبدون مطاردة للسعر.</i>"
    )

def format_close_text(t: Trade, r_multiple: float | None = None) -> str:
    res = getattr(t, "result", "") or ""
    emoji = {"tp1": "🎯", "tp2": "🏆", "tp3": "🥇", "tp4": "🥈", "tp5": "🥉", "sl": "🛑", "time": "⌛"}.get(res, "ℹ️")
    result_label = {
        "tp1": "تحقق الهدف 1 — خطوة ممتازة!",
        "tp2": "تحقق الهدف 2 — إنجاز رائع!",
        "tp3": "تحقق الهدف 3 — تقدّم قوي!",
        "tp4": "تحقق الهدف 4 — رائعة!",
        "tp5": "تحقق الهدف 5 — قمة الصفقة!",
        "sl": "وقف الخسارة — حماية رأس المال",
        "time": "خروج زمني — الحركة لم تتفعّل سريعًا"
    }.get(res, "إغلاق")

    r_line = f"\n📐 R: <b>{round(r_multiple, 3)}</b>" if r_multiple is not None else ""
    tip = (
        "🔁 نبحث عن فرصة أقوى تالية… الصبر مكسب."
        if res == "sl"
        else "🎯 إدارة الربح أهم من كثرة الصفقات."
    )

    # حاول استخراج tp_final إن وُجد لعرضه مع الأهداف
    tpf = ""
    if getattr(t, "tp_final", None):
        tpf = f" | 🏁 Final: <code>{_fmt_price(t.tp_final)}</code>"

    return (
        f"{emoji} <b>حالة صفقة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(str(t.symbol))}</b>\n"
        f"💵 الدخول: <code>{_fmt_price(t.entry)}</code>\n"
        f"📉 الوقف: <code>{_fmt_price(t.sl)}</code>\n"
        f"🎯 TP1: <code>{_fmt_price(t.tp1)}</code> | 🏁 TP2: <code>{_fmt_price(t.tp2)}</code>{tpf}\n"
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
    # side-aware R
    try:
        if (t.side or "").lower() == "sell":
            R = float(t.sl) - float(t.entry)
            r_multiple = 0.0 if R <= 0 else (float(t.entry) - float(exit_price)) / R
        else:
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
# OKX — build AVAILABLE_SYMBOLS with swap adaptation
# ---------------------------

async def load_okx_markets_and_filter():
    """يبني AVAILABLE_SYMBOLS من SYMBOLS مع تكييف :USDT لعقود السواب، باستخدام لودر آمن."""
    global AVAILABLE_SYMBOLS
    try:
        loop = asyncio.get_event_loop()

        # جرّب اللودر الآمن أولاً
        markets = await loop.run_in_executor(None, safe_load_okx_markets, exchange, logger)
        if not markets:
            # fallback: حاول load_markets التقليدي (قد ينجح أحيانًا)
            await loop.run_in_executor(None, exchange.load_markets)
            markets = set(exchange.markets.keys())

        syms = list(SYMBOLS)
        meta = getattr(symbols_mod, "SYMBOLS_META", {}) or {}

        filtered, skipped = [], []
        for s in syms:
            src = (meta.get(s) or {}).get("source")  # "SPOT" أو "SWAP" إن وُجد
            if src == "SPOT":
                # فضّل السبوت كما هو، وإلا حاول بدون :USDT
                m = s if s in markets else s.replace(":USDT", "")
                if m and m in markets:
                    filtered.append(m)
                else:
                    # محاولة أخيرة: السواب
                    m2 = prefer_swap_symbol(s, markets)
                    (filtered if m2 else skipped).append(m2 or s)
            else:
                # افتراض SWAP/غير محدد → فضّل السواب USDT ثم البدائل
                m = prefer_swap_symbol(s, markets)
                (filtered if m else skipped).append(m or s)

        async with AVAILABLE_SYMBOLS_LOCK:
            AVAILABLE_SYMBOLS = filtered
        logger.info(f"✅ OKX: Loaded {len(filtered)} symbols. Skipped {len(skipped)}: {skipped[:12]}")
    except Exception as e:
        logger.exception(f"❌ load_okx_markets error: {e}")
        async with AVAILABLE_SYMBOLS_LOCK:
            AVAILABLE_SYMBOLS = []

# --- NEW: إعادة بناء القائمة وتحديثها دورياً مع تكييف السواب ---

async def rebuild_available_symbols(new_symbols: List[str] | Tuple[List[str], Dict[str, dict]]):
    """
    يقبل list أو (list, meta). يكيّف الترميز لعقود السواب (:USDT) قبل الفلترة،
    ويستخدم لودر أسواق آمن يحمي من عناصر OKX المعطوبة.
    """
    global AVAILABLE_SYMBOLS
    try:
        meta: Dict[str, dict] = {}
        if isinstance(new_symbols, tuple):
            raw_list, meta = new_symbols
        else:
            raw_list = _ensure_symbols_list(new_symbols)

        if not raw_list:
            logger.warning("rebuild_available_symbols: incoming list is EMPTY — skipped (keeping previous).")
            return

        loop = asyncio.get_event_loop()
        markets = await loop.run_in_executor(None, safe_load_okx_markets, exchange, logger)
        if not markets:
            await loop.run_in_executor(None, exchange.load_markets)
            markets = set(exchange.markets.keys())

        filtered, skipped = [], []
        for s in raw_list:
            src = (meta.get(s) or {}).get("source")
            if src == "SPOT":
                m = s if s in markets else s.replace(":USDT", "")
                if m and m in markets:
                    filtered.append(m)
                else:
                    m2 = prefer_swap_symbol(s, markets)
                    (filtered if m2 else skipped).append(m2 or s)
            else:
                m = prefer_swap_symbol(s, markets)
                (filtered if m else skipped).append(m or s)

        if not filtered:
            logger.warning("rebuild_available_symbols: FILTERED result is EMPTY — skipped (keeping previous).")
            return

        async with AVAILABLE_SYMBOLS_LOCK:
            AVAILABLE_SYMBOLS = filtered
        logger.info(f"✅ symbols reloaded: {len(filtered)} symbols. Skipped {len(skipped)}. First 10: {filtered[:10]}")
    except Exception as e:
        logger.exception(f"❌ rebuild_available_symbols error: {e}")


async def refresh_symbols_periodically():
    """
    يعيد توليد الرموز من symbols.py كل SYMBOLS_REFRESH_HOURS ثم يعيد بناء AVAILABLE_SYMBOLS.
    يستفيد من (list, meta) العائدة من _prepare_symbols().
    """
    init_delay = int(os.getenv("SYMBOLS_INIT_DELAY_SEC", "60"))
    if init_delay > 0:
        await asyncio.sleep(init_delay)

    while True:
        try:
            fresh_list, fresh_meta = symbols_mod._prepare_symbols()
            if not fresh_list:
                logger.warning("[symbols] refresh returned EMPTY — keeping previous AVAILABLE_SYMBOLS.")
            else:
                await rebuild_available_symbols((fresh_list, fresh_meta))
                logger.info(f"[symbols] refreshed → {len(fresh_list)} | first 10: {', '.join(fresh_list[:10])}")
        except Exception as e:
            logger.exception(f"[symbols] refresh failed: {e}")
        await asyncio.sleep(SYMBOLS_REFRESH_HOURS * 3600)

# ---------------------------
# Data fetchers
# ---------------------------
def _okx_ticker_params(sym_eff: str) -> dict:
    """
    يحدّد instType المناسب لـ OKX (SWAP أو SPOT) إن أمكن من exchange.markets.
    """
    try:
        m = None
        if getattr(exchange, "markets", None):
            m = exchange.markets.get(sym_eff)
            if m is None:
                # أحيانًا sym_eff يكون id (مثل IMX-USDT-SWAP)
                mid = getattr(exchange, "markets_by_id", {}).get(sym_eff)
                if mid:
                    m = mid
        if m:
            if m.get("contract") or (m.get("type") == "swap"):
                return {"instType": "SWAP"}
            if m.get("spot") or (m.get("type") == "spot"):
                return {"instType": "SPOT"}
    except Exception:
        pass
    # افتراضي آمن: جرّب SWAP أولًا (أغلب رموزنا سواب)، وسنُسقط إلى SPOT بالفولباك داخل المستدعي.
    return {"instType": "SWAP"}

def _maybe_adapt_symbol_for_fetch(sym: str) -> str:
    """
    يكيّف الرمز للجلب: يفضّل السواب :USDT إن وُجد، وإلا يحاول البدائل.
    يعتمد على exchange.markets، ويعيد sym الأصلي إن تعذّر التكييف.
    """
    try:
        markets = set(exchange.markets.keys()) if getattr(exchange, "markets", None) else set()
        if not markets:
            # حمّل الأسواق بطريقة آمنة أولاً
            loop = asyncio.get_event_loop()
            markets = asyncio.get_event_loop().run_until_complete(
                loop.run_in_executor(None, safe_load_okx_markets, exchange, logger)
            )
            if not markets:
                loop.run_in_executor(None, exchange.load_markets)
                markets = set(exchange.markets.keys()) if getattr(exchange, "markets", None) else set()
        if markets:
            alt = prefer_swap_symbol(sym, markets)
            return alt or sym
    except Exception:
        pass
    return sym

async def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=300) -> list:
    sym_eff = _maybe_adapt_symbol_for_fetch(symbol)
    for attempt in range(4):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: exchange.fetch_ohlcv(sym_eff, timeframe=timeframe, limit=limit)
            )
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.6 * (attempt + 1) + random.uniform(0.1, 0.4))
        except ccxt.BadSymbol as e:
            if sym_eff != symbol:
                logger.info(f"FETCH_OHLCV retry with original symbol after adapt fail: {sym_eff} -> {symbol}")
                sym_eff = symbol  # جرّب الأصل كفرصة أخيرة
                continue
            logger.warning(f"❌ FETCH_OHLCV BadSymbol [{symbol}]: {e}")
            return []
        except Exception as e:
            logger.warning(f"❌ FETCH_OHLCV ERROR [{sym_eff}]: {e}")
            return []
    return []

# NEW: HTF support (H1/H4/D1)
HTF_MAP = {"H1": ("1h", 220), "H4": ("4h", 220), "D1": ("1d", 220)}

async def fetch_ohlcv_htf(symbol: str) -> dict:
    """Fetches H1/H4/D1 OHLCV; parallelism optional to reduce connection pool pressure."""
    sym_eff = _maybe_adapt_symbol_for_fetch(symbol)

    async def _one(tf_ccxt: str, limit: int) -> list:
        se = sym_eff
        for attempt in range(3):
            try:
                await RATE.wait()
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None, lambda: exchange.fetch_ohlcv(se, timeframe=tf_ccxt, limit=limit)
                )
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                await asyncio.sleep(0.5 * (attempt + 1))
            except ccxt.BadSymbol:
                # حاول الرمز الأصلي كفرصة أخيرة
                if se != symbol:
                    se = symbol
                    continue
                return []
            except Exception:
                return []
        return []

    if HTF_FETCH_PARALLEL:
        tasks = {k: asyncio.create_task(_one(v[0], v[1])) for k, v in HTF_MAP.items()}
        out = {}
        for k, t in tasks.items():
            try:
                out[k] = await t
            except Exception:
                out[k] = []
    else:
        out = {}
        for k, v in HTF_MAP.items():
            out[k] = await _one(v[0], v[1])

    return {k: v for k, v in out.items() if v}

async def fetch_ticker_price(symbol: str) -> Optional[float]:
    """
    يجلب آخر سعر بدقّة أعلى على OKX:
    - يحاول تمرير instType المناسب (SWAP للرموز التي تحتوي ':USDT'، وإلا SPOT).
    - في حال الفشل، يبدّل بين SWAP/SPOT ثم يجرب بدون params كفولباك نهائي.
    - يحتفظ بنفس منطق إعادة المحاولة وBadSymbol كما في الأصل.
    """
    def _guess_inst_type(sym: str) -> str:
        # عقود OKX عادةً ترميزها: "BTC/USDT:USDT" → SWAP
        # بينما السبوت: "BTC/USDT" → SPOT
        return "SWAP" if ":USDT" in (sym or "") else "SPOT"

    sym_eff = _maybe_adapt_symbol_for_fetch(symbol)

    for attempt in range(3):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()

            params = {"instType": _guess_inst_type(sym_eff)}

            def _do_fetch_ticker(sym: str, p: dict):
                try:
                    return exchange.fetch_ticker(sym, params=p)
                except Exception:
                    # فولباك 1: قلب النوع SPOT↔SWAP
                    try:
                        alt = {"instType": "SPOT"} if p.get("instType") == "SWAP" else {"instType": "SWAP"}
                        return exchange.fetch_ticker(sym, params=alt)
                    except Exception:
                        # فولباك 2: بدون أي params
                        return exchange.fetch_ticker(sym)

            ticker = await loop.run_in_executor(None, lambda: _do_fetch_ticker(sym_eff, params))

            price = (
                ticker.get("last")
                or ticker.get("close")
                or (ticker.get("info", {}) or {}).get("last")
                or (ticker.get("info", {}) or {}).get("close")
            )
            return float(price) if price is not None else None

        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.5 * (attempt + 1) + random.uniform(0.05, 0.2))

        except ccxt.BadSymbol:
            if sym_eff != symbol:
                logger.info(f"FETCH_TICKER retry with original symbol after adapt fail: {sym_eff} -> {symbol}")
                sym_eff = symbol
                continue
            logger.warning(f"❌ FETCH_TICKER BadSymbol [{symbol}]")
            return None

        except Exception as e:
            logger.warning(f"❌ FETCH_TICKER ERROR [{sym_eff}]: {e}")
            return None

    return None


async def fetch_spread_pct(symbol: str) -> Optional[float]:
    """
    يعيد (ask - bid) / mid إن توفّر bid/ask.
    تحسينات:
    - يمرّر instType إلى OKX: SWAP للرموز التي تحتوي ':USDT' وإلا SPOT.
    - فولباك ذكي: جرّب عكس النوع (SPOT↔SWAP) ثم بدون بارامترات.
    - إن لم تتوفر bid/ask من ticker، نحاول order_book (أفضل عرض/طلب).
    """
    def _guess_inst_type(sym: str) -> str:
        return "SWAP" if ":USDT" in (sym or "") else "SPOT"

    async def _fetch_ticker_any(sym: str) -> Optional[dict]:
        # 1) بالـ instType المتوقّع
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: exchange.fetch_ticker(sym, params={"instType": _guess_inst_type(sym)})
            )
        except Exception:
            pass
        # 2) عكس النوع
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            alt = "SPOT" if _guess_inst_type(sym) == "SWAP" else "SWAP"
            return await loop.run_in_executor(None, lambda: exchange.fetch_ticker(sym, params={"instType": alt}))
        except Exception:
            pass
        # 3) بدون أي بارامترات
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: exchange.fetch_ticker(sym))
        except Exception:
            return None

    async def _fetch_book_any(sym: str) -> Optional[tuple[float, float]]:
        # نحاول بعمق صغير لتقليل الضغط
        for params in (
            {"instType": _guess_inst_type(sym)},
            {"instType": "SPOT" if _guess_inst_type(sym) == "SWAP" else "SWAP"},
            None,
        ):
            try:
                await RATE.wait()
                loop = asyncio.get_event_loop()
                if params is not None:
                    book = await loop.run_in_executor(None, lambda: exchange.fetch_order_book(sym, limit=5, params=params))
                else:
                    book = await loop.run_in_executor(None, lambda: exchange.fetch_order_book(sym, limit=5))
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                best_bid = float(bids[0][0]) if bids and bids[0] and bids[0][0] is not None else None
                best_ask = float(asks[0][0]) if asks and asks[0] and asks[0][0] is not None else None
                if best_bid and best_ask and best_bid > 0 and best_ask > 0:
                    return best_bid, best_ask
            except Exception:
                continue
        return None

    sym_eff = _maybe_adapt_symbol_for_fetch(symbol)

    try:
        # 1) جرّب التيكر مع الفولباكات
        ticker = await _fetch_ticker_any(sym_eff)
        bid = (ticker or {}).get("bid")
        ask = (ticker or {}).get("ask")

        # 2) إن كان التيكر غير كافٍ، استخدم دفتر الأوامر
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            book_pair = await _fetch_book_any(sym_eff)
            if not book_pair:
                return None
            bid, ask = book_pair

        mid = (float(ask) + float(bid)) / 2.0
        if mid <= 0:
            return None
        return (float(ask) - float(bid)) / max(mid, 1e-9)

    except ccxt.BadSymbol:
        # محاولة أخيرة بالرمز الأصلي غير المكيّف
        if sym_eff != symbol:
            try:
                ticker = await _fetch_ticker_any(symbol)
                bid = (ticker or {}).get("bid")
                ask = (ticker or {}).get("ask")
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    book_pair = await _fetch_book_any(symbol)
                    if not book_pair:
                        return None
                    bid, ask = book_pair
                mid = (float(ask) + float(bid)) / 2.0
                if mid <= 0:
                    return None
                return (float(ask) - float(bid)) / max(mid, 1e-9)
            except Exception:
                return None
        return None
    except Exception:
        return None

# ---------------------------
# Dedupe signals
# ---------------------------

def _norm_sym_for_dedupe(sym: Optional[str]) -> str:
    s = (sym or "").upper()
    return s.replace(":USDT", "/USDT").replace("USDT:", "USDT/")  # تطبيع أوسع

def _should_skip_duplicate(sig: dict) -> bool:
    sym = _norm_sym_for_dedupe(sig.get("symbol"))
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
    htf = await fetch_ohlcv_htf(sym)
    sig = check_signal(sym, data, htf if htf else None)
    return sig if sig else None

async def scan_and_dispatch():
    async with AVAILABLE_SYMBOLS_LOCK:
        symbols_snapshot = list(AVAILABLE_SYMBOLS)
    if not symbols_snapshot:
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

        for i in range(0, len(symbols_snapshot), SCAN_BATCH_SIZE):
            batch = symbols_snapshot[i:i + SCAN_BATCH_SIZE]
            sigs = await asyncio.gather(*[_guarded_scan(s) for s in batch])

            for sig in filter(None, sigs):
                # === Extra safety gates BEFORE persisting/sending ===
                # 1) Strict MTF gate (require full MTF points)
                try:
                    bd = ((sig.get('features') or {}).get('score_breakdown') or {})
                    mtf_points = float(bd.get('mtf', 0))
                    if STRICT_MTF_GATE and mtf_points < 13:
                        logger.info(f"⛔ MTF_STRICT skip {sig['symbol']} (mtf_points={mtf_points})")
                        continue
                except Exception:
                    if STRICT_MTF_GATE:
                        continue

                # 2) Spread sanity
                try:
                    sp = await fetch_spread_pct(sig["symbol"])  # None = can't measure → allow
                    if sp is not None and sp > SPREAD_MAX_PCT:
                        logger.info(f"⛔ Spread>{SPREAD_MAX_PCT:.4f} skip {sig['symbol']} (spread={sp:.4f})")
                        continue
                except Exception:
                    pass

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

                    # audit id + fallback-safe entry
                    entry_for_id = sig.get("entry")
                    if entry_for_id is None:
                        try:
                            entry_for_id = (sig.get("entries") or [None])[0]
                        except Exception:
                            entry_for_id = None

                    audit_id = _make_audit_id(sig["symbol"], entry_for_id or 0.0, sig.get("score", 0))

                    try:
                        trade_id = add_trade_sig(s, sig, audit_id=audit_id, qty=None)
                    except Exception as e:
                        logger.exception(f"⚠️ add_trade_sig failed, fallback: {e}")
                        # fallback: احفظ صفقة أساسية بأقل الحقول
                        trade_id = add_trade(
                            s,
                            sig["symbol"], sig.get("side", "buy"),
                            entry_for_id or 0.0,
                            sig.get("sl", 0.0),
                            sig.get("tp1", 0.0), sig.get("tp2", 0.0)
                        )

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

                        logger.info(f"✅ SIGNAL SENT: {sig['symbol']} audit={audit_id}")
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
# Monitor open trades (multi-target + dynamic stop + time exit)
# ---------------------------

def _tp_key(idx: int) -> str:
    return f"tp{idx+1}"

def _parse_stop_rule_json(raw: Optional[str]) -> Optional[dict]:
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None

def _stop_triggered_by_rule(t: Trade, price: float, last_hit_idx: int) -> bool:
    """
    قواعد بسيطة وقوية:
    - fixed (الافتراضي): يستخدم SL من الـ DB.
    - breakeven_after / move_to_entry_on_tp1: انقل الوقف لنقطة الدخول بعد تحقق هدف at_idx.
    """
    sr = _parse_stop_rule_json(getattr(t, "stop_rule_json", None))
    side = (t.side or "buy").lower()

    # الأساس = SL من الـ DB
    effective_sl = float(t.sl)

    if sr and isinstance(sr, dict):
        ttype = (sr.get("type") or "").lower()
        if ttype in ("breakeven_after", "be_after", "move_to_entry_on_tp1"):
            at_idx = int(sr.get("at_idx", 0))
            if last_hit_idx >= at_idx:
                effective_sl = float(t.entry)

    # شرط التفعيل حسب الاتجاه
    if side == "sell":
        return price >= effective_sl
    return price <= effective_sl

def timeframe_to_seconds(tf: str) -> int:
    tf = (tf or "5m").lower().strip()
    if tf.endswith("ms"): return 0
    if tf.endswith("s"):  return int(tf[:-1])
    if tf.endswith("m"):  return int(tf[:-1]) * 60
    if tf.endswith("h"):  return int(tf[:-1]) * 3600
    if tf.endswith("d"):  return int(tf[:-1]) * 86400
    if tf.endswith("w"):  return int(tf[:-1]) * 7 * 86400
    return 300

def _get_bars_budget_for_trade(t: Trade) -> Optional[int]:
    """
    يحدد سقف الشموع لبلوغ TP1:
    - يفضّل t.max_bars_to_tp1 إن كان موجوداً.
    - وإلا يحاول extra_json.max_bars_to_tp1.
    - وإلا يعتمد الافتراضي مع تسريع خفيف لـ BRK/SWEEP.
    """
    try:
        if hasattr(t, "max_bars_to_tp1") and t.max_bars_to_tp1:
            return int(t.max_bars_to_tp1)
    except Exception:
        pass
    try:
        extra = json.loads(getattr(t, "extra_json", "") or "{}")
        if extra.get("max_bars_to_tp1") is not None:
            return int(extra["max_bars_to_tp1"])
    except Exception:
        pass
    # افتراضي
    try:
        if getattr(t, "strategy_code", None) in ("BRK", "SWEEP"):
            return max(6, TIME_EXIT_DEFAULT_BARS - 2)
    except Exception:
        pass
    return TIME_EXIT_DEFAULT_BARS

async def _calc_trailing_stop_if_any(t: Trade, last_hit_idx: int) -> Optional[float]:
    """
    يُعيد مستوى وقف متحرّك (Trailing) اختياري بعد TP2:
    - يعتمد على ATR الحالي ومضاعف trail_atr_mult (من الحقول أو extra_json).
    - للإتجاه buy: SL_trail = max(SL_current, price - ATR*mult) (لا نُنزل الوقف).
    - للإتجاه sell: SL_trail = min(SL_current, price + ATR*mult) (لا نُرفع الوقف).
    - لا يكتب في DB؛ يُستخدم كشرط خروج فقط.
    """
    try:
        # مفعّل فقط بعد تحقق TP2 أو أكثر
        if last_hit_idx < 1:
            return None

        trail_enabled = False
        trail_mult = None

        # حاول من خصائص الصفقة مباشرة
        if hasattr(t, "trail_after_tp2") and getattr(t, "trail_after_tp2"):
            trail_enabled = True
            trail_mult = float(getattr(t, "trail_atr_mult", 1.0) or 1.0)

        # fallback: من extra_json
        if not trail_enabled:
            try:
                extra = json.loads(getattr(t, "extra_json", "") or "{}")
                if extra.get("trail_after_tp2"):
                    trail_enabled = True
                    trail_mult = float(extra.get("trail_atr_mult", 1.0))
            except Exception:
                pass

        if not trail_enabled or trail_mult is None:
            return None

        # احسب ATR سريع من آخر 120 شمعة TF الحالي
        ohlcv = await fetch_ohlcv(t.symbol, timeframe=TIMEFRAME, limit=160)
        if not ohlcv or len(ohlcv) < 20:
            return None

        # ATR EWM(14) المختصر
        import math
        import pandas as pd
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
        c_prev = df["close"].shift(1)
        tr = pd.concat([
            (df["high"] - df["low"]).abs(),
            (df["high"] - c_prev).abs(),
            (df["low"]  - c_prev).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
        if not math.isfinite(atr) or atr <= 0:
            return None

        price = await fetch_ticker_price(t.symbol)
        if price is None:
            return None
        price = float(price)
        cur_sl = float(t.sl)
        side = (t.side or "buy").lower()

        if side == "buy":
            proposed = price - atr * float(trail_mult)
            # لا نُنزل الوقف تحت الوقف الحالي
            sl_trail = max(cur_sl, proposed)
        else:
            proposed = price + atr * float(trail_mult)
            # لا نُرفع الوقف فوق الوقف الحالي
            sl_trail = min(cur_sl, proposed)

        return float(sl_trail)
    except Exception:
        return None

async def monitor_open_trades():
    from types import SimpleNamespace
    tf_sec = timeframe_to_seconds(TIMEFRAME)

    while True:
        try:
            with get_session() as s:
                open_trades = s.query(Trade).filter(Trade.status == "open").all()
                for t in open_trades:
                    price = await fetch_ticker_price(t.symbol)
                    if price is None:
                        continue
                    price = float(price)

                    side = (t.side or "buy").lower()
                    tgts = trade_targets_list(t)  # ordered list
                    if not tgts:
                        # حماية نادرة: لا أهداف — لا نفعل شيئًا
                        continue

                    last_idx = int(getattr(t, "last_hit_idx", 0) or 0)

                    # ---- قواعد الوقف (ثابت/تعادل) + Trailing (اختياري بعد TP2)
                    # 1) وقف القاعدة/التعادل
                    if _stop_triggered_by_rule(t, price, last_idx):
                        result, exit_px = "sl", price  # استخدم السعر الحالي
                        r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                        try:
                            close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                        except Exception as e:
                            logger.warning(f"⚠️ close_trade warn: {e}")

                        msg = format_close_text(t, r_multiple)
                        extra = (MESSAGES_CACHE.get(t.id, {}) or {}).get("sl")
                        if extra:
                            msg += "\n\n" + extra
                        await notify_subscribers(msg)
                        await asyncio.sleep(0.05)
                        continue

                    # 2) وقف متحرّك بعد TP2 (لا يكتب للـ DB؛ شرط خروج فقط)
                    sl_trail = await _calc_trailing_stop_if_any(t, last_idx)
                    if sl_trail is not None:
                        if (side == "buy" and price <= sl_trail) or (side == "sell" and price >= sl_trail):
                            result, exit_px = "sl", price
                            r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                            try:
                                close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                            except Exception as e:
                                logger.warning(f"⚠️ close_trade warn (trail): {e}")
                            t.result = result
                            msg = format_close_text(t, r_multiple)
                            extra_msg = (MESSAGES_CACHE.get(t.id, {}) or {}).get("tp2")  # غالبًا بعد TP2
                            if extra_msg:
                                msg += "\n\n" + extra_msg
                            await notify_subscribers(msg)
                            await asyncio.sleep(0.05)
                            continue

                    # ---- حساب أعلى هدف تم بلوغه حتى الآن
                    new_hit_idx = -1
                    if side == "sell":
                        for idx, tgt in enumerate(tgts):
                            try:
                                if price <= float(tgt):
                                    new_hit_idx = max(new_hit_idx, idx)
                            except Exception:
                                continue
                    else:
                        for idx, tgt in enumerate(tgts):
                            try:
                                if price >= float(tgt):
                                    new_hit_idx = max(new_hit_idx, idx)
                            except Exception:
                                continue

                    # ---- خروج زمني قبل بلوغ آخر هدف
                    if TIME_EXIT_ENABLED:
                        try:
                            created_at: Optional[datetime] = getattr(t, "created_at", None)
                            if created_at is None and hasattr(t, "opened_at"):
                                created_at = getattr(t, "opened_at")  # fallback
                            if created_at and isinstance(created_at, datetime):
                                # اجعلها aware بـ UTC
                                if created_at.tzinfo is None:
                                    created_at = created_at.replace(tzinfo=timezone.utc)
                                eff_last = int(getattr(t, "last_hit_idx", -1) or -1)
                                if eff_last < 0:
                                    bars_budget = _get_bars_budget_for_trade(t)
                                    elapsed = (datetime.now(timezone.utc) - created_at).total_seconds()
                                    if elapsed >= bars_budget * tf_sec + TIME_EXIT_GRACE_SEC:
                                        result, exit_px = "time", price
                                        r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                                        try:
                                            close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                                        except Exception as e:
                                            logger.warning(f"⚠️ close_trade warn (time-exit): {e}")
                                        t.result = result
                                        msg = format_close_text(t, r_multiple)
                                        extra_msg = (MESSAGES_CACHE.get(t.id, {}) or {}).get("time")
                                        if extra_msg:
                                            msg += "\n\n" + extra_msg
                                        await notify_subscribers(msg)
                                        await asyncio.sleep(0.05)
                                        continue
                        except Exception as e:
                            logger.debug(f"time-exit check warn: {e}")

                    # ---- لا أهداف مُحقَّقة
                    if new_hit_idx < 0:
                        continue

                    # ---- إغلاق عند بلوغ الهدف الأخير
                    if new_hit_idx >= len(tgts) - 1:
                        res_key = _tp_key(len(tgts) - 1)
                        exit_px = float(tgts[-1])
                        r_multiple = on_trade_closed_update_risk(t, res_key, exit_px)
                        try:
                            close_trade(s, t.id, res_key, exit_price=exit_px, r_multiple=r_multiple)
                        except Exception as e:
                            logger.warning(f"⚠️ close_trade warn: {e}")

                        t.result = res_key  # للعرض
                        msg = format_close_text(t, r_multiple)
                        extra = (MESSAGES_CACHE.get(t.id, {}) or {}).get(res_key)
                        if extra:
                            msg += "\n\n" + extra
                        await notify_subscribers(msg)
                        await asyncio.sleep(0.05)
                        continue

                    # ---- هدف وسيط مُحقَّق (تقدّم)
                    if new_hit_idx > last_idx:
                        update_last_hit_idx(s, t.id, new_hit_idx)
                        tmp = SimpleNamespace(
                            symbol=t.symbol, entry=t.entry, sl=t.sl, tp1=t.tp1, tp2=t.tp2,
                            tp_final=t.tp_final, result=_tp_key(new_hit_idx)
                        )
                        msg = format_close_text(tmp, None)
                        extra = (MESSAGES_CACHE.get(t.id, {}) or {}).get(_tp_key(new_hit_idx))
                        if extra:
                            msg += "\n\n" + extra
                        if new_hit_idx == 0:
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

def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)

def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return int(default)

def render_daily_report(stats: dict) -> str:
    """
    stats: ناتج database._period_stats
    keys المتوقعة: signals, open, tp1, tp2, tp_total, sl, win_rate, r_sum
    """
    signals  = _safe_int(stats.get("signals", 0))
    open_now = _safe_int(stats.get("open", 0))
    # لو tp_total غير موجود نحسبه من tp1+tp2
    wins     = _safe_int(stats.get("tp_total", stats.get("tp1", 0))) + _safe_int(stats.get("tp2", 0))
    losses   = _safe_int(stats.get("sl", 0))
    win_rate = max(0.0, min(100.0, _safe_float(stats.get("win_rate", 0.0))))
    r_sum    = _safe_float(stats.get("r_sum", 0.0))

    msg = (
        "📊 <b>التقرير اليومي — لقطة أداء</b>\n"
        f"• إشارات جديدة: <b>{signals}</b>\n"
        f"• صفقات مفتوحة حاليًا: <b>{open_now}</b>\n"
        f"• نتائج الإغلاقات: ربح <b>{wins}</b> / خسارة <b>{losses}</b>\n"
        f"• نسبة الربح: <b>{win_rate:.1f}%</b>\n"
        f"• صافي R: <b>{r_sum:+.2f}R</b>\n"
    )
    return msg

def _report_card(stats_24: dict, stats_7: dict) -> str:
    part1 = render_daily_report(stats_24)

    # ملخص 7 أيام
    try:
        wr7 = max(0.0, min(100.0, _safe_float(stats_7.get("win_rate", 0.0))))
        n7  = _safe_int(stats_7.get("signals", 0))
        r7  = _safe_float(stats_7.get("r_sum", 0.0))
        part2 = f"\n📅 آخر 7 أيام — إشارات: <b>{n7}</b> | نسبة الربح: <b>{wr7:.1f}%</b> | صافي R: <b>{r7:+.2f}R</b>"
    except Exception:
        part2 = ""

    # “منذ آخر إشارة” + مستوى Auto-Relax (من strategy_state.json)
    try:
        st_path = os.getenv("STRATEGY_STATE_FILE", "strategy_state.json")
        if Path(st_path).exists():
            st = json.loads(Path(st_path).read_text(encoding="utf-8") or "{}")
            last_ts = _safe_float(st.get("last_signal_ts", 0))
            if last_ts > 0:
                hours = max(0.0, (time.time() - last_ts) / 3600.0)
                h1 = _safe_int(os.getenv("AUTO_RELAX_AFTER_HRS_1", "6"), 6)
                h2 = _safe_int(os.getenv("AUTO_RELAX_AFTER_HRS_2", "12"), 12)
                lvl = 2 if hours >= h2 else (1 if hours >= h1 else 0)
                part2 += f"\n⏳ منذ آخر إشارة: <b>{hours:.1f} ساعة</b> | Auto-Relax: <b>L{lvl}</b>"
    except Exception:
        pass

    return part1 + part2

async def daily_report_once():
    with get_session() as s:
        stats_24 = get_stats_24h(s) or {}
        stats_7d = get_stats_7d(s) or {}
        txt = _report_card(stats_24, stats_7d)
        await send_channel(txt)
        logger.info("Daily report sent.")

async def daily_report_loop():
    tz = pytz.timezone(TIMEZONE)
    while True:
        try:
            now = datetime.now(tz)
            target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
            if now >= target:
                target = target + timedelta(days=1)
            delay = max(1.0, (target - now).total_seconds())
            logger.info(f"Next daily report at {target.isoformat()} ({TIMEZONE})")
            await asyncio.sleep(delay)
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
    # ربط الإحالة من ديب لينك (إن وُجد)
    payload_code = _parse_start_payload(m.text)
    if REFERRAL_ENABLED and payload_code:
        try:
            with get_session() as s:
                linked, reason = link_referred_by(s, m.from_user.id, payload_code)
            if linked:
                await m.answer(
                    "🤝 تم تسجيل كود الإحالة بنجاح. عند أول تفعيل مدفوع ستحصل على هدية الأيام تلقائيًا 🎁",
                    parse_mode="HTML"
                )
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

        referral_msg = (
            "🤝 <b>برنامج الإحالة</b>\n"
            f"🎁 شارك رابطك واحصل على <b>{REF_BONUS_DAYS} يوم</b> هدية عند أول اشتراك مدفوع لصديقك.\n"
            "—\n"
            "1) انسخ رابط الدعوة الخاص بك من /ref\n"
            "2) أرسله لصديقك وسجِّل عبره\n"
            "3) بعد الدفع، تصلك الهدية تلقائيًا\n"
        )
        await q.message.answer(referral_msg, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception as e:
        logger.warning(f"show_ref_link error: {e}")
        await q.answer("خطأ غير متوقع.", show_alert=True)

@dp.message(Command("ref"))
async def cmd_ref(m: Message):
    if not REFERRAL_ENABLED:
        return await m.answer("ℹ️ برنامج الإحالة غير مفعل حالياً.", parse_mode="HTML")
    with get_session() as s:
        code, link = await _build_ref_link(m.from_user.id, s)
        try:
            stats = get_ref_stats(s, m.from_user.id) or {}
        except Exception:
            stats = {}
    txt = (
        "🎁 <b>برنامج الإحالة</b>\n"
        f"• رابطك: <code>{link or '—'}</code>\n"
        f"• مدعوون: <b>{_safe_int(stats.get('referred_count',0))}</b>\n"
        f"• اشتراكات مدفوعة عبرك: <b>{_safe_int(stats.get('paid_count',0))}</b>\n"
        f"• أيام هدية: <b>{_safe_int(stats.get('total_bonus_days',0))}</b>\n"
        "أرسل هذا الرابط لصديقك. عند أول اشتراك مدفوع تُحتسب الهدية تلقائيًا."
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
    txt_ok = "✅ <b>اشتراكك نشط.</b>\n🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
    txt_no = "❌ <b>لا تملك اشتراكًا نشطًا.</b>\n✨ اطلب التفعيل وسيقوم الأدمن بإتمامه."
    await q.message.answer(txt_ok if ok else txt_no, parse_mode="HTML")
    await q.answer()

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)
    if ok:
        await q.message.answer(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁\n"
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
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\n"
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
        return getattr(inv, "invite_link", None)
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
        return getattr(inv, "invite_link", None)
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
        return getattr(inv, "invite_link", None)
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

    # أزرار الأدمن (النص محدث؛ callback_data تبقى كما هي 2w/4w/gift1d)
    kb_admin = InlineKeyboardBuilder()
    kb_admin.button(text="✅ تفعيل 1 أسبوع (1w)", callback_data=f"approve_inline:{uid}:2w")
    kb_admin.button(text="✅ تفعيل 4 أسابيع (4w)", callback_data=f"approve_inline:{uid}:4w")
    kb_admin.button(text="🎁 تفعيل يوم مجاني (gift1d)", callback_data=f"approve_inline:{uid}:gift1d")
    kb_admin.button(text="❌ رفض", callback_data=f"reject_inline:{uid}")
    kb_admin.button(text="👤 مراسلة المستخدم", url=f"tg://user?id={uid}")
    kb_admin.adjust(1)

    await send_admins(
        "🔔 <b>طلب اشتراك جديد</b>\n"
        f"المستخدم: {user_line}\n"
        "اختر نوع التفعيل:",
        reply_markup=kb_admin.as_markup(),
    )

    # رسالة للمستخدم
    try:
        price_line = (
            f"• 1 أسبوع: <b>{PRICE_2_WEEKS_USD}$</b> | "
            f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
        )
    except Exception:
        price_line = ""
    wallet_line = ""
    try:
        if USDT_TRC20_WALLET:
            wallet_line = "💳 محفظة USDT (TRC20):\n" + f"<code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
    except Exception:
        pass

    await q.message.answer(
        "📩 تم إرسال طلبك للأدمن.\n"
        "يرجى التحويل ثم مراسلة الأدمن لتأكيد التفعيل.\n\n"
        "الخطط:\n"
        f"{price_line}"
        f"{wallet_line}"
        "بعد التأكيد ستستلم رابط الدخول للقناة تلقائيًا.",
        parse_mode="HTML",
        reply_markup=support_dm_kb() if (SUPPORT_USERNAME or SUPPORT_CHAT_ID) else None,
    )
    await q.answer()

# === دفع يدوي: /submit_tx <hash> <1w|4w|2w|7d|28d> ===

def _looks_like_tx_hash(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 8:
        return False
    # سماح بأحرف/أرقام ومع بعض الرموز الشائعة
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-_./")
    return all(ch in allowed for ch in s)

@dp.message(Command("submit_tx"))
async def cmd_submit_tx(m: Message, command: CommandObject):
    """
    /submit_tx <tx_hash> <1w|4w|2w|7d|28d>
    يحفظ طلب تفعيل بانتظار موافقة الأدمن (لا يفعّل تلقائيًا).
    """
    args = (command.args or "").strip().split()
    if len(args) < 2:
        return await m.answer(
            "الصيغة: <code>/submit_tx &lt;tx_hash&gt; &lt;1w|4w&gt;</code>\n"
            "مثال: <code>/submit_tx e3f1a... 1w</code>",
            parse_mode="HTML"
        )

    tx_hash, plan_in = args[0], args[1]
    if not _looks_like_tx_hash(tx_hash):
        return await m.answer("⚠️ هاش التحويل غير واضح. أرسِل الهاش الكامل كما يظهر لك.", parse_mode="HTML")

    plan = normalize_plan_token(plan_in)
    if not plan:
        return await m.answer("خطة غير صحيحة. استخدم 1w أو 4w.", parse_mode="HTML")

    try:
        price = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD
    except Exception:
        price = "-"

    # إشعار الأدمن بطلب التفعيل
    kb_admin = InlineKeyboardBuilder()
    uid = m.from_user.id
    kb_admin.button(
        text=f"✅ تفعيل { '1 أسبوع' if plan=='2w' else '4 أسابيع' }",
        callback_data=f"approve_inline:{uid}:{plan}"
    )
    kb_admin.button(text="❌ رفض", callback_data=f"reject_inline:{uid}")
    kb_admin.button(text="👤 مراسلة المستخدم", url=f"tg://user?id={uid}")
    kb_admin.adjust(1)

    uname = (m.from_user.username and f"@{m.from_user.username}") or (m.from_user.full_name or "")
    await send_admins(
        "🧾 <b>طلب تفعيل (تحويل يدوي)</b>\n"
        f"المستخدم: {_h(uname)} (ID: <code>{uid}</code>)\n"
        f"الخطة: <b>{'1w' if plan=='2w' else '4w'}</b> – السعر: <b>{price}$</b>\n"
        f"الهاش: <code>{_h(tx_hash)}</code>",
        reply_markup=kb_admin.as_markup()
    )

    await m.answer(
        "✅ تم استلام طلبك، سيتم التفعيل بعد مراجعة الأدمن.\n"
        "إن احتجت مساعدة راسل الأدمن من الزر بالأسفل.",
        parse_mode="HTML",
        reply_markup=support_dm_kb() if (SUPPORT_USERNAME or SUPPORT_CHAT_ID) else None
    )

# === Admin Approvals (inline) ===

def _bonus_applied_text(applied: bool) -> str:
    return f"\n🎁 <i>تم إضافة هدية الإحالة (+{REF_BONUS_DAYS} يوم) تلقائيًا.</i>" if applied else ""

@dp.callback_query(F.data.startswith("approve_inline:"))
async def cb_approve_inline(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    try:
        parts = q.data.split(":")
        if len(parts) != 3:
            return await q.answer("صيغة غير صحيحة.", show_alert=True)
        _, uid_str, plan = parts
        uid = int(uid_str)
        if plan not in ("2w", "4w", "gift1d"):
            return await q.answer("خطة غير صالحة.", show_alert=True)

        # تحديد المدة
        if plan == "2w":
            dur = SUB_DURATION_2W
        elif plan == "4w":
            dur = SUB_DURATION_4W
        else:
            dur = timedelta(hours=GIFT_ONE_DAY_HOURS)

        # تنفيذ التفعيل
        with get_session() as s:
            end_at = approve_paid(s, uid, plan, dur, tx_hash=None)
            bonus_applied = False
            if REFERRAL_ENABLED and plan in ("2w", "4w"):
                try:
                    res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                    bonus_applied = bool(res)
                except Exception as e:
                    logger.warning(f"apply_referral_bonus_if_eligible error: {e}")

        # إشعار لوحة الأدمن
        end_txt = (
            end_at.strftime('%Y-%m-%d %H:%M UTC') if isinstance(end_at, datetime)
            else str(end_at)
        )
        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{'1w' if plan=='2w' else plan}</b>.\n"
            f"صالح حتى: <code>{end_txt}</code>"
            f"{_bonus_applied_text(bonus_applied)}",
            parse_mode="HTML",
        )

        # إرسال الدعوة للمستخدم
        invite = await get_paid_invite_link(uid)
        try:
            if invite:
                title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" \
                        else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                msg = title
                if bonus_applied:
                    msg += f"\n\n🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)."
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
        parts = q.data.split(":")
        if len(parts) != 2:
            return await q.answer("صيغة غير صحيحة.", show_alert=True)
        _, uid_str = parts
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
        "🤖 <b>أوامر المستخدم</b>\n"
        "• <code>/start</code> – البداية والقائمة الرئيسية\n"
        "• <code>/trial</code> – تجربة مجانية ليوم\n"
        "• <code>/pricing</code> – عرض الأسعار الحالية\n"
        "• <code>/status</code> – حالة الاشتراك\n"
        "• <code>/ref</code> – رابط الإحالة وإحصاءاتك\n"
        "• <code>/use_ref CODE</code> – ربط كود إحالة يدويًا\n"
        "• <code>/submit_tx HASH PLAN</code> – إرسال هاش التحويل (1w/4w)\n"
        "• (زر) 🔑 طلب اشتراك — لإرسال طلب للأدمن\n\n"
        "📞 <b>تواصل خاص مع الأدمن</b>:\n" + _contact_line()
    )
    await m.answer(txt, parse_mode="HTML")

@dp.message(Command("pricing"))
async def cmd_pricing(m: Message):
    try:
        p1 = PRICE_2_WEEKS_USD
        p4 = PRICE_4_WEEKS_USD
    except Exception:
        p1 = p4 = "—"
    await m.answer(
        f"💳 <b>الأسعار</b>\n"
        f"• 1 أسبوع: <b>{p1}$</b>\n"
        f"• 4 أسابيع: <b>{p4}$</b>\n"
        "للتفعيل: اضغط زر «🔑 طلب اشتراك» من /start أو استخدم /submit_tx بعد التحويل.",
        parse_mode="HTML"
    )

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    txt_ok = "✅ <ب>اشتراكك نشط.</ب>\n🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
    txt_no = "❌ <b>لا تملك اشتراكًا نشطًا.</b>\n✨ اطلب التفعيل وسيقوم الأدمن بإتمامه."
    # إصلاح الوسم الغليظ إن تعرّض لخطأ (fallback)
    try:
        await m.answer(txt_ok if ok else txt_no, parse_mode="HTML")
    except Exception:
        await m.answer("✅ اشتراكك نشط." if ok else "❌ لا تملك اشتراكًا نشطًا.")

# ---------------------------
# Admin commands + Debug helpers
# ---------------------------

@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    if ADMIN_MINIMAL:
        txt = (
            "🛠️ <b>أوامر الأدمن (مختصرة)</b>\n"
            "• <code>/admin</code> – لوحة التفعيل السريعة\n"
            "• <code>/approve &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [ref]</code>\n"
            "• <code>/activate &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [ref]</code>\n"
            "• <code>/force_report</code> – إرسال التقرير اليومي الآن\n"
        )
    else:
        txt = (
            "🛠️ <b>أوامر الأدمن (كاملة)</b>\n"
            "• <code>/admin</code> – لوحة الأزرار\n"
            "• <code>/approve &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [reference]</code>\n"
            "• <code>/activate &lt;user_id&gt; &lt;2w|4w|gift1d&gt; [reference]</code>\n"
            "• <code>/broadcast &lt;text&gt;</code>\n"
            "• <code>/force_report</code>\n"
            "• <code>/gift1d &lt;user_id&gt;</code>\n"
            "• <code>/refstats &lt;user_id&gt;</code>\n"
            "• <code>/debug_sig SYMBOL</code>\n"
            "• <code>/relax_status</code>\n"
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

ADMIN_FLOW: Dict[int, Dict[str, Any]] = {}

@dp.callback_query(F.data == "admin_manual")
async def cb_admin_manual(q: CallbackQuery):
    aid = q.from_user.id
    if aid not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    ADMIN_FLOW[aid] = {"stage": "await_user"}
    await q.message.answer("أرسل الآن <code>user_id</code> للمستخدم الذي تريد تفعيله:", parse_mode="HTML")
    await q.answer()

# ⬇️ NEW: إلغاء جلسة الأدمن
@dp.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    ADMIN_FLOW.pop(q.from_user.id, None)
    try:
        await q.message.answer("❎ تم إلغاء العملية.", parse_mode="HTML")
    except Exception:
        pass
    await q.answer("تم.")

@dp.message(F.text)
async def admin_manual_router(m: Message):
    aid = m.from_user.id
    flow = ADMIN_FLOW.get(aid)
    if not flow or aid not in ADMIN_USER_IDS:
        return
    stage = flow.get("stage")

    if stage == "await_user":
        try:
            uid = int((m.text or "").strip())
            if uid <= 0:
                raise ValueError("bad uid")
            flow["uid"] = uid
            flow["stage"] = "await_plan"
            kb = InlineKeyboardBuilder()
            kb.button(text="تفعيل 1 أسبوع (1w)", callback_data="admin_plan:2w")
            kb.button(text="تفعيل 4 أسابيع (4w)", callback_data="admin_plan:4w")
            kb.button(text="🎁 تفعيل يوم مجاني (gift1d)", callback_data="admin_plan:gift1d")
            kb.button(text="إلغاء", callback_data="admin_cancel")
            kb.adjust(1)
            await m.answer(
                f"تم استلام user_id: <code>{uid}</code>\nاختر الخطة:",
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )
        except Exception:
            await m.answer("الرجاء إرسال رقم user_id صحيح (أرقام فقط).")
        return

    if stage == "await_plan":
        # الحماية – يجب اختيار الخطة من الأزرار
        return

    if stage == "await_ref":
        ref = (m.text or "").strip()
        if ref.lower() in ("/skip", "skip", "تخطي", "تخطى"):
            ref = None

        uid = flow.get("uid")
        plan = flow.get("plan")
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
                if REFERRAL_ENABLED and plan in ("2w", "4w"):
                    try:
                        res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                        bonus_applied = bool(res)
                    except Exception as e:
                        logger.warning(f"apply_referral_bonus_if_eligible error: {e}")

            ADMIN_FLOW.pop(aid, None)
            extra = _bonus_applied_text(bonus_applied)
            end_txt = end_at.strftime('%Y-%m-%d %H:%M UTC') if isinstance(end_at, datetime) else str(end_at)

            await m.answer(
                f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{'1w' if plan=='2w' else plan}</b>.\n"
                f"صالح حتى: <code>{end_txt}</code>{extra}",
                parse_mode="HTML",
            )

            # إرسال الدعوة
            invite = await get_paid_invite_link(uid)
            try:
                if invite:
                    title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                    msg = title + (f"\n\n🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)." if bonus_applied else "")
                    await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
                else:
                    await bot.send_message(
                        uid,
                        "✅ تم التفعيل. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.",
                        parse_mode="HTML",
                    )
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
        "أرسل رقم مرجع (اختياري) لإرفاقه بالإيصال.\n"
        "أو اضغط «تخطي المرجع».",
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

    uid = flow.get("uid")
    plan = flow.get("plan")
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
            if REFERRAL_ENABLED and plan in ("2w", "4w"):
                try:
                    res = apply_referral_bonus_if_eligible(s, uid, bonus_days=REF_BONUS_DAYS)
                    bonus_applied = bool(res)
                except Exception as e:
                    logger.warning(f"apply_referral_bonus_if_eligible error: {e}")

        ADMIN_FLOW.pop(aid, None)
        extra = _bonus_applied_text(bonus_applied)
        end_txt = end_at.strftime('%Y-%m-%d %H:%M UTC') if isinstance(end_at, datetime) else str(end_at)

        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{'1w' if plan=='2w' else plan}</b>.\n"
            f"صالح حتى: <code>{end_txt}</code>{extra}",
            parse_mode="HTML",
        )

        # إرسال الدعوة
        invite = await get_paid_invite_link(uid)
        try:
            if invite:
                title = "🎁 تم تفعيل يوم مجاني إضافي! ادخل القناة:" if plan == "gift1d" else "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:"
                msg = title + (f"\n\n🎉 تمت إضافة مكافأة إحالة (+{REF_BONUS_DAYS} يوم)." if bonus_applied else "")
                await bot.send_message(uid, msg, reply_markup=invite_kb(invite))
            else:
                await bot.send_message(
                    uid,
                    "✅ تم التفعيل. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning(f"USER DM ERROR: {e}")
    except Exception as e:
        ADMIN_FLOW.pop(aid, None)
        await q.message.answer(f"❌ فشل التفعيل: {e}")
    await q.answer("تم.")

# ---- Admin debug helpers ----

@dp.message(Command("debug_sig"))
async def cmd_debug_sig(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    sym = (command.args or "").strip()
    if not sym:
        return await m.answer("استخدم: /debug_sig SYMBOL\nمثال: /debug_sig BTC/USDT")
    await m.answer(f"⏳ فحص {sym} …")
    try:
        data = await fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=300)
        if not data:
            return await m.answer("لم أستطع جلب OHLCV.")
        htf = await fetch_ohlcv_htf(sym)
        sig = check_signal(sym, data, htf if htf else None)
        if sig:
            txt = format_signal_text_basic(sig)
            return await m.answer("✅ إشارة متاحة:\n\n" + txt, parse_mode="HTML", disable_web_page_preview=True)
        # لا توجد إشارة: اعرض مقاييس مفيدة
        import pandas as pd
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        close = float(df.iloc[-2]["close"]); vol = float(df.iloc[-2]["volume"])
        vma20 = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-2])
        rvol = (vol / vma20) if vma20 > 0 else 0.0
        await m.answer(
            "ℹ️ لا توجد إشارة الآن.\n"
            f"• السعر: <code>{_fmt_price(close)}</code>\n"
            f"• RVOL≈ <b>{rvol:.2f}</b>\n"
            f"• إطارات HTF المحمّلة: <b>{', '.join(htf.keys()) if htf else '—'}</b>\n"
            "جرّب لاحقًا أو رموزًا أخرى.",
            parse_mode="HTML"
        )
    except Exception as e:
        await m.answer(f"خطأ أثناء الفحص: <code>{_h(str(e))}</code>", parse_mode="HTML")

@dp.message(Command("relax_status"))
async def cmd_relax(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    try:
        st_path = os.getenv("STRATEGY_STATE_FILE", "strategy_state.json")
        last_ts = (json.loads(Path(st_path).read_text(encoding="utf-8")).get("last_signal_ts") or 0)
        h1 = int(os.getenv("AUTO_RELAX_AFTER_HRS_1", "24"))
        h2 = int(os.getenv("AUTO_RELAX_AFTER_HRS_2", "48"))
        hours = 1e9 if not last_ts else max(0, (time.time() - float(last_ts)) / 3600.0)
        lvl = 2 if hours >= h2 else (1 if hours >= h1 else 0)
        await m.answer(f"Auto-Relax: L{lvl} | منذ آخر إشارة: {hours:.1f} ساعة")
    except Exception as e:
        await m.answer(f"تعذر قراءة الحالة: {e}")

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
    holder = f"{os.getenv('SERVICE_NAME', 'svc')}:{os.getpid()}"

    def _on_sigterm(*_):
        try:
            logger.info("SIGTERM received → releasing leader lock and shutting down…")
            if os.getenv("ENABLE_DB_LOCK", "1") != "0":
                try:
                    from database import release_leader_lock
                    release_leader_lock(os.getenv("LEADER_LOCK_NAME", "telebot_poller"), holder)
                except Exception as e:
                    logger.warning(f"release_leader_lock on SIGTERM warn: {e}")
        except Exception:
            pass
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        pass

    # Leader lock (compat shim)
    ENABLE_DB_LOCK = os.getenv("ENABLE_DB_LOCK", "1") != "0"
    LEADER_TTL = int(os.getenv("LEADER_TTL", "300"))
    LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
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

    # ✅ تحميل أسواق OKX عبر اللودر الآمن ثم تهيئة AVAILABLE_SYMBOLS
    await load_okx_markets_and_filter()

    # بناء أولي بالقائمة الحالية من symbols.py (مع الميتا) — يستخدم تكييف السواب داخليًا
    try:
        await rebuild_available_symbols((SYMBOLS, getattr(symbols_mod, "SYMBOLS_META", {}) or {}))
    except Exception as e:
        logger.warning(f"init rebuild_available_symbols warn: {e}")

    # حذف أي Webhook سابق قبل polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    await check_channel_and_admin_dm()

    # مهام الخلفية
    t1 = asyncio.create_task(resilient_polling())
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(monitor_open_trades())
    t5 = asyncio.create_task(kick_expired_members_loop())
    t6 = asyncio.create_task(notify_trial_expiring_soon_loop())
    t_symbols = asyncio.create_task(refresh_symbols_periodically())  # NEW: تحديث الرموز كل 4 ساعات

    try:
        await asyncio.gather(t1, t2, t3, t4, t5, t6, t_symbols)
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
        if ENABLE_DB_LOCK and 'release_leader_lock' in globals() and release_leader_lock:
            try:
                release_leader_lock(LEADER_LOCK_NAME, holder)
                logger.info("Leader lock released.")
            except Exception:
                pass
        if hb_task:
            hb_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())

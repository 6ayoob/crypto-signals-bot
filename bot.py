# bot.py — تشغيل Aiogram v3 | تفعيل يدوي + دعوة قناة + تجربة يومية + تقارير + مخاطر + قفل قائد
# الجديد:
# - زر "🔑 طلب اشتراك": يرسل إشعارًا للأدمن + يرسل للمستخدم عنوان المحفظة والانتقال لمراسلة الأدمن.
# - بعد أي تفعيل (Approve): إرسال رابط دعوة للقناة تلقائيًا (توليد/ثابت).
# - إضافة أمر /trial بجانب زر التجربة في /start، ومعه إرسال رابط القناة عند النجاح.
# - لا يوجد دفع أوتوماتيكي/TxID.
# المتطلبات: اجعل البوت "مشرفًا" في القناة ليتمكن من create_chat_invite_link، أو وفّر CHANNEL_INVITE_LINK ثابت.

import asyncio
import json
import hashlib
import logging
import os
import sys
import time
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

# ====== قفل ملف محلي لمنع نسختين على نفس الجهاز ======
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

# تلغرام Conflict (قد لا يتوفر في بعض الإصدارات)
try:
    from aiogram.exceptions import TelegramConflictError
except Exception:
    class TelegramConflictError(Exception): ...

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD,  # اختياري للعرض
    SUB_DURATION_2W, SUB_DURATION_4W,      # إلزامي للتفعيل
    USDT_TRC20_WALLET                      # اختياري للعرض في الرسائل
)

# قاعدة البيانات
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, close_trade,
    add_trade_sig, has_open_trade_on_symbol,
    get_stats_24h, get_stats_7d, User, Trade
)

# ---------- Leader Lock ----------
ENABLE_DB_LOCK = os.getenv("ENABLE_DB_LOCK", "1") != "0"
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL = int(os.getenv("LEADER_TTL", "300"))  # ثواني
HEARTBEAT_INTERVAL = max(10, LEADER_TTL // 2)

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
# -----------------------------------

from strategy import check_signal
from symbols import SYMBOLS

# ---------------------------
# إعدادات عامة
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# OKX
exchange = ccxt.okx({"enableRateLimit": True})
AVAILABLE_SYMBOLS: List[str] = []

# ==== Rate Limiter لواجهات OKX العامة ====
OKX_PUBLIC_MAX = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))      # طلبات لكل نافذة
OKX_PUBLIC_WIN = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))  # مدة النافذة بالثواني
class SlidingRateLimiter:
    def __init__(self, max_calls: int, window_sec: float):
        self.max_calls = max_calls
        self.window = window_sec
        self.calls = deque()
        self._lock = asyncio.Lock()
    async def wait(self):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            while self.calls and (now - self.calls[0]) > self.window:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                sleep_for = self.window - (now - self.calls[0]) + 0.05
                await asyncio.sleep(max(sleep_for, 0.05))
                return await self.wait()
            self.calls.append(now)
RATE = SlidingRateLimiter(OKX_PUBLIC_MAX, OKX_PUBLIC_WIN)

# جداول المسح
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))
MONITOR_INTERVAL_SEC = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")

# ضبط التوازي والدفعات لمسح الشموع
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))

# مخاطر V2
RISK_STATE_FILE = Path("risk_state.json")
MAX_DAILY_LOSS_R = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "6"))
AUDIT_IDS: Dict[int, str] = {}

# Dedupe نافذة لمنع تكرار الإشارات المتقاربة
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))
_LAST_SIGNAL_AT: Dict[str, float] = {}

# ===== دعم تواصل خاص مع الأدمن =====
SUPPORT_CHAT_ID: Optional[int] = int(os.getenv("SUPPORT_CHAT_ID")) if os.getenv("SUPPORT_CHAT_ID") else None
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME")  # اسم المستخدم بدون @ لزر الخاص

# ===== روابط دعوة القناة =====
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")  # إن وفّرت رابطًا ثابتًا
async def get_channel_invite_link() -> Optional[str]:
    # 1) إن وُجد رابط ثابت في .env استخدمه
    if CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    # 2) توليد رابط جديد (يتطلب أن يكون البوت مشرفًا في القناة)
    try:
        inv = await bot.create_chat_invite_link(TELEGRAM_CHANNEL_ID, creates_join_request=False)
        return inv.invite_link
    except Exception as e:
        logger.warning(f"INVITE_LINK create failed: {e}")
        return None

def invite_kb(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 ادخل القناة الآن", url=url)
    return kb.as_markup()

# ====== تدفق تفعيل يدوي للأدمن ======
ADMIN_FLOW: Dict[int, Dict[str, Any]] = {}  # {admin_id: {'stage': 'await_user'|'await_plan'|'await_ref', 'uid': int, 'plan': '2w'|'4w'}}

# ---------------------------
# أدوات مساعدة
# ---------------------------
def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
    try:
        with get_session() as s:
            now = datetime.now(timezone.utc)
            rows = s.query(User.tg_user_id).filter(User.end_at != None, User.end_at > now).all()  # noqa: E711
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

async def welcome_text() -> str:
    price_line = ""
    try:
        price_line = f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b> | • 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
    except Exception:
        pass
    wallet_line = ""
    try:
        if USDT_TRC20_WALLET:
            wallet_line = f"محفظة USDT (TRC20): <code>{_h(USDT_TRC20_WALLET)}</code>\n"
    except Exception:
        pass
    return (
        "👋 أهلاً بك في <b>عالم الفرص</b>\n\n"
        "🔔 إشارات لحظية + تقرير يومي + إدارة مخاطر.\n"
        f"🕘 التقرير اليومي: <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)\n\n"
        "خطط الاشتراك:\n"
        f"{price_line}"
        "للاشتراك: اضغط <b>«🔑 طلب اشتراك»</b> وسيصل طلبك للأدمن لتفعيلك لمدة 2 أسابيع أو 4 أسابيع.\n\n"
        "✨ جرّب الإصدار الكامل مجانًا لمدة <b>يوم واحد</b>.\n\n"
        f"{wallet_line}"
        "📞 تواصل مباشر مع الأدمن:\n" + _contact_line()
    )

# ===== زر مراسلة الأدمن (خاص) =====
def support_dm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        kb.button(text="💬 مراسلة الأدمن (خاص)", url=f"https://t.me/{SUPPORT_USERNAME}")
    elif SUPPORT_CHAT_ID:
        kb.button(text="💬 مراسلة الأدمن (خاص)", url=f"tg://user?id={SUPPORT_CHAT_ID}")
    return kb.as_markup()

# ===== تنسيق رسائل الإشارة/الإغلاق =====
def format_signal_text_basic(sig: dict) -> str:
    extra = ""
    if "score" in sig or "regime" in sig:
        extra = f"\n📊 Score: <b>{sig.get('score','-')}</b> | Regime: <b>{_h(sig.get('regime','-'))}</b>"
        if sig.get("reasons"):
            extra += f"\n🧠 أسباب مختصرة: <i>{_h(', '.join(sig['reasons'][:6]))}</i>"
    return (
        "🚀 <b>إشارة شراء جديدة!</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(sig['symbol'])}</b>\n"
        f"💵 الدخول: <code>{sig['entry']}</code>\n"
        f"📉 الوقف: <code>{sig['sl']}</code>\n"
        f"🎯 الهدف 1: <code>{sig['tp1']}</code>\n"
        f"🏁 الهدف 2: <code>{sig['tp2']}</code>\n"
        f"⏰ (UTC): <code>{_h(sig['timestamp'])}</code>"
        f"{extra}\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ <i>التزم بالخطة: مخاطرة ثابتة، لا تلحق بالسعر، والتزم بالوقف.</i>"
    )

def format_close_text(t: Trade, r_multiple: float | None = None) -> str:
    emoji = {"tp1": "🎯", "tp2": "🏆", "sl": "🛑"}.get(t.result or "", "ℹ️")
    result_label = {"tp1": "تحقق الهدف 1 — خطوة ممتازة!", "tp2": "تحقق الهدف 2 — إنجاز رائع!", "sl": "وقف الخسارة — حماية رأس المال"}.get(t.result or "", "إغلاق")
    r_line = f"\n📐 R: <b>{round(r_multiple, 3)}</b>" if r_multiple is not None else ""
    tip = "🔁 نبحث عن فرصة أقوى تالية… الصبر مكسب." if (t.result == "sl") else "🎯 إدارة الربح أهم من كثرة الصفقات."
    return (
        f"{emoji} <b>إغلاق صفقة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الأصل: <b>{_h(t.symbol)}</b>\n"
        f"💵 الدخول: <code>{t.entry}</code>\n"
        f"📉 الوقف: <code>{t.sl}</code>\n"
        f"🎯 TP1: <code>{t.tp1}</code> | 🏁 TP2: <code>{t.tp2}</code>\n"
        f"📌 النتيجة: <b>{result_label}</b>{r_line}\n"
        f"⏰ الإغلاق (UTC): <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{tip}"
    )

# ---------------------------
# إدارة المخاطر
# ---------------------------
def _load_risk_state() -> dict:
    try:
        if RISK_STATE_FILE.exists():
            return json.loads(RISK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"RISK_STATE load warn: {e}")
    return {"date": datetime.now(timezone.utc).date().isoformat(),
            "r_today": 0.0, "loss_streak": 0, "cooldown_until": None}

def _save_risk_state(state: dict):
    try:
        RISK_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"RISK_STATE save warn: {e}")

def _reset_if_new_day(state: dict) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("date") != today:
        state.update({"date": today, "r_today": 0.0, "loss_streak": 0, "cooldown_until": None})
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
        cooldown_reason = (cooldown_reason + " + " if cooldown_reason else "") + f"{MAX_LOSSES_STREAK} خسائر متتالية"

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
        mkts = set(exchange.markets.keys())
        filtered = [s for s in SYMBOLS if s in mkts]
        skipped = [s for s in SYMBOLS if s not in mkts]
        AVAILABLE_SYMBOLS = filtered
        logger.info(f"OKX markets loaded. Using {len(filtered)} symbols, skipped {len(skipped)}: {skipped}")
    except Exception as e:
        logger.exception(f"load_okx_markets error: {e}")
        AVAILABLE_SYMBOLS = []

# ---------------------------
# جلب البيانات/الأسعار
# ---------------------------
async def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=300):
    for attempt in range(4):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            )
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
        except Exception as e:
            logger.warning(f"FETCH_OHLCV ERROR {symbol}: {e}")
            return []
    return []

async def fetch_ticker_price(symbol: str) -> float | None:
    for attempt in range(3):
        try:
            await RATE.wait()
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, lambda: exchange.fetch_ticker(symbol))
            price = ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("last")
            return float(price) if price is not None else None
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as e:
            logger.warning(f"FETCH_TICKER ERROR {symbol}: {e}")
            return None
    return None

# ---------------------------
# Dedupe: منع تكرار الإشارات
# ---------------------------
_last_signal_at: Dict[str, float] = {}
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
# فحص الإشارات (Batch + Concurrency)
# ---------------------------
SCAN_LOCK = asyncio.Lock()

async def _send_signal_to_channel(sig: dict, audit_id: str | None) -> None:
    await send_channel(format_signal_text_basic(sig))

async def _scan_one_symbol(sym: str) -> Optional[dict]:
    data = await fetch_ohlcv(sym)
    if not data:
        return None
    sig = check_signal(sym, data)
    if not sig:
        return None
    return sig

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
                    logger.warning(f"scan symbol error {sym}: {e}")
                    return None

        for i in range(0, len(AVAILABLE_SYMBOLS), SCAN_BATCH_SIZE):
            batch = AVAILABLE_SYMBOLS[i:i+SCAN_BATCH_SIZE]
            sigs = await asyncio.gather(*[ _guarded_scan(s) for s in batch ])
            for sig in filter(None, sigs):
                if _should_skip_duplicate(sig):
                    logger.info(f"DEDUPE SKIP {sig['symbol']}")
                    continue

                with get_session() as s:
                    allowed, reason = can_open_new_trade(s)
                    if not allowed:
                        logger.info(f"SKIP SIGNAL {sig['symbol']}: {reason}")
                        continue
                    try:
                        if has_open_trade_on_symbol(s, sig["symbol"]):
                            logger.info(f"SKIP {sig['symbol']}: already open position")
                            continue
                    except Exception:
                        pass

                    audit_id = _make_audit_id(sig["symbol"], sig["entry"], sig.get("score", 0))
                    try:
                        trade_id = add_trade_sig(s, sig, audit_id=audit_id, qty=None)
                    except Exception as e:
                        logger.exception(f"add_trade_sig error, fallback to add_trade: {e}")
                        trade_id = add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

                    AUDIT_IDS[trade_id] = audit_id

                try:
                    await _send_signal_to_channel(sig, audit_id)
                    note = (
                        "🚀 <b>إشارة جديدة وصلت!</b>\n"
                        "🔔 الهدوء أفضل من مطاردة الشمعة — التزم بالخطة."
                    )
                    uids = list_active_user_ids()
                    for uid in uids:
                        try:
                            await bot.send_message(uid, note, parse_mode="HTML", disable_web_page_preview=True)
                            await asyncio.sleep(0.02)
                        except Exception:
                            pass
                    logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']} audit={audit_id}")
                except Exception as e:
                    logger.exception(f"SEND SIGNAL ERROR: {e}")

                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)

async def loop_signals():
    while True:
        started = time.time()
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        elapsed = time.time() - started
        sleep_for = max(1.0, SIGNAL_SCAN_INTERVAL_SEC - elapsed)
        await asyncio.sleep(sleep_for)

# ---------------------------
# مراقبة الصفقات
# ---------------------------
async def monitor_open_trades():
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
                    hit_sl  = price <= t.sl

                    result, exit_px = None, None
                    if hit_tp2: result, exit_px = "tp2", float(t.tp2)
                    elif hit_tp1: result, exit_px = "tp1", float(t.tp1)
                    elif hit_sl:  result, exit_px = "sl",  float(t.sl)

                    if not result:
                        continue

                    r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                    try:
                        close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                    except Exception as e:
                        logger.warning(f"close_trade warn: {e}")

                    msg = format_close_text(t, r_multiple)
                    msg += "\n💡 <i>الانضباط مع الوقف والأهداف يصنع الفرق على المدى الطويل.</i>"
                    await notify_subscribers(msg)
                    await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"MONITOR ERROR: {e}")
        await asyncio.sleep(MONITOR_INTERVAL_SEC)

# ---------------------------
# التقرير اليومي
# ---------------------------
def _report_card(stats_24: dict, stats_7d: dict) -> str:
    return (
        "📊 <b>التقرير اليومي — لقطة أداء مركّزة</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>آخر 24 ساعة</b>\n"
        f"• إشارات: <b>{stats_24['signals']}</b> | صفقات مفتوحة الآن: <b>{stats_24['open']}</b>\n"
        f"• محقق من الأهداف: <b>{stats_24['tp_total']}</b> (TP1: {stats_24['tp1']} | TP2: {stats_24['tp2']})\n"
        f"• وقف خسارة: <b>{stats_24['sl']}</b>\n"
        f"• معدل نجاح: <b>{stats_24['win_rate']}%</b>\n"
        f"• صافي R تقريبي: <b>{stats_24['r_sum']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>آخر 7 أيام</b>\n"
        f"• إشارات: <b>{stats_7d['signals']}</b> | أهداف: <b>{stats_7d['tp_total']}</b> | SL: <b>{stats_7d['sl']}</b>\n"
        f"• معدل نجاح أسبوعي: <b>{stats_7d['win_rate']}%</b> | صافي R: <b>{stats_7d['r_sum']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "💡 <i>الخطة أهم من الضجيج: مخاطرة ثابتة + التزام بالأهداف.</i>"
    )

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
# أوامر المستخدم
# ---------------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 طلب اشتراك", callback_data="req_sub")
    kb.button(text="✨ ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="🧾 حالة اشتراكي", callback_data="status_btn")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="HTML", reply_markup=kb.as_markup())
    if SUPPORT_USERNAME or SUPPORT_CHAT_ID:
        await m.answer("تحتاج مساعدة؟ راسل الأدمن مباشرة:", reply_markup=support_dm_kb())

@dp.callback_query(F.data == "status_btn")
async def cb_status_btn(q: CallbackQuery):
    with get_session() as s:
        ok = is_active(s, q.from_user.id)
    await q.message.answer(
        "✅ <b>اشتراكك نشط.</b>\n🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
        if ok else
        "❌ <b>لا تملك اشتراكًا نشطًا.</b>\n✨ اطلب التفعيل وسيقوم الأدمن بإتمامه.",
        parse_mode="HTML"
    )
    await q.answer()

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)
    if ok:
        await q.message.answer(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁\n"
            "🚀 استمتع بالإشارات والتقرير اليومي.", parse_mode="HTML")
        # إرسال دعوة للقناة
        invite = await get_channel_invite_link()
        if invite:
            try:
                await bot.send_message(q.from_user.id, "📣 ادخل القناة الآن:", reply_markup=invite_kb(invite))
            except Exception as e:
                logger.warning(f"SEND INVITE(TRIAL) ERROR: {e}")
    else:
        await q.message.answer(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\n"
            "✨ يمكنك طلب الاشتراك الآن وسيقوم الأدمن بالتفعيل.", parse_mode="HTML")
    await q.answer()

# أمر نصّي للتجربة (إضافة على الزر)
@dp.message(Command("trial"))
async def cmd_trial(m: Message):
    with get_session() as s:
        ok = start_trial(s, m.from_user.id)
    if ok:
        await m.answer("✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁", parse_mode="HTML")
        invite = await get_channel_invite_link()
        if invite:
            try:
                await bot.send_message(m.from_user.id, "📣 ادخل القناة الآن:", reply_markup=invite_kb(invite))
            except Exception as e:
                logger.warning(f"SEND INVITE(TRIAL CMD) ERROR: {e}")
    else:
        await m.answer("ℹ️ لقد استخدمت التجربة المجانية مسبقًا.", parse_mode="HTML")

# === زر "طلب اشتراك" للمستخدم → إشعار للأدمن + إرشاد دفع للمستخدم ===
@dp.callback_query(F.data == "req_sub")
async def cb_req_sub(q: CallbackQuery):
    u = q.from_user
    uid = u.id
    uname = (u.username and f"@{u.username}") or (u.full_name or "")
    user_line = f"{_h(uname)} (ID: <code>{uid}</code>)"

    # إشعار للأدمن مع أزرار الموافقة/الرفض
    kb_admin = InlineKeyboardBuilder()
    kb_admin.button(text="✅ تفعيل 2 أسابيع (2w)", callback_data=f"approve_inline:{uid}:2w")
    kb_admin.button(text="✅ تفعيل 4 أسابيع (4w)", callback_data=f"approve_inline:{uid}:4w")
    kb_admin.button(text="❌ رفض", callback_data=f"reject_inline:{uid}")
    kb_admin.adjust(1)
    await send_admins(
        "🔔 <b>طلب اشتراك جديد</b>\n"
        f"المستخدم: {user_line}\n"
        "اختر نوع التفعيل:",
        reply_markup=kb_admin.as_markup()
    )

    # رسالة للمستخدم: الأسعار + عنوان المحفظة + زر مراسلة الأدمن
    price_line = ""
    try:
        price_line = f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b> | • 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
    except Exception:
        pass
    wallet_line = ""
    try:
        if USDT_TRC20_WALLET:
            wallet_line = f"💳 محفظة USDT (TRC20):\n<code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
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
        reply_markup=support_dm_kb() if (SUPPORT_USERNAME or SUPPORT_CHAT_ID) else None
    )
    await q.answer()

# موافقات الأدمن السريعة من إشعار "طلب اشتراك"
@dp.callback_query(F.data.startswith("approve_inline:"))
async def cb_approve_inline(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    try:
        _, uid_str, plan = q.data.split(":")
        uid = int(uid_str)
        if plan not in ("2w", "4w"):
            return await q.answer("خطة غير صالحة.", show_alert=True)
        dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
        with get_session() as s:
            end_at = approve_paid(s, uid, plan, dur, tx_hash=None)
        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
            f"\nصالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>",
            parse_mode="HTML"
        )
        # إرسال رابط دعوة للمستخدم
        invite = await get_channel_invite_link()
        try:
            if invite:
                await bot.send_message(uid, "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:", reply_markup=invite_kb(invite))
            else:
                await bot.send_message(uid, "✅ تم تفعيل اشتراكك. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.", parse_mode="HTML")
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

@dp.message(Command("help"))
async def cmd_help(m: Message):
    text = (
        "🤖 <b>أوامر المستخدم</b>\n"
        "• <code>/start</code> – البداية والقائمة الرئيسية\n"
        "• <code>/trial</code> – تجربة مجانية ليوم\n"
        "• <code>/status</code> – حالة الاشتراك\n"
        "• (زر) 🔑 طلب اشتراك — لإرسال طلب للأدمن\n\n"
        "📞 <b>تواصل خاص مع الأدمن</b>:\n" + _contact_line()
    )
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer(
        "✅ <b>اشتراكك نشط.</b>\n🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
        if ok else
        "❌ <b>لا تملك اشتراكًا نشطًا.</b>\n✨ اطلب التفعيل وسيقوم الأدمن بإتمامه.",
        parse_mode="HTML"
    )

# ---------------------------
# أوامر الأدمن
# ---------------------------
@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    txt = (
        "🛠️ <b>أوامر الأدمن</b>\n"
        "• <code>/admin</code> – لوحة الأزرار\n"
        "• <code>/approve &lt;user_id&gt; &lt;2w|4w&gt; [reference]</code> – تفعيل مباشر\n"
        "• <code>/activate &lt;user_id&gt; &lt;2w|4w&gt; [reference]</code> – مرادف لـ /approve\n"
        "• <code>/broadcast &lt;text&gt;</code> – رسالة جماعية للمشتركين النشطين\n"
        "• <code>/force_report</code> – إرسال التقرير اليومي الآن"
    )
    await m.answer(txt, parse_mode="HTML")

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ تفعيل اشتراك يدوي (إدخال user_id)", callback_data="admin_manual")
    kb.button(text="ℹ️ أوامر الأدمن", callback_data="admin_help_btn")
    kb.adjust(1)
    await m.answer("لوحة الأدمن:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "admin_help_btn")
async def cb_admin_help_btn(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS: return await q.answer()
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

@dp.message(F.text)
async def admin_manual_router(m: Message):
    """تدفق الإدخال للأدمن: user_id -> اختيار الخطة -> (مرجع اختياري) -> تفعيل + إرسال دعوة."""
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
            kb.button(text="إلغاء", callback_data="admin_cancel")
            kb.adjust(1)
            await m.answer(f"تم استلام user_id: <code>{uid}</code>\nاختر الخطة:", parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await m.answer("الرجاء إرسال رقم user_id صحيح (أرقام فقط).")
        return

    if stage == "await_ref":
        ref = m.text.strip()
        if ref.lower() in ("/skip", "skip", "تخطي", "تخطى"):
            ref = None
        uid = flow.get("uid")
        plan = flow.get("plan")
        dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
        try:
            with get_session() as s:
                end_at = approve_paid(s, uid, plan, dur, tx_hash=ref)
            ADMIN_FLOW.pop(aid, None)
            await m.answer(
                f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
                f"\nصالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>",
                parse_mode="HTML"
            )
            # إرسال الدعوة
            invite = await get_channel_invite_link()
            try:
                if invite:
                    await bot.send_message(uid, "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:", reply_markup=invite_kb(invite))
                else:
                    await bot.send_message(uid, "✅ تم تفعيل اشتراكك. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.", parse_mode="HTML")
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
    if plan not in ("2w", "4w"):
        return await q.answer("خطة غير صالحة.", show_alert=True)

    flow["plan"] = plan
    flow["stage"] = "await_ref"

    kb = InlineKeyboardBuilder()
    kb.button(text="تخطي المرجع", callback_data="admin_skip_ref")
    kb.button(text="إلغاء", callback_data="admin_cancel")
    kb.adjust(1)

    await q.message.answer(
        "أرسل رقم مرجع (اختياري) لإرفاقه بالإيصال.\n"
        "أو اضغط «تخطي المرجع».", reply_markup=kb.as_markup()
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
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    try:
        with get_session() as s:
            end_at = approve_paid(s, uid, plan, dur, tx_hash=None)
        ADMIN_FLOW.pop(aid, None)
        await q.message.answer(
            f"✅ تم التفعيل للمستخدم <code>{uid}</code> بخطة <b>{plan}</b>."
            f"\nصالح حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>",
            parse_mode="HTML"
        )
        # إرسال الدعوة
        invite = await get_channel_invite_link()
        try:
            if invite:
                await bot.send_message(uid, "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:", reply_markup=invite_kb(invite))
            else:
                await bot.send_message(uid, "✅ تم تفعيل اشتراكك. لم أستطع توليد رابط الدعوة تلقائيًا — راسل الأدمن للحصول على الرابط.", parse_mode="HTML")
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

# مرادف للأمر /approve
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    parts = (m.text or "").strip().split()
    if len(parts) not in (3, 4) or parts[2] not in ("2w", "4w"):
        return await m.answer("استخدم: /approve <user_id> <2w|4w> [reference]")
    uid = int(parts[1]); plan = parts[2]
    txh = parts[3] if len(parts) == 4 else None
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
    await m.answer(f"تم التفعيل للمستخدم {uid}. صالح حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}.")
    # إرسال دعوة
    invite = await get_channel_invite_link()
    if invite:
        try:
            await bot.send_message(uid, "✅ تم تفعيل اشتراكك. اضغط للدخول إلى القناة:", reply_markup=invite_kb(invite))
        except Exception as e:
            logger.warning(f"SEND INVITE ERROR (/approve): {e}")

@dp.message(Command("activate"))
async def cmd_activate(m: Message):
    # مرادف لـ /approve
    await cmd_approve(m)

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    txt = m.text.partition(" ")[2].strip()
    if not txt: return await m.answer("استخدم: /broadcast <text>")
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
    if m.from_user.id not in ADMIN_USER_IDS: return
    with get_session() as s:
        stats_24 = get_stats_24h(s); stats_7d = get_stats_7d(s)
    await send_channel(_report_card(stats_24, stats_7d))
    await m.answer("تم إرسال التقرير للقناة.")

# ---------------------------
# فحوصات التشغيل
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

# ---------------------------
# التشغيل
# ---------------------------
async def main():
    init_db()

    hb_task = None
    holder = f"{SERVICE_NAME}:{os.getpid()}"
    if ENABLE_DB_LOCK and acquire_or_steal_leader_lock:
        ok = acquire_or_steal_leader_lock(LEADER_LOCK_NAME, holder, ttl_seconds=LEADER_TTL)
        if not ok:
            logger.error("Another instance holds the leader DB lock. Exiting.")
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
                await asyncio.sleep( max(10, LEADER_TTL // 2) )
        hb_task = asyncio.create_task(_leader_heartbeat_task(LEADER_LOCK_NAME, holder))

    await load_okx_markets_and_filter()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    await check_channel_and_admin_dm()

    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(monitor_open_trades())

    try:
        await asyncio.gather(t1, t2, t3, t4)
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
            except Exception:
                pass
        if hb_task:
            hb_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())

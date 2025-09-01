# bot.py — مُشغِّل البوت (Aiogram v3) مع OKX + اشتراكات + TRC20 + تقارير + مخاطر V2
# تحسينات: منع تكرار الإشارات (Dedupe)، تسريع فحص الشموع (Batch + Concurrency)،
# إصلاح إغلاق الصفقة (استدعاء واحد)، إعادة محاولة للتقرير اليومي، لوج أوضح، لمسات استقرار.
# جديد: صورة دليل الدفع للمشترك + لوحة تفعيل يدوي للأدمن بخطوات سهلة.
# مضاف حديثًا: Rate Limiter لطلبات OKX لمنع 50011 (Too Many Requests)
# + زر "💬 مراسلة الدعم (خاص)" يفتح الخاص مباشرة + رسائل تحفيزية للمشتركين.

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
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS, USDT_TRC20_WALLET,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD, SUB_DURATION_2W, SUB_DURATION_4W
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

# الدفع TRON
from payments_tron import extract_txid, find_trc20_transfer_to_me, REFERENCE_HINT

# Trust Layer (اختياري)
try:
    from trust_layer import format_signal_card, log_signal, log_close, make_audit_id
    TRUST_LAYER = True
except Exception:
    TRUST_LAYER = False

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
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "10"))   # 10 رموز بالدفعة
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))    # 5 مهام fetch متزامنة كحد أقصى

# مخاطر V2
RISK_STATE_FILE = Path("risk_state.json")
MAX_DAILY_LOSS_R = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "6"))
AUDIT_IDS: Dict[int, str] = {}

# Dedupe نافذة لمنع تكرار الإشارات المتقاربة
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))
_LAST_SIGNAL_AT: Dict[str, float] = {}  # key=symbol, value=unix_ts

# دعم/تواصل
SUPPORT_CHAT_ID: Optional[int] = int(os.getenv("SUPPORT_CHAT_ID")) if os.getenv("SUPPORT_CHAT_ID") else None
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME")  # اسم المستخدم بدون @ لزر الخاص
SUPPORT_WAIT: Dict[int, float] = {}
ADMIN_REPLY_TARGET: Dict[int, int] = {}
SUPPORT_WAIT_MINUTES = int(os.getenv("SUPPORT_WAIT_MINUTES", "10"))

# ====== إعدادات صورة دليل الدفع (اختياري) ======
PAY_GUIDE_FILE_ID = os.getenv("PAY_GUIDE_FILE_ID")      # إن وُجد file_id لصورة سبق رفعها
PAY_GUIDE_URL = os.getenv("PAY_GUIDE_URL")              # أو رابط مباشر للصورة
PAY_GUIDE_LOCAL_PATH = os.getenv("PAY_GUIDE_LOCAL_PATH")# أو مسار محلي داخل المشروع (assets/payment_guide.jpg)

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

def _now_ts() -> float:
    return time.time()

def _support_set(uid: int):
    SUPPORT_WAIT[uid] = _now_ts() + SUPPORT_WAIT_MINUTES * 60

def _support_clear(uid: int):
    SUPPORT_WAIT.pop(uid, None)

def _support_waiting(uid: int) -> bool:
    exp = SUPPORT_WAIT.get(uid)
    if not exp:
        return False
    if _now_ts() > exp:
        SUPPORT_WAIT.pop(uid, None)
        return False
    return True

async def send_channel(text: str):
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def send_admins(text: str):
    if SUPPORT_CHAT_ID:
        try:
            await bot.send_message(SUPPORT_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"SUPPORT_CHAT notify error: {e}")
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", disable_web_page_preview=True)
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
            await asyncio.sleep(0.02)  # rate-limit لطيف
        except Exception:
            pass

# ===== رسائل ترحيب/دفع تحفيزية =====
async def welcome_text() -> str:
    return (
        "👋 أهلاً بك في <b>عالم الفرص</b> — حيث تُلتقط الحركات القوية قبل أن يشاهدها الجميع!\n\n"
        "🔔 إشارات لحظية مبنية على منهجية صارمة (Score + Regime + إدارة مخاطر)\n"
        f"🕘 تقرير يومي الساعة <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)\n"
        "💰 استراتيجيتنا تركز على <b>حماية رأس المال أولاً</b> ثم تعظيم العائد.\n\n"
        "خطط الاشتراك:\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b> | • 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
        f"محفظة USDT (TRC20): <code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
        "✨ جرّب الإصدار الكامل مجانًا لمدة <b>يوم واحد</b> وراقب قوة الإشارات بنفسك.\n"
        "💳 بعد الدفع أرسل رقم المرجع (TxID):\n"
        "<code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>\n\n"
        "🚀 <i>كل صفقة مدروسة بإطار واضح: دخول، وقف، أهداف، ورسائل متابعة.</i>"
    )

# ===== زر مراسلة الدعم (خاص) =====
def support_dm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        kb.button(text="💬 مراسلة الدعم (خاص)", url=f"https://t.me/{SUPPORT_USERNAME}")
    return kb.as_markup()

# ===== إرسال صورة دليل الدفع =====
from aiogram.types import FSInputFile
async def send_pay_guide(chat_id: int):
    """إرسال صورة/إنفوغرافيك طريقة الدفع إن توفّر ملف محلي أو file_id أو URL."""
    # 1) ملف محلي
    if PAY_GUIDE_LOCAL_PATH and os.path.exists(PAY_GUIDE_LOCAL_PATH):
        try:
            msg = await bot.send_photo(chat_id, photo=FSInputFile(PAY_GUIDE_LOCAL_PATH), caption="📸 دليل الدفع المختصر")
            logger.info(f"pay_guide sent (local) → msg_id={msg.message_id} chat_id={chat_id} path={PAY_GUIDE_LOCAL_PATH}")
            return
        except Exception as e:
            logger.warning(f"PAY_GUIDE_LOCAL failed: {e}")
    # 2) file_id
    if PAY_GUIDE_FILE_ID:
        try:
            msg = await bot.send_photo(chat_id, PAY_GUIDE_FILE_ID, caption="📸 دليل الدفع المختصر")
            logger.info(f"pay_guide sent (file_id) → msg_id={msg.message_id} chat_id={chat_id}")
            return
        except Exception as e:
            logger.warning(f"PAY_GUIDE_FILE_ID failed: {e}")
    # 3) URL
    if PAY_GUIDE_URL:
        try:
            msg = await bot.send_photo(chat_id, PAY_GUIDE_URL, caption="📸 دليل الدفع المختصر")
            logger.info(f"pay_guide sent (url) → msg_id={msg.message_id} chat_id={chat_id} url={PAY_GUIDE_URL}")
            return
        except Exception as e:
            logger.warning(f"PAY_GUIDE_URL failed: {e}")
    # فشل
    await bot.send_message(chat_id, "⚠️ تعذّر إرفاق صورة دليل الدفع حاليًا.")
    logger.warning("pay_guide: no valid source (local/file_id/url).")

# ===== تنسيق رسائل الإشارة/الإغلاق بتحفيز =====
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
        "⚡️ <i>التزم بالخطة: حجم مخاطرة ثابت، لا تلحق بالسعر، والتزم بالوقف.</i>\n"
        "💡 <i>نصيحة: إدخال هادئ أفضل من مطاردة شمعة سريعة.</i>"
    )

def format_close_text(t: Trade, r_multiple: float | None = None) -> str:
    emoji = {"tp1": "🎯", "tp2": "🏆", "sl": "🛑"}.get(t.result or "", "ℹ️")
    result_label = {"tp1": "تحقق الهدف 1 — خطوة ممتازة!", "tp2": "تحقق الهدف 2 — إنجاز رائع!", "sl": "وقف الخسارة — حماية رأس المال"}.get(t.result or "", "إغلاق")
    r_line = f"\n📐 R: <b>{round(r_multiple, 3)}</b>" if r_multiple is not None else ""
    tip = "🔁 نبحث عن فرصة أقوى تالية… الصبر مكسب." if (t.result == "sl") else "🎯 إدارة الربح أهم من الصفقات الكثيرة."
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
# جلب البيانات/الأسعار (مع Rate Limiter + Backoff)
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
def _should_skip_duplicate(sig: dict) -> bool:
    """منع إرسال إشارة متقاربة لنفس الرمز ضمن نافذة زمنية محددة."""
    sym = sig.get("symbol")
    if not sym:
        return False
    now = _now_ts()
    last_ts = _LAST_SIGNAL_AT.get(sym, 0)
    if now - last_ts < DEDUPE_WINDOW_MIN * 60:
        return True
    _LAST_SIGNAL_AT[sym] = now
    return False

# ---------------------------
# فحص الإشارات (Batch + Concurrency)
# ---------------------------
SCAN_LOCK = asyncio.Lock()

async def _send_signal_to_channel(sig: dict, audit_id: str | None) -> None:
    if TRUST_LAYER:
        try:
            text = format_signal_card(sig, risk_pct=0.005, daily_cap_r=MAX_DAILY_LOSS_R)
            await send_channel(text)
            _ = log_signal(sig, status="opened")
            return
        except Exception as e:
            logger.exception(f"TRUST LAYER send error: {e}")
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

        # تقسيم على دفعات
        for i in range(0, len(AVAILABLE_SYMBOLS), SCAN_BATCH_SIZE):
            batch = AVAILABLE_SYMBOLS[i:i+SCAN_BATCH_SIZE]
            sigs = await asyncio.gather(*[ _guarded_scan(s) for s in batch ])
            # معالجة النتائج
            for sig in filter(None, sigs):
                # منع التكرار
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

                    audit_id = make_audit_id(sig["symbol"], sig["entry"], sig.get("score", 0)) if TRUST_LAYER \
                               else _make_audit_id(sig["symbol"], sig["entry"], sig.get("score", 0))
                    try:
                        trade_id = add_trade_sig(s, sig, audit_id=audit_id, qty=None)
                    except Exception as e:
                        logger.exception(f"add_trade_sig error, fallback to add_trade: {e}")
                        trade_id = add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

                    AUDIT_IDS[trade_id] = audit_id

                try:
                    await _send_signal_to_channel(sig, audit_id)
                    # رسالة تحفيزية قصيرة على الخاص للمشتركين النشطين
                    note = (
                        "🚀 <b>إشارة جديدة وصلت!</b>\n"
                        "🔔 لا تتعجل — التزم بحجم مخاطرة ثابت وانتظر مناطق الدخول المحددة."
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

                await asyncio.sleep(0.05)  # لطيف على التليجرام/API
            # فاصل بسيط بين الدفعات
            await asyncio.sleep(0.1)

async def loop_signals():
    while True:
        started = time.time()
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        # احترام الفترة المتبقية من الإطار الزمني للدورة
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

                    # حساب R ثم إغلاق باستدعاء واحد
                    r_multiple = on_trade_closed_update_risk(t, result, exit_px)
                    try:
                        close_trade(s, t.id, result, exit_price=exit_px, r_multiple=r_multiple)
                    except Exception as e:
                        logger.warning(f"close_trade warn: {e}")

                    audit_id = AUDIT_IDS.get(t.id) or _make_audit_id(t.symbol, float(t.entry), 0)
                    if TRUST_LAYER:
                        try:
                            log_close(audit_id, t.symbol, float(exit_px), float(r_multiple), reason=result)
                        except Exception:
                            pass

                    # إشعار تحفيزي
                    msg = format_close_text(t, r_multiple)
                    msg += "\n💡 <i>انضباطك مع الوقف والأهداف يصنع الفرق على المدى الطويل.</i>"
                    await notify_subscribers(msg)
                    await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"MONITOR ERROR: {e}")
        await asyncio.sleep(MONITOR_INTERVAL_SEC)

# ---------------------------
# التقرير اليومي (مع إعادة محاولة)
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
        "💡 <i>الخطة أهم من الضجيج: إدارة مخاطرة ثابتة + التزام بالأهداف.</i>\n"
        "🚀 <i>نستهدف فرصًا منتقاة بجودة أعلى بدل كثرة الإشارات.</i>"
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
    kb.button(text="✨ ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="💳 الدفع (USDT TRC20) + رقم المرجع", callback_data="subscribe_info")
    kb.button(text="💬 تواصل الدعم", callback_data="support")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="HTML", reply_markup=kb.as_markup())
    if SUPPORT_USERNAME:
        await m.answer("تحتاج مساعدة؟ فريق الدعم يرد بسرعة:", reply_markup=support_dm_kb())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)
    if ok:
        await q.message.edit_text(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁\n"
            "🚀 استعد لتجربة إشارات احترافية مع إدارة مخاطرة منضبطة.", parse_mode="HTML")
    else:
        await q.message.edit_text(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\n"
            "✨ يمكنك الاشتراك الآن والاستفادة من كامل الميزات.", parse_mode="HTML")
    await q.answer()

@dp.message(Command("help"))
async def cmd_help(m: Message):
    text = (
        "🤖 <b>أوامر المستخدم</b>\n"
        "• <code>/start</code> – البداية والقائمة الرئيسية\n"
        "• <code>/pay</code> – الدفع وشرح رقم المرجع (TxID)\n"
        "• <code>/submit_tx</code> – إرسال رقم المرجع لتفعيل الاشتراك\n"
        "• <code>/status</code> – حالة الاشتراك\n"
        "• <code>/support</code> – فتح محادثة مع الدعم + زر مراسلة خاص\n"
        "• <code>/cancel</code> – إلغاء محادثة الدعم الحالية"
    )
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer(
        "✅ <b>اشتراكك نشط.</b>\n🚀 ابق منضبطًا—النتيجة مجموع خطوات صحيحة."
        if ok else
        "❌ <b>لا تملك اشتراكًا نشطًا.</b>\n✨ اشترك اليوم وابدأ مع أول تقرير صباحي.",
        parse_mode="HTML"
    )

@dp.message(Command("pay"))
async def cmd_pay(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 طريقة الإرسال ورقم المرجع (TxID)", callback_data="tx_help")
    kb.button(text="💳 أسعار وخطط الاشتراك", callback_data="subscribe_info")
    kb.button(text="💬 تواصل الدعم", callback_data="support")
    kb.adjust(1)
    txt = (
        "💳 <b>الدفع عبر USDT (TRC20)</b>\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>\n"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n\n"
        f"أرسل إلى المحفظة:\n<code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
        "بعد التحويل أرسل رقم المرجع (TxID) مع الخطة:\n"
        "<code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>\n\n"
        "✅ يمكنك لصق <i>رابط Tronscan</i> مباشرة (سأستخرج رقم المرجع تلقائيًا).\n"
        "📸 أرفقنا لك دليلًا بصريًا مختصرًا 👇"
    )
    await m.answer(txt, parse_mode="HTML", reply_markup=kb.as_markup())
    await send_pay_guide(m.chat.id)  # إرسال الصورة إن توفرت
    if SUPPORT_USERNAME:
        await m.answer("تحتاج مساعدة بالدفع؟ اضغط الزر لفتح الخاص:", reply_markup=support_dm_kb())

@dp.callback_query(F.data == "tx_help")
async def cb_tx_help(q: CallbackQuery):
    await q.message.answer(REFERENCE_HINT, parse_mode="HTML")
    await send_pay_guide(q.message.chat.id)
    if SUPPORT_USERNAME:
        await q.message.answer("لو واجهتك صعوبة، راسلنا على الخاص:", reply_markup=support_dm_kb())
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    await cmd_pay(q.message); await q.answer()

# --- دعم: فتح محادثة المستخدم ---
@dp.message(Command("support"))
async def cmd_support(m: Message):
    _support_set(m.from_user.id)
    await m.answer(
        "🆘 <b>تم فتح محادثة الدعم.</b>\n"
        f"اكتب مشكلتك الآن (المهلة {SUPPORT_WAIT_MINUTES} دقائق). أرسل <code>/cancel</code> للإلغاء.\n\n"
        "أو اضغط الزر لفتح الخاص مباشرة مع الدعم:",
        parse_mode="HTML",
        reply_markup=support_dm_kb()
    )

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    if _support_waiting(m.from_user.id):
        _support_clear(m.from_user.id)
        return await m.answer("✅ تم إلغاء محادثة الدعم. نحن هنا متى ما احتجتنا.", parse_mode="HTML")
    await m.answer("ℹ️ لا توجد محادثة دعم جارية.", parse_mode="HTML")

@dp.callback_query(F.data == "support")
async def cb_support(q: CallbackQuery):
    _support_set(q.from_user.id)
    await q.message.answer(
        "🆘 <b>تم فتح محادثة الدعم.</b>\n"
        f"اكتب مشكلتك الآن (المهلة {SUPPORT_WAIT_MINUTES} دقائق). أرسل <code>/cancel</code> للإلغاء.\n\n"
        "أو اضغط الزر لفتح الخاص مباشرة مع الدعم:",
        parse_mode="HTML",
        reply_markup=support_dm_kb()
    )
    await q.answer()

# ---------------------------
# تفعيل يدوي للأدمن — لوحة مبسطة
# ---------------------------
@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ تفعيل اشتراك يدوي", callback_data="admin_manual")
    kb.button(text="📸 إرسال صورة دليل الدفع", callback_data="admin_send_guide")
    kb.button(text="ℹ️ مساعدة الأوامر", callback_data="admin_help_btn")
    kb.adjust(1)
    await m.answer("لوحة الأدمن:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "admin_help_btn")
async def cb_admin_help_btn(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS: return await q.answer()
    await cmd_admin_help(q.message)
    await q.answer()

@dp.callback_query(F.data == "admin_send_guide")
async def cb_admin_send_guide(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS: return await q.answer()
    await send_pay_guide(q.message.chat.id)
    await q.answer("تم.")

@dp.callback_query(F.data == "admin_manual")
async def cb_admin_manual(q: CallbackQuery):
    aid = q.from_user.id
    if aid not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    ADMIN_FLOW[aid] = {"stage": "await_user"}
    await q.message.answer("أرسل الآن <code>user_id</code> للمشترك الذي تريد تفعيله:", parse_mode="HTML")
    await q.answer()

@dp.message(F.text)
async def admin_manual_router(m: Message):
    """تدفق الإدخال للأدمن: user_id -> اختيار الخطة -> إدخال مرجع اختياري -> تفعيل."""
    aid = m.from_user.id
    # لو الإدمن في وضع "الرد على المستخدم" لا نتدخل
    if aid in ADMIN_REPLY_TARGET:
        return

    flow = ADMIN_FLOW.get(aid)
    if not flow:
        return  # لا يوجد تدفق تفعيل جارٍ لهذا الأدمن

    if aid not in ADMIN_USER_IDS:
        ADMIN_FLOW.pop(aid, None)
        return

    stage = flow.get("stage")

    # 1) انتظار user_id
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

    # 2) انتظار المرجع (اختياري)
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
            try:
                await bot.send_message(uid, "✅ تم تفعيل اشتراكك. أهلاً بك! 🚀", parse_mode="HTML")
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
        "أرسل رقم المرجع (TxID) الآن لإرفاقه بالإيصال.\n"
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
        try:
            await bot.send_message(uid, "✅ تم تفعيل اشتراكك. أهلاً بك! 🚀", parse_mode="HTML")
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

# --- دعم: نقل رسائل المستخدم إلى الدعم كـ «تذكرة» ---
async def _send_ticket_to_admins(user_msg: Message):
    uid = user_msg.from_user.id
    username = f"@{user_msg.from_user.username}" if user_msg.from_user.username else "-"
    with get_session() as s:
        u = s.query(User).filter(User.tg_user_id == uid).first()
    plan = u.plan if (u and u.plan) else "-"
    end_at = u.end_at.strftime("%Y-%m-%d %H:%M UTC") if (u and u.end_at) else "-"
    is_active_txt = "نعم" if (u and u.end_at and u.end_at > datetime.now(timezone.utc)) else "لا"
    last_tx = u.last_tx_hash if (u and u.last_tx_hash) else "-"

    header = (
        "📩 <b>بلاغ دعم جديد</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: <code>{uid}</code> ({_h(username)})\n"
        f"🔖 الخطة: <b>{_h(plan)}</b> | نشط: <b>{is_active_txt}</b>\n"
        f"⏳ صالح حتى: <code>{_h(end_at)}</code>\n"
        f"🧾 آخر مرجع: <code>{_h(last_tx)}</code>\n"
        "━━━━━━━━━━━━━━\n"
        "⬇️ محتوى البلاغ:"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ الرد على المستخدم", callback_data=f"reply_to:{uid}")
    kb.adjust(1)

    await send_admins(header)
    targets = list(ADMIN_USER_IDS)
    if SUPPORT_CHAT_ID:
        targets.append(SUPPORT_CHAT_ID)
    for chat_id in targets:
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=user_msg.chat.id, message_id=user_msg.message_id)
            await bot.send_message(chat_id, "—", reply_markup=kb.as_markup())
        except Exception as e:
            logger.warning(f"copy ticket to {chat_id} failed: {e}")

# --- دعم: تحويل أي رسالة أثناء الانتظار إلى «تذكرة» ---
@dp.message(F.text | F.photo | F.document | F.video | F.voice | F.audio)
async def any_user_message_router(m: Message):
    uid = m.from_user.id
    if _support_waiting(uid):
        _support_clear(uid)
        await m.answer("✅ تم استلام رسالتك. سيردّ عليك الدعم قريبًا — شكراً على ثقتك.", parse_mode="HTML")
        await _send_ticket_to_admins(m)

# --- دعم: أول رسالة يرسلها الإداري بعد الضغط تُرسل للمستخدم ---
@dp.callback_query(F.data.startswith("reply_to:"))
async def cb_reply_to(q: CallbackQuery):
    if q.from_user.id not in ADMIN_USER_IDS:
        return await q.answer("غير مُصرّح.", show_alert=True)
    try:
        uid = int(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("بيانات غير صالحة.", show_alert=True)

    ADMIN_REPLY_TARGET[q.from_user.id] = uid
    await q.message.answer(
        f"✍️ أرسل رسالتك الآن هنا أو على الخاص، وسيتم إرسالها للمستخدم <code>{uid}</code>.",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            q.from_user.id,
            f"✍️ أرسل ردك الآن ليُرسل للمستخدم <code>{uid}</code>.",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await q.answer("اكتب ردك الآن.")

@dp.message(F.text | F.photo | F.document | F.video | F.voice | F.audio)
async def admin_reply_bridge(m: Message):
    aid = m.from_user.id
    if aid not in ADMIN_USER_IDS:
        return
    target = ADMIN_REPLY_TARGET.get(aid)
    if not target:
        return
    try:
        await bot.send_message(target, "📩 <b>رد من الدعم</b>:", parse_mode="HTML")
        await bot.copy_message(chat_id=target, from_chat_id=m.chat.id, message_id=m.message_id)
        await m.answer("✅ تم إرسال ردك إلى المستخدم.")
    except Exception as e:
        await m.answer(f"❌ تعذّر إرسال الرد: {e}")
    finally:
        ADMIN_REPLY_TARGET.pop(aid, None)

# ---------------------------
# مسار التفعيل عن طريق المرجع للمستخدم
# ---------------------------
@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
    parts = (m.text or "").strip().split(maxsplit=2)
    if len(parts) != 3 or parts[2] not in ("2w", "4w"):
        return await m.answer(
            "استخدم: <code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>\n"
            "يمكنك أيضًا إلصاق <i>رابط Tronscan</i> بدل رقم المرجع.", parse_mode="HTML")
    ref_or_url, plan = parts[1], parts[2]
    min_amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD
    txid = extract_txid(ref_or_url)
    ok, info = find_trc20_transfer_to_me(ref_or_url, min_amount)
    if ok:
        with get_session() as s:
            dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
            end_at = approve_paid(s, m.from_user.id, plan, dur, tx_hash=txid or ref_or_url)
        return await m.answer(
            "✅ <b>تم التحقق من الدفع بنجاح</b>\n"
            f"💵 المبلغ المستلم: <b>{info} USDT</b>\n"
            f"⏳ الاشتراك فعّال حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n"
            "🚀 أهلاً بك في النسخة الكاملة — استمتع بالإشارات والتقرير اليومي!",
            parse_mode="HTML")
    alert = (
        "🔔 <b>طلب تفعيل — فشل التحقق التلقائي</b>\n"
        f"User: <code>{m.from_user.id}</code>\n"
        f"Plan: <b>{plan}</b>\n"
        f"Reference: <code>{_h(ref_or_url)}</code>\n"
        f"Reason: {_h(info)}"
    )
    await send_admins(alert)
    await m.answer(
        "❗ لم نتمكن من التحقق تلقائيًا من الدفع.\n"
        "سيقوم فريق الدعم بالمراجعة اليدوية قريبًا.\n"
        "💡 تأكد أن الإرسال كان USDT على شبكة TRON (TRC20) وأن رقم المرجع صحيح.",
        parse_mode="HTML"
    )
    if SUPPORT_USERNAME:
        await m.answer("تسريع المعالجة؟ راسلنا على الخاص:", reply_markup=support_dm_kb())

# ---------------------------
# أوامر الإدارة (التاريخية)
# ---------------------------
@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS: return
    txt = (
        "🛠️ <b>أوامر الأدمن</b>\n"
        "• <code>/admin</code> – لوحة الأزرار (تفعيل يدوي سهل)\n"
        "• <code>/approve &lt;user_id&gt; &lt;2w|4w&gt; [reference]</code>\n"
        "• <code>/broadcast &lt;text&gt;</code>\n"
        "• <code>/force_report</code>"
    )
    await m.answer(txt, parse_mode="HTML")

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
    try:
        await bot.send_message(uid, "✅ تم تفعيل اشتراكك. أهلاً بك! 🚀", parse_mode="HTML")
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

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
# Leader heartbeat task
# ---------------------------
async def _leader_heartbeat_task(name: str, holder: str):
    while True:
        try:
            ok = heartbeat_leader_lock(name, holder)
            if not ok:
                logger.error("Leader lock lost! Exiting worker loop.")
                os._exit(1)
        except Exception as e:
            logger.warning(f"Heartbeat error: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL)

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

# bot.py — Telegram Bot (إشارات + إدارة + إحالات + pep message + تحسين HTF)
# ملاحظات:
# - لا نوقف الصفقات عند بلوغ الهدف اليومي؛ فقط نرسل تنبيه اختياري للمشتركين.
# - تنبيه التهدئة عند خسارة يومية (R-) يُرسل للآدمن فقط إذا COOLDOWN_ALERT_ADMIN_ONLY=1.
# - رسالة صباحية تحفيزية تُرسل مرة واحدة يوميًا (اضبط نص/وقت الرسالة من config.py).
# - نظام إحالة: المُحيل يحصل على يوم مجاني عند تفعيل المُحال بخطة مدفوعة.

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

import ccxt
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties

# ====== قفل ملف محلي لمنع نسختين على نفس الجهاز ======
LOCKFILE_PATH = os.getenv("BOT_INSTANCE_LOCK") or ("/tmp/mk1_ai_bot.lock" if os.name != "nt" else "mk1_ai_bot.lock")
_LOCK_FP = None

def _acquire_single_instance_lock():
    """قفل خفيف يمنع تشغيل نسختين على نفس الجهاز/السيرفر."""
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

# ===== إعدادات/واردات المشروع =====
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD,
    SUB_DURATION_2W, SUB_DURATION_4W,
    USDT_TRC20_WALLET,
    DAILY_PEP_MSG_ENABLED, DAILY_PEP_MSG_HOUR_LOCAL, DAILY_PEP_MSG_TEXT,
    REF_REWARD_MODE, GIFT_ONE_DAY_HOURS,
    COOLDOWN_ALERT_ADMIN_ONLY,
)
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, close_trade, add_trade_sig,
    has_open_trade_on_symbol, get_stats_24h, get_stats_7d,
    User, Trade,
    ensure_ref_code, set_referred_by, get_user_by_tg, grant_free_hours, mark_referral_rewarded,
    get_user_by_ref_code,  # ← تمت إضافتها هنا
)

from strategy import check_signal
from symbols import SYMBOLS

# ===== تهيئة لوجينج وبوت =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ===== OKX/CCXT =====
exchange = ccxt.okx({"enableRateLimit": True})
AVAILABLE_SYMBOLS: List[str] = []

OKX_PUBLIC_MAX = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))
OKX_PUBLIC_WIN = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))

class SlidingRateLimiter:
    def __init__(self, max_calls: int, window_sec: float):
        self.max_calls = max_calls
        self.window = window_sec
        self.calls = deque()
        self._lock = asyncio.Lock()
    async def wait(self):
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                while self.calls and (now - self.calls[0]) > self.window:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.window - (now - self.calls[0]) + 0.05
            await asyncio.sleep(max(sleep_for, 0.05))

RATE = SlidingRateLimiter(OKX_PUBLIC_MAX, OKX_PUBLIC_WIN)

SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))
MONITOR_INTERVAL_SEC = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")

SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))

DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))
_LAST_SIGNAL_AT: Dict[str, float] = {}  # لكل رمز آخر وقت إرسال

# ===== حالة المخاطر اليومية (R) =====
RISK_STATE_FILE = Path("risk_state.json")
MAX_DAILY_LOSS_R = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "6"))

# هيكل: {"date":"YYYY-MM-DD", "r_sum": float, "losses_streak": int, "notified_loss": bool, "notified_gain": bool}
def load_risk_state() -> dict:
    if RISK_STATE_FILE.exists():
        try: return json.loads(RISK_STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"date": "", "r_sum": 0.0, "losses_streak": 0, "notified_loss": False, "notified_gain": False}

def save_risk_state(state: dict):
    try: RISK_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass

def reset_if_new_day(state: dict, tz_str: str = TIMEZONE) -> dict:
    tz = pytz.timezone(tz_str)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "r_sum": 0.0, "losses_streak": 0, "notified_loss": False, "notified_gain": False}
    return state

# ===== مساعدات رسائل =====
def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_channel(text: str):
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def send_admins(text: str):
    targets = list(ADMIN_USER_IDS or [])
    for admin_id in targets:
        try: await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e: logger.warning(f"ADMIN NOTIFY ERROR: {e}")

# ===== روابط دعوة (اختياري) =====
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")
TRIAL_INVITE_HOURS = int(os.getenv("TRIAL_INVITE_HOURS", "24"))

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

# ===== نصوص ثابتة قصيرة =====
def prices_text() -> str:
    parts = [f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>", f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>"]
    wallet = f"\nمحفظة USDT (TRC20): <code>{_h(USDT_TRC20_WALLET)}</code>" if USDT_TRC20_WALLET else ""
    return "الأسعار:\n" + "\n".join(parts) + wallet

async def welcome_text() -> str:
    return (
        "👋 أهلاً بك في <b>الربح التراكمي</b>\n\n"
        "🔔 إشارات لحظية + إدارة مخاطر + تقرير يومي.\n"
        f"🕘 التقرير اليومي: <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)\n\n"
        "✨ جرّب النسخة الكاملة مجانًا ليوم واحد عبر /trial\n\n" + prices_text()
    )

# ===== تنسيق الإشارة =====
def format_signal(sig: dict) -> str:
    lines = [
        f"🚨 <b>إشارة دخول</b> • { _h(sig['symbol']) }",
        f"⏱️ {_h(sig.get('timestamp',''))}",
        f"👈 BUY @ <code>{sig['entry']}</code>",
        f"🛡️ SL: <code>{sig['sl']}</code>",
        f"🎯 TP1/TP2/TP3: <code>{sig['tp1']}</code> / <code>{sig['tp2']}</code> / <code>{sig['tp3']}</code>",
        "—",
        "📌 التزم بـ TP1/SL، والتعادل بعد TP1. لا مطاردة للشموع.",
    ]
    if sig.get("reasons"):
        lines.append(f"🧠 أسباب: <i>{_h(', '.join(sig['reasons'][:6]))}</i>")
    return "\n".join(lines)

def format_tp_update(symbol: str, which: str, price: float, msg: str) -> str:
    label = "TP1" if which == "tp1" else "TP2" if which == "tp2" else "TP3"
    return f"{msg}\n\n<b>{_h(symbol)}</b> • {label} @ <code>{price}</code>"

def format_sl_update(symbol: str, price: float, msg: str) -> str:
    return f"{msg}\n\n<b>{_h(symbol)}</b> • SL @ <code>{price}</code>"

# =====/start + إحالات =====
BOT_USERNAME_CACHE: Optional[str] = None
async def get_bot_username() -> str:
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE
    me = await bot.get_me()
    BOT_USERNAME_CACHE = me.username
    return BOT_USERNAME_CACHE

def parse_start_payload(text: str) -> Optional[str]:
    # يدعم: /start ref=RCODE  أو  /start RCODE
    try:
        parts = (text or "").split(maxsplit=1)
        if len(parts) < 2: return None
        payload = parts[1].strip()
        if payload.startswith("ref="):
            return payload[4:].strip()
        return payload
    except Exception:
        return None

@dp.message(Command("start"))
async def cmd_start(message: Message):
    uid = message.from_user.id
    payload = parse_start_payload(message.text or "")
    with get_session() as s:
        # توليد كود إحالة للمستخدم إذا لا يملك
        code = ensure_ref_code(s, uid)
        # لو وصلنا مع كود إحالة صالح ولم يكن هذا المستخدم مرتبطًا من قبل
        if payload:
            if set_referred_by(s, uid, payload):
                # زد عداد المُحيل
                ref_u = get_user_by_ref_code(s, payload)
                if ref_u:
                    try:
                        ref_u.referrals_count = int(ref_u.referrals_count or 0) + 1
                        s.flush()
                    except Exception:
                        pass

    text = await welcome_text()
    # لو فيه كود إحالة خاص بالمستخدم نذكره مختصرًا
    username = await get_bot_username()
    my_link = f"https://t.me/{username}?start=ref={code}"
    text += f"\n\n🔗 رابط إحالتك: <code>{_h(my_link)}</code>\n🎁 عند تفعيل صديقك اشتراكًا مدفوعًا ستحصل على <b>يوم مجاني</b> تلقائيًا."
    await message.answer(text)

@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    with get_session() as s:
        code = ensure_ref_code(s, uid)
    username = await get_bot_username()
    my_link = f"https://t.me/{username}?start=ref={code}"
    await message.answer(
        "🎯 <b>نظام الإحالة</b>\n"
        "أرسل هذا الرابط لأصدقائك. عند تفعيلهم خطة مدفوعة، تحصل على <b>يوم مجاني</b>:\n"
        f"<code>{_h(my_link)}</code>"
    )

# ===== اشتراك تجريبي يوم واحد =====
@dp.message(Command("trial"))
async def cmd_trial(message: Message):
    uid = message.from_user.id
    with get_session() as s:
        ok = start_trial(s, uid)
    if not ok:
        await message.answer("⚠️ لقد استخدمت التجربة من قبل. تواصل معنا للترقية إلى خطة مدفوعة.")
        return
    link = await get_trial_invite_link(uid)
    if link:
        await message.answer("✅ تم تفعيل التجربة ليوم واحد.\nادخل القناة:\n" + link)
    else:
        await message.answer("✅ تم تفعيل التجربة ليوم واحد.\nأرسل /id لاستلام رابط دعوة.")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    uid = message.from_user.id
    with get_session() as s:
        u = get_user_by_tg(s, uid)
        active = is_active(s, uid)
        end_at = u.end_at.strftime("%Y-%m-%d %H:%M UTC") if (u and u.end_at) else "—"
    await message.answer(
        f"🪪 حسابك: <code>{uid}</code>\n"
        f"الحالة: {'✅ فعّال' if active else '❌ غير فعّال'}\n"
        f"ينتهي: <b>{end_at}</b>"
    )

@dp.message(Command("status"))
async def cmd_status(message: Message):
    uid = message.from_user.id
    with get_session() as s:
        active = is_active(s, uid)
        st24 = get_stats_24h(s)
        st7  = get_stats_7d(s)
    await message.answer(
        f"📊 آخر 24 ساعة: إشارات {st24['signals']} | TP {st24['tp_total']} | SL {st24['sl']} | Win% {st24['win_rate']} | ΣR {st24['r_sum']}\n"
        f"📈 7 أيام: TP {st7['tp_total']} | SL {st7['sl']} | Win% {st7['win_rate']} | ΣR {st7['r_sum']}\n"
        f"الحالة: {'✅ فعّال' if active else '❌ غير فعّال'}"
    )

@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    await message.answer("💳 الدفع/الترقية:\n" + prices_text())

# ===== أوامر أدمن بسيطة =====
def _is_admin(uid: int) -> bool:
    return uid in set(ADMIN_USER_IDS or [])

@dp.message(Command("approve"))
async def cmd_approve(message: Message):
    """استخدام: /approve <user_id> <plan:2w|4w> [tx_hash]"""
    if not _is_admin(message.from_user.id):
        return
    try:
        _, u_id, plan, *rest = (message.text or "").split()
        u_id = int(u_id)
        txh = rest[0] if rest else None
        days = SUB_DURATION_2W if plan.lower() == "2w" else SUB_DURATION_4W
    except Exception:
        await message.answer("صيغة: /approve <user_id> <2w|4w> [tx_hash]")
        return

    with get_session() as s:
        new_end = approve_paid(s, u_id, plan.lower(), days, txh)
        # مكافأة إحالة (إن وجدت) ولم تُمنح سابقًا
        new_user = get_user_by_tg(s, u_id)
        if new_user and new_user.referred_by and not new_user.referral_rewarded:
            if REF_REWARD_MODE == "paid":  # نكافئ فقط عند خطة مدفوعة
                ref_u = get_user_by_ref_code(s, new_user.referred_by)
                if ref_u and ref_u.tg_user_id:
                    grant_free_hours(s, ref_u.tg_user_id, GIFT_ONE_DAY_HOURS)
                    mark_referral_rewarded(s, new_user.tg_user_id)
                    try:
                        asyncio.create_task(bot.send_message(ref_u.tg_user_id,
                            f"🎁 مبروك! صديقك فعّل خطة مدفوعة — حصلت على يوم مجاني ✅"))
                    except Exception:
                        pass

    await message.answer(f"تمت الترقية. ينتهي اشتراك المستخدم في: <b>{new_end.strftime('%Y-%m-%d %H:%M UTC')}</b>")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not _is_admin(message.from_user.id):
        return
    txt = message.text.partition(" ")[2].strip()
    if not txt:
        await message.answer("اكتب: /broadcast نص الرسالة")
        return
    # إرسالة إلى القناة + المشتركين الفعّالين
    await send_channel(txt)
    try:
        with get_session() as s:
            now = datetime.now(timezone.utc)
            uids = [r[0] for r in s.query(User.tg_user_id).filter(User.end_at != None, User.end_at > now).all() if r[0]]
        for uid in uids:
            try: await bot.send_message(uid, txt, disable_web_page_preview=True)
            except Exception: pass
            await asyncio.sleep(0.02)
    except Exception as e:
        logger.warning(f"broadcast warn: {e}")

# ===== التحميل الأولي لأسواق OKX =====
async def load_okx_markets():
    global AVAILABLE_SYMBOLS
    try:
        await RATE.wait()
        markets = await asyncio.get_running_loop().run_in_executor(None, exchange.load_markets)
        okx_syms = set(markets.keys())
        # نحافظ على الرموز الموجودة التي يدعمها OKX
        kept = [s for s in SYMBOLS if (s.replace("/", "/") in okx_syms)]
        AVAILABLE_SYMBOLS = kept or [s for s in markets if s.endswith("/USDT")]
        logger.info(f"OKX markets loaded. Using {len(AVAILABLE_SYMBOLS)} symbols.")
    except Exception as e:
        logger.error(f"load_okx_markets error: {e}")
        AVAILABLE_SYMBOLS = SYMBOLS[:]

# ===== جلب شموع =====
async def fetch_ohlcv(symbol: str, timeframe: str = TIMEFRAME, limit: int = 400) -> Optional[List[List[float]]]:
    try:
        await RATE.wait()
        o = await asyncio.get_running_loop().run_in_executor(None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit))
        return o
    except Exception:
        return None

# ===== إرسال الإشارة =====
async def publish_signal(sig: dict):
    text = format_signal(sig)
    await send_channel(text)
    # حفظ في DB
    with get_session() as s:
        audit_id = hashlib.md5(f"{sig['symbol']}_{sig['entry']}_{sig['timestamp']}".encode()).hexdigest()[:8]
        add_trade_sig(s, sig, audit_id=audit_id, qty=None)

# ===== مراقبة الصفقات (TP/SL) =====
async def monitor_trades_loop():
    while True:
        try:
            with get_session() as s:
                open_trades = s.query(Trade).filter(Trade.status == "open").all()
            if not open_trades:
                await asyncio.sleep(MONITOR_INTERVAL_SEC)
                continue

            tickers_needed = list({t.symbol for t in open_trades})
            prices: Dict[str, float] = {}
            for sym in tickers_needed:
                try:
                    await RATE.wait()
                    ticker = await asyncio.get_running_loop().run_in_executor(None, lambda: exchange.fetch_ticker(sym))
                    prices[sym] = float(ticker.get("last") or ticker.get("close") or 0.0)
                except Exception:
                    prices[sym] = 0.0
                await asyncio.sleep(0.01)

            # تحديث النتائج وإدارة R
            state = reset_if_new_day(load_risk_state())
            for t in open_trades:
                p = prices.get(t.symbol) or 0.0
                if p <= 0: continue
                # تحقق TP/SL
                hit = None
                msg = None
                if p >= float(t.tp2 or 0) and (t.tp_final and p >= float(t.tp_final)):
                    hit = "tp2"  # سنسجّل TP2 كحد أدنى، وTP3 عندما نغلق نهائيًا
                if t.tp_final and p >= float(t.tp_final or 0):
                    hit = "tp3"
                if p <= float(t.sl or 0):
                    hit = "sl"

                if not hit:
                    continue

                # حساب R تقريبي
                risk = max(float(t.entry) - float(t.sl), 1e-9)
                r_multiple = 0.0
                if hit == "tp1":
                    r_multiple = (float(t.tp1) - float(t.entry)) / risk
                elif hit == "tp2":
                    r_multiple = (float(t.tp2) - float(t.entry)) / risk
                elif hit == "tp3":
                    r_multiple = (float(t.tp_final or t.tp2) - float(t.entry)) / risk
                elif hit == "sl":
                    r_multiple = -1.0

                # إرسال
                if hit == "sl":
                    msg = format_sl_update(t.symbol, p, "🛑 تم ضرب وقف الخسارة — ننتقل للفرصة القادمة.")
                elif hit == "tp3":
                    msg = format_tp_update(t.symbol, "tp3", p, "🏁 TP3 — إغلاق جميل، مكسب كامل ✨")
                elif hit == "tp2":
                    msg = format_tp_update(t.symbol, "tp2", p, "🚀 TP2 — فعّلنا التريلينغ لحماية المكسب.")
                else:
                    msg = format_tp_update(t.symbol, "tp1", p, "🎯 TP1 — ثبّت جزءًا وانقل SL للتعادل.")

                await send_channel(msg)

                # إغلاق الصفقة وتحديث الحالة
                with get_session() as s:
                    close_trade(s, t.id, hit, exit_price=p, r_multiple=r_multiple)

                # تحديث R اليومي
                state["r_sum"] = float(state.get("r_sum", 0.0)) + float(r_multiple)
                if hit == "sl":
                    state["losses_streak"] = int(state.get("losses_streak", 0)) + 1
                else:
                    state["losses_streak"] = 0
                save_risk_state(state)

                # تنبيه آدمن عند خسارة يومية -R
                if (state["r_sum"] <= -abs(MAX_DAILY_LOSS_R)) and not state.get("notified_loss", False):
                    txt = (
                        "⏸️ <b>إيقاف مؤقّت لفتح صفقات جديدة (تنبيه آدمن فقط)</b>\n"
                        f"السبب: حد الخسارة اليومي −{MAX_DAILY_LOSS_R}R\n"
                        f"حتى: { (datetime.utcnow()+timedelta(hours=COOLDOWN_HOURS)).strftime('%Y-%m-%d %H:%M UTC') }\n"
                        "💡 نحافظ على الذخيرة لفرص أعلى جودة."
                    )
                    if COOLDOWN_ALERT_ADMIN_ONLY:
                        await send_admins(txt)
                    else:
                        await send_channel(txt)
                    state["notified_loss"] = True
                    save_risk_state(state)

                # تهنئة +2R للمشتركين (بدون إيقاف إجباري)
                if (state["r_sum"] >= 2.0) and not state.get("notified_gain", False):
                    await send_channel(
                        "🎉 <b>إنجاز يومي:</b> حققنا اليوم +2R تقريبًا.\n"
                        "إذا اكتفيت، يكفيك هذا الربح — وإلا سنستمر في اقتناص الفرص لمن يرغب بالاستمرار."
                    )
                    state["notified_gain"] = True
                    save_risk_state(state)

            await asyncio.sleep(MONITOR_INTERVAL_SEC)
        except Exception as e:
            logger.exception(f"monitor_trades_loop error: {e}")
            await asyncio.sleep(3)

# ===== ماسح الإشارات =====
async def scan_symbols_once():
    if not AVAILABLE_SYMBOLS:
        return
    # تقسيم دفعات
    batches = [AVAILABLE_SYMBOLS[i:i+SCAN_BATCH_SIZE] for i in range(0, len(AVAILABLE_SYMBOLS), SCAN_BATCH_SIZE)]
    for batch in batches:
        await asyncio.gather(*[process_symbol(sym) for sym in batch])

async def process_symbol(symbol: str):
    try:
        # منع التكرار خلال نافذة
        last_time = _LAST_SIGNAL_AT.get(symbol, 0.0)
        if (time.time() - last_time) < (DEDUPE_WINDOW_MIN * 60):
            return

        ohlcv = await fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=240)
        if not ohlcv or len(ohlcv) < 120:
            return
        # إطار أعلى 15m إن توفر
        ohlcv_htf = await fetch_ohlcv(symbol, timeframe="15m", limit=240)

        sig = check_signal(symbol, ohlcv, ohlcv_htf)
        if not sig: return

        # حد عدد الصفقات المفتوحة
        with get_session() as s:
            if count_open_trades(s) >= MAX_OPEN_TRADES:
                return
            if has_open_trade_on_symbol(s, symbol):
                return

        await publish_signal(sig)
        _LAST_SIGNAL_AT[symbol] = time.time()
    except Exception as e:
        logger.debug(f"process_symbol({symbol}) warn: {e}")

async def scan_loop():
    await load_okx_markets()
    while True:
        try:
            await scan_symbols_once()
        except Exception as e:
            logger.exception(f"scan_loop error: {e}")
        await asyncio.sleep(SIGNAL_SCAN_INTERVAL_SEC)

# ===== تقارير يومية + رسالة Pep يومية =====
async def daily_report_loop():
    tz = pytz.timezone(TIMEZONE)
    logged_next = None
    while True:
        try:
            now = datetime.now(tz)
            target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            if logged_next != target:
                logger.info(f"Next daily report at {target.isoformat()} ({TIMEZONE})")
                logged_next = target
            await asyncio.sleep((target - now).total_seconds())
            with get_session() as s:
                st24 = get_stats_24h(s); st7 = get_stats_7d(s)
            text = (
                "🗓️ <b>التقرير اليومي</b>\n"
                f"آخر 24 ساعة — إشارات: {st24['signals']} | TP: {st24['tp_total']} | SL: {st24['sl']} | Win%: {st24['win_rate']} | ΣR: {st24['r_sum']}\n"
                f"7 أيام — TP: {st7['tp_total']} | SL: {st7['sl']} | Win%: {st7['win_rate']} | ΣR: {st7['r_sum']}"
            )
            await send_channel(text)
        except Exception as e:
            logger.exception(f"daily_report_loop error: {e}")
            await asyncio.sleep(5)

async def pep_message_loop():
    if not DAILY_PEP_MSG_ENABLED:
        return
    tz = pytz.timezone(TIMEZONE)
    last_sent_date = ""
    while True:
        try:
            now = datetime.now(tz)
            target = now.replace(hour=DAILY_PEP_MSG_HOUR_LOCAL, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep(max((target - now).total_seconds(), 1))
            today = target.strftime("%Y-%m-%d")
            if today != last_sent_date:
                await send_channel(DAILY_PEP_MSG_TEXT)
                last_sent_date = today
        except Exception as e:
            logger.exception(f"pep_message_loop error: {e}")
            await asyncio.sleep(5)

# ===== الإقلاع =====
async def on_startup():
    init_db()
    await load_okx_markets()
    logger.info("Bot is ready.")

async def main():
    await on_startup()
    # مهام خلفية
    asyncio.create_task(scan_loop())
    asyncio.create_task(monitor_trades_loop())
    asyncio.create_task(daily_report_loop())
    asyncio.create_task(pep_message_loop())
    # polling مباشر (بدون webhook)
    logger.info(f"Run polling for bot @{(await bot.get_me()).username}")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

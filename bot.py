# bot.py — مشغّل البوت (نسخة محسّنة: OKX + "رقم المرجع" + HTML + فحص قناة/أدمن)

import asyncio
import logging
import re
from datetime import datetime, timedelta

import ccxt
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from payments_tron import extract_txid, find_trc20_transfer_to_me, REFERENCE_HINT
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS, USDT_TRC20_WALLET,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD, SUB_DURATION_2W, SUB_DURATION_4W
)
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade
)
from strategy import check_signal
from symbols import SYMBOLS
from payments_tron import find_trc20_transfer_to_me

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ---------------------------
# تهيئة البوت والتبادل (OKX)
# ---------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# سنستخدم OKX بدلاً من Binance لتجنّب القيود الجغرافية
exchange = ccxt.okx({"enableRateLimit": True})
OKX_SYMBOLS: list[str] = []  # سنملؤها بعد تحميل الأسواق

# ---------------------------
# أدوات مساعدة
# ---------------------------
async def send_channel(text: str):
    """إرسال رسالة إلى قناة الإشارات"""
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

def format_usdt(addr: str) -> str:
    if not addr:
        return "<i>(لم يتم ضبط المحفظة في الإعدادات)</i>"
    # عرض العنوان كاملاً داخل code
    return f"<code>{addr}</code>"

async def welcome_text() -> str:
    """نص ترحيبي جذّاب (HTML)"""
    return (
        "👋 <b>مرحبًا بك في عالم الفرص</b> 🚀\n\n"
        "🔔 إشارات لحظية مبنية على استراتيجية احترافية (اتجاه + زخم + حجم + ATR + S/R)\n"
        f"📊 <b>تقرير يومي</b> الساعة <b>{DAILY_REPORT_HOUR_LOCAL}:00</b> (بتوقيت السعودية)\n"
        f"⏱ حد أقصى <b>{MAX_OPEN_TRADES}</b> صفقات مفتوحة + إدارة مخاطرة صارمة\n\n"
        "💎 <b>الاشتراك</b>:\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>\n"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n\n"
        f"ادفع USDT (TRC20) إلى:\n{format_usdt(USDT_TRC20_WALLET)}\n\n"
        "ثم أرسل للبوت <b>رقم المرجع</b> بهذا الشكل:\n"
        "<code>/submit_ref رقم_المرجع 2w</code>\n\n"
        "✨ جرّب يوم مجاني الآن بالضغط على الزر أدناه."
    )

async def check_channel_and_admin_on_boot():
    # فحص القناة
    try:
        msg = await bot.send_message(TELEGRAM_CHANNEL_ID, "🤖 البوت متصل بالقناة.")
        logger.info(f"CHANNEL OK: {TELEGRAM_CHANNEL_ID} / {msg.chat.title or 'channel'}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")
    # فحص تواصل الأدمن
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ إشعار: البوت بدأ العمل.")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت في الخاص وتحقق من الـ ID.")

async def load_okx_symbols():
    """تحميل أسواق OKX وتقييد قائمة الرموز بما هو متاح فعلاً."""
    global OKX_SYMBOLS
    try:
        loop = asyncio.get_event_loop()
        markets = await loop.run_in_executor(None, exchange.load_markets)
        available = set()
        for sym in SYMBOLS:
            if sym in exchange.markets:
                m = exchange.markets[sym]
                # نضمن أن الرمز سبوت ومفعّل
                if m.get("spot") and (m.get("active") in (True, None)):
                    available.add(sym)
        OKX_SYMBOLS = sorted(available)
        skipped = sorted(set(SYMBOLS) - available)
        logger.info(f"OKX markets loaded. Using {len(OKX_SYMBOLS)} symbols, skipped {len(skipped)}: {skipped}")
    except Exception as e:
        logger.exception(f"LOAD OKX MARKETS ERROR: {e}")
        OKX_SYMBOLS = []

# ---------------------------
# أوامر عامة للمستخدم
# ---------------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="💳 طريقة الاشتراك", callback_data="subscribe_info")
    kb.button(text="💵 دفع وإرسال المرجع", callback_data="pay_now")
    kb.adjust(1)
    await m.answer(await welcome_text(), reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)  # False لو سبق استخدم التجربة
    if ok:
        await q.message.edit_text(
            "✅ <b>تم تفعيل التجربة المجانية</b> لمدة يوم واحد 🎁\n"
            "ستصلك الإشارات والتقرير اليومي.",
        )
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="💳 طريقة الاشتراك", callback_data="subscribe_info")
        kb.adjust(1)
        await q.message.edit_text(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\nيمكنك الاشتراك عبر الزر أدناه.",
            reply_markup=kb.as_markup(),
        )
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    # نعيد استخدام نص الدفع المُحسّن
    await cmd_pay(q.message)
    await q.answer()

@dp.callback_query(F.data == "pay_now")
async def cb_pay_now(q: CallbackQuery):
    await cmd_pay(q.message)
    await q.answer()

@dp.message(Command("pay", "subscribe"))
async def cmd_pay(m: Message):
    txt = (
        "💎 <b>الاشتراك</b>\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>\n"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n\n"
        f"حوّل <b>USDT (TRC20)</b> إلى:\n{format_usdt(USDT_TRC20_WALLET)}\n\n"
        "ثم أرسل للبوت <b>رقم المرجع</b> بهذا الشكل:\n"
        "<code>/submit_ref رقم_المرجع 2w</code>\n\n"
        "أمثلة:\n"
        "<code>/submit_ref 9f0a...c31 2w</code>\n"
        "<code>/تأكيد_الدفع 9f0a...c31 4w</code>\n\n"
        "ملاحظات سريعة:\n"
        "• تأكد أن الشبكة TRC20.\n"
        "• الفرق في الفاصلة/الرسوم لا يؤثر إذا كان المبلغ ≥ قيمة الخطة.\n"
        "• إن تعذّر التحقق تلقائيًا سنراجع يدويًا سريعًا."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 ابدأ التجربة المجانية", callback_data="start_trial")
    kb.adjust(1)
    await m.answer(txt, reply_markup=kb.as_markup())

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ اشتراكك <b>نشط</b>." if ok else "❌ لا تملك اشتراكًا نشطًا.")

@dp.message(Command("help"))
async def cmd_help(m: Message):
    # لا نعرض أوامر الأدمن في Help العام
    await m.answer(
        "🆘 <b>مساعدة</b>\n"
        "الأوامر المتاحة:\n"
        "• <code>/start</code> — بدء واختيار التجربة/الاشتراك\n"
        "• <code>/pay</code> — طريقة الدفع وإرسال المرجع\n"
        "• <code>/submit_ref رقم_المرجع 2w|4w</code> — تأكيد الدفع\n"
        "• <code>/status</code> — حالة الاشتراك\n"
        "• <code>/help</code> — عرض هذه القائمة"
    )

# ---------------------------
# هاندلر تأكيد الدفع (رقم المرجع)
# ---------------------------
PLAN_ALIASES = {
    "2w": "2w", "14": "2w", "14d": "2w", "اسبوعين": "2w",
    "4w": "4w", "28": "4w", "28d": "4w", "4اسابيع": "4w", "4أسابيع": "4w",
}

def looks_like_txid(s: str) -> bool:
    # TRON txid عادة 64 hex، نسمح 32–100 احتياطاً
    return bool(re.fullmatch(r"[A-Fa-f0-9]{32,100}", s))

@dp.message(Command("submit_ref", "تأكيد_الدفع", "submit_tx", "ref"))
async def cmd_submit(m: Message):
    """
    الاستخدام: /submit_ref <مرجع_التحويل> <2w|4w>
    أمثلة: /submit_ref 9f0a...c31 2w  |  /تأكيد_الدفع 9f0a...c31 4w
    """
    parts = m.text.strip().split()
    if len(parts) < 2:
        return await m.answer(
            "استخدم: <b>/submit_ref رقم_المرجع 2w|4w</b>\n"
            "مثال: <code>/submit_ref 9f0a...c31 2w</code>"
        )

    txh = parts[1]
    plan_raw = parts[2] if len(parts) >= 3 else None

    if not looks_like_txid(txh):
        return await m.answer(
            "رقم المرجع غير واضح.\n"
            "أرسل بالشكل: <code>/submit_ref 9f0a...c31 2w</code>"
        )

    if not plan_raw:
        return await m.answer(
            "حدد الخطة: <b>2w</b> أو <b>4w</b>.\n"
            "مثال: <code>/submit_ref 9f0a...c31 4w</code>"
        )

    plan_key = PLAN_ALIASES.get(plan_raw.lower())
    if plan_key not in ("2w", "4w"):
        return await m.answer(
            "خطة غير معروفة. استخدم 2w أو 4w.\n"
            "مثال: <code>/submit_ref 9f0a...c31 2w</code>"
        )

    min_amount = PRICE_2_WEEKS_USD if plan_key == "2w" else PRICE_4_WEEKS_USD

    ok, info = find_trc20_transfer_to_me(txh, min_amount)
    if ok:
        with get_session() as s:
            dur = SUB_DURATION_2W if plan_key == "2w" else SUB_DURATION_4W
            end_at = approve_paid(s, m.from_user.id, plan_key, dur, tx_hash=txh)
        return await m.answer(
            f"✅ تم التحقق من <b>رقم المرجع</b> (<b>{info} USDT</b>) وتفعيل اشتراكك.\n"
            f"⏳ صالح حتى: <b>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</b>"
        )

    # فشل التحقق التلقائي — تنبيه للأدمن
    alert = (
        "🔔 طلب تفعيل (فشل تحقق تلقائي)\n"
        f"User: {m.from_user.id}\nPlan: {plan_key}\nRef: {txh}\nReason: {info}"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, alert)
        except Exception:
            pass

    await m.answer(
        "❗ لم أستطع التحقق تلقائيًا من <b>رقم المرجع</b>.\n"
        "سيتم مراجعته يدويًا قريبًا."
    )

# ---------------------------
# أوامر الإدارة (مخفية عن /help العام)
# ---------------------------
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    parts = m.text.strip().split()
    if len(parts) not in (3, 4):
        return await m.answer("استخدم: <code>/approve &lt;user_id&gt; &lt;2w|4w&gt; [ref]</code>")

    uid = int(parts[1])
    plan = parts[2]
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    txh = parts[3] if len(parts) == 4 else None

    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
    await m.answer(f"تم التفعيل للمستخدم <b>{uid}</b>. صالح حتى <b>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</b>.")
    try:
        await bot.send_message(uid, "✅ تم تفعيل اشتراكك. مرحبًا بك!")
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    await m.answer(
        "👑 <b>أوامر الإدارة</b>\n"
        "• <code>/approve user_id 2w|4w [ref]</code> — تفعيل يدوي\n"
        "• (قريبًا) /broadcast — إرسال رسالة للمشتركين\n"
        "• (قريبًا) /stats — ملخص أداء عام"
    )

# ---------------------------
# فحص الشموع/الإشارات (OKX)
# ---------------------------
async def fetch_ohlcv(symbol: str, timeframe="5m", limit=150):
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
    except Exception as e:
        logger.warning(f"FETCH_OHLCV ERROR {symbol}: {e}")
        return []

async def scan_and_dispatch():
    """
    يفحص الرموز المتاحة على OKX، يطبق الاستراتيجية، ويرسل الإشارات فورًا للقناة.
    يسجل الصفقات في DB (لتوسعة الأداء لاحقًا).
    """
    syms = OKX_SYMBOLS or SYMBOLS
    for sym in syms:
        data = await fetch_ohlcv(sym)
        sig = check_signal(sym, data)
        if sig:
            with get_session() as s:
                if count_open_trades(s) < MAX_OPEN_TRADES:
                    add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])
            text = (
                "🚀 <b>إشارة جديدة [BUY]</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"🔹 العملة: <b>{sig['symbol']}</b>\n"
                f"💵 سعر الدخول: <b>{sig['entry']}</b>\n"
                f"📉 وقف الخسارة: <b>{sig['sl']}</b>\n"
                f"🎯 الهدف 1: <b>{sig['tp1']}</b>\n"
                f"🎯 الهدف 2: <b>{sig['tp2']}</b>\n"
                f"⏰ الوقت: <code>{sig['timestamp']}</code>\n"
                "━━━━━━━━━━━━━━\n⚡️ إدارة رأس المال قبل كل صفقة."
            )
            await send_channel(text)
            logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")
        await asyncio.sleep(0.4)  # تهدئة لتجنب rate limits

async def loop_signals():
    """حلقة فحص الإشارات (كل 5 دقائق)."""
    while True:
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        await asyncio.sleep(300)  # 5 دقائق

# ---------------------------
# التقرير اليومي (صيغة أجمل؛ مع fallback لو لم تتوفر إحصائيات DB)
# ---------------------------
def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

async def daily_report_loop():
    """
    يرسل تقريرًا يوميًا مُحسّن النص الساعة المحددة (بتوقيت الرياض).
    إن توفرت دوال الإحصائيات في database.py سنستخدمها؛ وإلا نرسل تقريرًا عامًا جميل الصياغة.
    """
    tz = pytz.timezone(TIMEZONE)
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info(f"Next daily report at {target.isoformat()} ({TIMEZONE}) in {int(delay)}s")
        await asyncio.sleep(delay)

        try:
            # محاولة إحضار إحصائيات، لو غير موجودة نرسل نصًا عامًا
            stats_text = None
            try:
                from database import get_stats_24h, get_stats_7d  # اختيارية
                with get_session() as s:
                    d24 = get_stats_24h(s)  # dict متوقعة: signals, open, tp1, tp2, sl, win_rate, r_sum
                    w7  = get_stats_7d(s)   # dict مماثلة لفترة 7 أيام
                stats_text = (
                    "📊 <b>تقرير اليوم</b>\n"
                    "━━━━━━━━━━━━━━\n"
                    f"• الإشارات (24h): <b>{d24.get('signals', 0)}</b>\n"
                    f"• الصفقات المفتوحة الآن: <b>{d24.get('open', 0)}</b>\n\n"
                    f"• نتائج آخر 24h:\n"
                    f"  – TP hit: <b>{d24.get('tp_total', 0)}</b> "
                    f"(TP1: {d24.get('tp1', 0)} • TP2: {d24.get('tp2', 0)})\n"
                    f"  – SL hit: <b>{d24.get('sl', 0)}</b>\n"
                    f"  – Win Rate (24h): <b>{d24.get('win_rate', 0)}%</b>\n"
                    f"  – R المحققة (24h): <b>{d24.get('r_sum', 0)}</b>\n\n"
                    f"• نظرة 7 أيام:\n"
                    f"  – Win Rate: <b>{w7.get('win_rate', 0)}%</b>\n"
                    f"  – R التراكمي: <b>{w7.get('r_sum', 0)}</b>\n\n"
                    f"⏱ يُرسل يوميًا <b>{DAILY_REPORT_HOUR_LOCAL}:00</b> ({TIMEZONE}).\n"
                    "⚡️ تذكير: إدارة رأس المال أولًا."
                )
            except Exception:
                pass

            if not stats_text:
                stats_text = (
                    "📊 <b>تقرير الإشارات اليومي</b>\n"
                    "━━━━━━━━━━━━━━\n"
                    "سنضيف قريبًا ملخصًا بالأرقام (نسبة الفوز + R التراكمي).\n"
                    f"⏱ يُرسل يوميًا <b>{DAILY_REPORT_HOUR_LOCAL}:00</b> ({TIMEZONE}).\n"
                    "⚡️ تذكير: إدارة رأس المال أولًا."
                )

            await send_channel(stats_text)
            logger.info("Daily report sent.")
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e}")

# ---------------------------
# التشغيل
# ---------------------------
async def main():
    logger.info("Initializing DB...")
    init_db()
    logger.info("DB initialized.")

    # حذف الـ Webhook لأننا نستعمل polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # تحميل أسواق OKX وتقييد الرموز
    await load_okx_symbols()

    # فحص القناة والأدمن
    await check_channel_and_admin_on_boot()

    # إشعار بدء التشغيل للأدمن (تلخيص)
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ العمل على Render (polling).")
        except Exception:
            pass

    # نطلق 3 مهام متوازية:
    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())

    try:
        await asyncio.gather(t1, t2, t3)
    except Exception as e:
        logger.exception(f"FATAL ERROR: {e}")
        for admin_id in ADMIN_USER_IDS:
            try:
                await bot.send_message(admin_id, f"❌ تعطل البوت: {e}")
            except Exception:
                pass
        raise

if __name__ == "__main__":
    asyncio.run(main())

# ---------------------------------------------
# ملاحظات سريعة لبوت فاذر (قائمة الأوامر المقترحة للمستخدمين فقط):
#
# start - ابدأ
# pay - طريقة الدفع وإرسال المرجع
# submit_ref - تأكيد الدفع (أرسل: /submit_ref رقم_المرجع 2w|4w)
# status - حالة الاشتراك
# help - مساعدة
#
# (لا تضف أوامر الأدمن في /setcommands حتى لا تظهر للمشتركين)
# ---------------------------------------------

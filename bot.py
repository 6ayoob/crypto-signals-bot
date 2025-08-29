# bot.py — مشغّل البوت

import asyncio
import logging
import ccxt
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta
import pytz


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
# تهيئة البوت والتبادل
# ---------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
exchange = ccxt.bybit({"enableRateLimit": True})

# ---------------------------
# أدوات مساعدة
# ---------------------------
async def send_channel(text: str):
    """إرسال رسالة إلى قناة الإشارات"""
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def welcome_text() -> str:
    """نص ترحيبي مختصر"""
    return (
        "👋 أهلاً بك في عالم الفرص 🚀\n\n"
        "🔔 إشارات لحظية مدعومة باستراتيجية احترافية\n"
        f"📊 تقرير يومي الساعة {DAILY_REPORT_HOUR_LOCAL} صباحًا (بتوقيت السعودية)\n"
        f"⏱ حد أقصى {MAX_OPEN_TRADES} صفقات مفتوحة\n"
        "💰 إدارة مخاطرة صارمة\n\n"
        "خطط الاشتراك:\n"
        f"• أسبوعان: {PRICE_2_WEEKS_USD}$\n"
        f"• 4 أسابيع: {PRICE_4_WEEKS_USD}$\n"
        f"(USDT TRC20): `{USDT_TRC20_WALLET}`\n\n"
        "✨ جرّبنا مجانًا لمدة يوم واحد بالضغط على الزر."
    )

# ---------------------------
# أوامر عامة للمستخدم
# ---------------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="طريقة الاشتراك", callback_data="subscribe_info")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="Markdown", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)  # False لو سبق استخدم التجربة
    if ok:
        await q.message.edit_text("✅ تم تفعيل التجربة المجانية لمدة يوم واحد 🎁\nستصلك الإشارات والتقرير اليومي.")
    else:
        await q.message.edit_text("ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\nيمكنك الاشتراك عبر الزر أدناه.")
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    text = (
        "💎 الاشتراك:\n"
        f"• أسبوعان: {PRICE_2_WEEKS_USD}$\n"
        f"• 4 أسابيع: {PRICE_4_WEEKS_USD}$\n\n"
        f"أرسل USDT (TRC20) إلى:\n`{USDT_TRC20_WALLET}`\n\n"
        "بعد التحويل أرسل:\n/submit_tx <TransactionHash> <2w|4w>\n"
        "مثال: /submit_tx abcd1234... 2w"
    )
    await q.message.edit_text(text, parse_mode="Markdown")
    await q.answer()

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ اشتراكك نشط." if ok else "❌ لا تملك اشتراكًا نشطًا.")

@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
    """
    يتحقق تلقائيًا من معاملة TRC20 USDT.
    إن نجحت الشروط، يُفعّل الاشتراك تلقائيًا؛ وإلا يرسل تنبيه للأدمن للمراجعة اليدوية.
    """
    parts = m.text.strip().split()
    if len(parts) != 3 or parts[2] not in ("2w", "4w"):
        return await m.answer("استخدم: /submit_tx <hash> <2w|4w>")

    txh, plan = parts[1], parts[2]
    min_amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD

    ok, info = find_trc20_transfer_to_me(txh, min_amount)
    if ok:
        with get_session() as s:
            dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
            end_at = approve_paid(s, m.from_user.id, plan, dur, tx_hash=txh)
        return await m.answer(
            f"✅ تم التحقق من المعاملة ({info} USDT) وتفعيل اشتراكك تلقائيًا.\n"
            f"⏳ صالح حتى: {end_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )

    # فشل التحقق التلقائي — تنبيه للأدمن
    alert = (
        "🔔 طلب تفعيل (فشل تحقق تلقائي)\n"
        f"User: {m.from_user.id}\nPlan: {plan}\nTX: {txh}\nReason: {info}"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, alert)
        except Exception as e:
            logger.warning(f"ADMIN ALERT ERROR: {e}")
    await m.answer("❗ لم أستطع التحقق تلقائيًا من التحويل.\nسيتم مراجعته يدويًا قريبًا.")

# ---------------------------
# أوامر الإدارة (أنت فقط)
# ---------------------------
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    parts = m.text.strip().split()
    if len(parts) not in (3, 4):
        return await m.answer("استخدم: /approve <user_id> <2w|4w> [tx_hash]")

    uid = int(parts[1])
    plan = parts[2]
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    txh = parts[3] if len(parts) == 4 else None

    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
    await m.answer(f"تم التفعيل للمستخدم {uid}. صالح حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}.")
    try:
        await bot.send_message(uid, "✅ تم تفعيل اشتراكك. مرحبًا بك!")
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

# ---------------------------
# فحص الشموع/الإشارات
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
    يفحص كل الرموز، يطبق الاستراتيجية، ويرسل الإشارات فورًا للقناة.
    يسجل الصفقات في DB (اختياري الآن للمتابعة لاحقًا).
    """
    for sym in SYMBOLS:
        data = await fetch_ohlcv(sym)
        sig = check_signal(sym, data)
        if sig:
            with get_session() as s:
                if count_open_trades(s) < MAX_OPEN_TRADES:
                    add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

            text = (
                "🚀 إشارة جديدة [BUY]\n"
                "━━━━━━━━━━━━━━\n"
                f"🔹 العملة: {sig['symbol']}\n"
                f"💵 سعر الدخول: {sig['entry']}\n"
                f"📉 وقف الخسارة: {sig['sl']}\n"
                f"🎯 الهدف 1: {sig['tp1']}\n"
                f"🎯 الهدف 2: {sig['tp2']}\n"
                f"⏰ الوقت: {sig['timestamp']}\n"
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
# التقرير اليومي
# ---------------------------
async def daily_report_loop():
    """
    يرسل تقريرًا يوميًا بسيطًا الساعة المحددة (بتوقيت الرياض).
    يمكنك ربطه لاحقًا ببيانات الأداء/الصفقات من DB.
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
            await send_channel(
                "📊 تقرير الإشارات اليومي\n"
                "━━━━━━━━━━━━━━\n"
                "(سيتم توسيع التقرير لاحقًا لعرض أداء الصفقات)\n"
                "━━━━━━━━━━━━━━\n"
                f"🕘 {DAILY_REPORT_HOUR_LOCAL} صباحًا"
            )
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

    # حذف أي Webhook لأننا نستعمل polling
    try:
        await bot.session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "true"}
        )
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # إشعار بدء التشغيل للأدمن
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ العمل على Render (polling).")
        except Exception as e:
            logger.warning(f"ADMIN NOTIFY ERROR: {e}")

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

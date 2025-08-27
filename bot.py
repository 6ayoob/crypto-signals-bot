# bot.py — مشغّل البوت كعامل Background Worker
import asyncio
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
from database import init_db, get_session, is_active, start_trial, approve_paid, Trade, count_open_trades, add_trade
from strategy import check_signal
from symbols import SYMBOLS

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
exchange = ccxt.binance({"enableRateLimit": True})

async def send_channel(text: str):
    await bot.send_message(TELEGRAM_CHANNEL_ID, text)

async def welcome_text() -> str:
    return (
        "👋 أهلاً بك في عالم الفرص 🚀\n\n"
        "🔔 إشارات لحظية مدعومة باستراتيجية احترافية\n"
        "📊 تقرير يومي الساعة 9 صباحًا (بتوقيت السعودية)\n"
        f"⏱ لا أكثر من {MAX_OPEN_TRADES} صفقات مفتوحة\n"
        "💰 إدارة مخاطرة صارمة\n\n"
        "خطط الاشتراك:\n"
        f"• أسبوعان: {PRICE_2_WEEKS_USD}$\n"
        f"• 4 أسابيع: {PRICE_4_WEEKS_USD}$\n"
        f"(USDT TRC20): `{USDT_TRC20_WALLET}`\n\n"
        "✨ جرّبنا مجانًا لمدة يومين بالضغط على الزر."
    )

# ==== أوامر عامة ====
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="ابدأ التجربة المجانية (يومين)", callback_data="start_trial")
    kb.button(text="طريقة الاشتراك", callback_data="subscribe_info")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="Markdown", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        start_trial(s, q.from_user.id)
    await q.message.edit_text("✅ تم تفعيل التجربة المجانية لمدة يومين 🎁\nستصلك الإشارات والتقرير اليومي.")
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    text = (
        "💎 الاشتراك:\n"
        f"• أسبوعان: {PRICE_2_WEEKS_USD}$\n"
        f"• 4 أسابيع: {PRICE_4_WEEKS_USD}$\n\n"
        f"أرسل USDT (TRC20) إلى:\n`{USDT_TRC20_WALLET}`\n\n"
        "بعد التحويل أرسل:\n/submit_tx <TransactionHash> <2w|4w>"
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
    parts = m.text.strip().split()
    if len(parts) != 3 or parts[2] not in ("2w","4w"):
        return await m.answer("استخدم: /submit_tx <hash> <2w|4w>")
    txh, plan = parts[1], parts[2]
    alert = f"🔔 طلب تفعيل جديد\nUser: {m.from_user.id}\nPlan: {plan}\nTX: {txh}"
    for admin_id in ADMIN_USER_IDS:
        try: await bot.send_message(admin_id, alert)
        except: pass
    await m.answer("تم استلام طلبك ✅ سيتم التفعيل بعد التحقق.")

# ==== أوامر الإدارة ====
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    parts = m.text.strip().split()
    if len(parts) not in (3,4): return await m.answer("استخدم: /approve <user_id> <2w|4w> [tx_hash]")
    uid = int(parts[1]); plan = parts[2]
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    txh = parts[3] if len(parts) == 4 else None
    with get_session() as s:
        approve_paid(s, uid, plan, dur, txh)
    await m.answer(f"تم التفعيل للمستخدم {uid}.")
    try: await bot.send_message(uid, "✅ تم تفعيل اشتراكك. مرحبًا بك!")
    except: pass

# ==== فحص الإشارات ====
async def fetch_ohlcv(symbol: str, timeframe="5m", limit=150):
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
    except Exception:
        return []

async def scan_and_dispatch():
    for sym in SYMBOLS:
        data = await fetch_ohlcv(sym)
        sig = check_signal(sym, data)
        if sig:
            # تأكد من الحد الأقصى لعدد الصفقات المفتوحة (اختياري لحفظ سجل فقط)
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
            await bot.send_message(TELEGRAM_CHANNEL_ID, text)
        await asyncio.sleep(0.2)  # تهدئة بسيطة

async def loop_signals():
    while True:
        try: await scan_and_dispatch()
        except Exception: pass
        await asyncio.sleep(300)  # كل 5 دقائق

# ==== التقرير اليومي ====
async def daily_report_loop():
    tz = pytz.timezone(TIMEZONE)
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        if now >= target: target = target + timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await bot.send_message(TELEGRAM_CHANNEL_ID,
                "📊 تقرير الإشارات اليومي\n━━━━━━━━━━━━━━\n(سيتم توسيع التقرير لاحقًا لعرض أداء الصفقات)\n━━━━━━━━━━━━━━\n🕘 9 صباحًا")
        except Exception: pass

# ==== التشغيل ====
async def main():
    init_db()
    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    await asyncio.gather(t1, t2, t3)

if __name__ == "__main__":
    asyncio.run(main())

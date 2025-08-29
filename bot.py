# bot.py — مشغّل البوت (OKX + MTF 5m/15m + إحصائيات يومية + أوامر إدارية + فحوص مبكرة)
import os
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import ccxt
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
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
# Sentry (اختياري) — فعّل بـ SENTRY_DSN
# ---------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0)
    except Exception:
        pass

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

# ✅ استخدم OKX Spot وتفعيل rate limit
exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"}
})

ACTIVE_SYMBOLS = []  # سيتم ملؤها بالمدعوم فعلاً من OKX
CHANNEL_TARGET = TELEGRAM_CHANNEL_ID  # قد يكون int -100... أو '@username' حسب الإعداد

# ---------------------------
# إحصائيات يومية بسيطة في الذاكرة
# ---------------------------
tz = pytz.timezone(TIMEZONE)
STATS = {
    "today": None,                 # تاريخ اليوم (بتوقيت TIMEZONE)
    "signals": 0,                  # عدد الإشارات المرسلة اليوم
    "per_symbol": defaultdict(int) # عدد الإشارات لكل رمز اليوم
}

def maybe_reset_stats():
    today = datetime.now(tz).date()
    if STATS["today"] != today:
        STATS["today"] = today
        STATS["signals"] = 0
        STATS["per_symbol"].clear()

def user_is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in [int(x) for x in ADMIN_USER_IDS]
    except Exception:
        return False

async def assert_token_ok():
    """فحص صحة توكن البوت مبكرًا (يعطي خطأ واضح لو غير صالح)."""
    try:
        me = await bot.get_me()
        logger.info(f"BOT OK: @{me.username} (id={me.id})")
    except Exception as e:
        logger.critical(f"BAD BOT TOKEN (Unauthorized?): {e}")
        raise SystemExit(1)

async def send_channel(text: str):
    """إرسال رسالة إلى قناة الإشارات"""
    try:
        await bot.send_message(CHANNEL_TARGET, text)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def welcome_text() -> str:
    """نص ترحيبي جذاب"""
    return (
        "👋 أهلاً وسهلاً بك في *بوت الإشارات الاحترافية* 🚀\n\n"
        "💡 *ماذا ستحصل معنا؟*\n"
        "━━━━━━━━━━━━━━\n"
        "🔔 إشارات فورية مبنية على استراتيجية دقيقة\n"
        f"📊 تقرير يومي الساعة {DAILY_REPORT_HOUR_LOCAL} صباحًا (بتوقيت السعودية)\n"
        "💰 إدارة صارمة لرأس المال وتقليل المخاطر\n"
        "📈 فرص حقيقية مدروسة بعناية\n"
        "━━━━━━━━━━━━━━\n\n"
        "💎 *خطط الاشتراك:*\n"
        f"▫️ أسبوعان: *{PRICE_2_WEEKS_USD}$*\n"
        f"▫️ 4 أسابيع: *{PRICE_4_WEEKS_USD}$*\n"
        f"📥 عنوان الدفع (USDT TRC20):\n`{USDT_TRC20_WALLET}`\n\n"
        "🎁 *هدية خاصة*: تجربة مجانية لمدة *يوم واحد* — ابدأها الآن بالضغط على الزر 👇"
    )

def help_text(is_admin: bool) -> str:
    base = (
        "📚 *دليل الأوامر*\n"
        "━━━━━━━━━━━━━━\n"
        "*/start* — بدء الاستخدام وعرض الترحيب\n"
        "*/help* — عرض هذه المساعدة\n"
        "*/status* — حالة اشتراكك\n"
        "*/submit_tx <hash> <2w|4w>* — إرسال معاملة الدفع للتحقق\n"
        "*/whoami* — يظهر معرفك الرقمي\n"
    )
    admin = (
        "━━━━━━━━━━━━━━\n"
        "🛡 *أوامر الأدمن*\n"
        "*/approve <user_id> <2w|4w> [tx_hash]* — تفعيل اشتراك يدوي\n"
        "*/ping_channel* — اختبار إرسال رسالة للقناة\n"
        "*/broadcast <النص>* — إرسال رسالة للقناة\n"
        "*/stats* — عرض إحصائيات اليوم"
    )
    return base + (admin if is_admin else "")

# ---------------------------
# تهيئة الأسواق + فلترة الرموز
# ---------------------------
async def init_exchange_and_symbols():
    """
    تحميل ماركت OKX وتصفية SYMBOLS إلى الرموز المدعومة فعلاً بصيغة CCXT الموحدة مثل BTC/USDT.
    """
    loop = asyncio.get_event_loop()
    markets = await loop.run_in_executor(None, exchange.load_markets)
    supported = set(markets.keys())
    ok, skipped = [], []
    for s in SYMBOLS:
        if s in supported:
            ok.append(s)
        else:
            skipped.append(s)
    global ACTIVE_SYMBOLS
    ACTIVE_SYMBOLS = ok
    logger.info(f"OKX markets loaded. Using {len(ok)} symbols, skipped {len(skipped)}: {skipped[:12]}")

# ---------------------------
# فحص القناة والأدمن عند الإقلاع
# ---------------------------
async def validate_targets():
    # فحص القناة
    try:
        chat = await bot.get_chat(CHANNEL_TARGET)
        await bot.send_message(chat.id, "🔧 تم الربط مع القناة بنجاح.")
        logger.info(f"CHANNEL OK: {chat.id} / {getattr(chat, 'title', '')}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")

    # فحص وصول رسالة للأدمن
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "🔧 اختبار وصول الرسائل إلى الأدمن.")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت في الخاص وتحقق من الـ ID.")

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

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(help_text(user_is_admin(m.from_user.id)), parse_mode="Markdown")

@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"👤 user_id: `{m.from_user.id}`", parse_mode="Markdown")

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
# أوامر الإدارة
# ---------------------------
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if not user_is_admin(m.from_user.id):
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

@dp.message(Command("ping_channel"))
async def ping_channel(m: Message):
    if not user_is_admin(m.from_user.id):
        return await m.answer("غير مصرح")
    await send_channel("✅ اختبار: اتصال القناة يعمل!")
    await m.answer("تم إرسال رسالة اختبار للقناة.")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if not user_is_admin(m.from_user.id):
        return await m.answer("غير مصرح")
    text = m.text.partition(' ')[2].strip()
    if not text:
        return await m.answer("استخدم: /broadcast <النص>")
    await send_channel(text)
    await m.answer("تم الإرسال إلى القناة ✅")

@dp.message(Command("stats"))
async def stats(m: Message):
    if not user_is_admin(m.from_user.id):
        return await m.answer("غير مصرح")
    maybe_reset_stats()
    top = sorted(STATS["per_symbol"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines = [f"📈 إحصائيات اليوم ({STATS['today']}):", f"• عدد الإشارات: {STATS['signals']}"]
    if top:
        lines.append("• أكثر الرموز إرسالاً:")
        for sym, c in top:
            lines.append(f"   - {sym}: {c}")
    await m.answer("\n".join(lines))

# ---------------------------
# فحص الشموع/الإشارات (MTF 5m/15m)
# ---------------------------
async def fetch_ohlcv(symbol: str, timeframe="5m", limit=150):
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
    except Exception as e:
        logger.warning(f"FETCH_OHLCV ERROR {symbol} {timeframe}: {e}")
        return []

async def scan_and_dispatch():
    """
    يفحص الرموز المدعومة، يطبق الاستراتيجية، ويرسل الإشارات فورًا للقناة.
    يستخدم إطار 5m أساسي + 15m تأكيد (اختياري).
    """
    if not ACTIVE_SYMBOLS:
        logger.warning("No ACTIVE_SYMBOLS yet; skipping scan cycle.")
        return

    maybe_reset_stats()

    for sym in ACTIVE_SYMBOLS:
        data5 = await fetch_ohlcv(sym, "5m", 150)
        data15 = await fetch_ohlcv(sym, "15m", 150)  # للتأكيد متعدد الأطر
        sig = check_signal(sym, data5, data15)
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
            STATS["signals"] += 1
            STATS["per_symbol"][sym] += 1
            logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")
        await asyncio.sleep(0.35)  # تهدئة لتجنب rate limits

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
    يرسل تقريرًا يوميًا بسيطًا الساعة المحددة (بتوقيت TIMEZONE).
    يتضمن عدد الإشارات لليوم وأبرز الرموز.
    """
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DAILY_REPORT_HOUR_LOCAL, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info(f"Next daily report at {target.isoformat()} ({TIMEZONE}) in {int(delay)}s")
        await asyncio.sleep(delay)

        try:
            maybe_reset_stats()
            top = sorted(STATS["per_symbol"].items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines = [
                "📊 تقرير الإشارات اليومي",
                "━━━━━━━━━━━━━━",
                f"• عدد الإشارات اليوم: {STATS['signals']}",
            ]
            if top:
                lines.append("• أكثر الرموز نشاطًا:")
                for sym, c in top:
                    lines.append(f"   - {sym}: {c}")
            lines.append("━━━━━━━━━━━━━━")
            lines.append(f"🕘 {DAILY_REPORT_HOUR_LOCAL} صباحًا")

            await send_channel("\n".join(lines))
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

    # ✅ فحص التوكن مبكرًا
    await assert_token_ok()

    # ✅ حذف الويبهوك — نستخدم polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # ✅ تحميل أسواق OKX وتصفية الرموز
    await init_exchange_and_symbols()

    # ✅ التحقق من القناة والأدمن
    await validate_targets()

    # إشعار بدء التشغيل للأدمن (إن تعذر لا يوقف البوت)
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ العمل على Render (polling).")
        except Exception as e:
            logger.warning(f"ADMIN NOTIFY ERROR: {e}")

    # تشغيل المهام
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

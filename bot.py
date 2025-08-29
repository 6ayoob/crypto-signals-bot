# bot.py — مُشغّل البوت مع قناة خاصة للمشتركين + فحص جاهزية + حلقة انتهاء الاشتراكات
import asyncio
import logging
from datetime import datetime, timedelta, timezone

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

# ============== Logging ==============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ============== Globals ==============
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
exchange = ccxt.okx({"enableRateLimit": True})  # OKX لتفادي قيود باينانس على Render
ACTIVE_SYMBOLS: list[str] = []                 # يتم ملؤها بعد load_markets

# قناة تسويق عامة (اختياري): ضع المعرف هنا أو في config لو رغبت بإعلانات عامة
MARKETING_CHANNEL_ID = None  # مثال: -100222333444 أو "@my_public_channel"
TEASER_TO_PUBLIC = False     # فعّلها True لو تريد نشر ملخصات للإشارات في القناة العامة

# ============== أدوات مساعدة ==============
async def send_channel(text: str):
    """إرسال إلى قناة المشتركين (الخاصة)."""
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def send_marketing(text: str):
    """إرسال إلى قناة التسويق العامة (اختياري)."""
    if not MARKETING_CHANNEL_ID:
        return
    try:
        await bot.send_message(MARKETING_CHANNEL_ID, text)
    except Exception as e:
        logger.warning(f"send_marketing warn: {e}")

async def invite_user_to_channel(user_id: int, days: int):
    """دعوة المستخدم للقناة الخاصة برابط لمرة واحدة ينتهي بعد مدة الاشتراك."""
    try:
        expire_at = datetime.utcnow() + timedelta(days=days, hours=1)  # هامش ساعة
        link = await bot.create_chat_invite_link(
            chat_id=TELEGRAM_CHANNEL_ID,
            expire_date=int(expire_at.timestamp()),
            member_limit=1,
            creates_join_request=False
        )
        await bot.send_message(
            user_id,
            "🎟️ تم تفعيل وصولك للقناة الخاصة.\n"
            "ادخل عبر هذا الرابط (صالح لمرة واحدة):\n" + link.invite_link
        )
    except Exception as e:
        logger.error(f"INVITE ERROR for {user_id}: {e}")

async def kick_user_from_channel(user_id: int):
    """إخراج مستخدم من القناة الخاصة عند انتهاء الاشتراك (ban ثم unban ليسمح بدخول لاحق)."""
    try:
        await bot.ban_chat_member(TELEGRAM_CHANNEL_ID, user_id)
        await bot.unban_chat_member(TELEGRAM_CHANNEL_ID, user_id)
    except Exception as e:
        logger.warning(f"KICK WARN for {user_id}: {e}")

async def welcome_text() -> str:
    """نص ترحيبي جذّاب"""
    return (
        "👋 **أهلًا بك في عالم الفرص**\n"
        "━━━━━━━━━━━━━━\n"
        "🚀 إشارات لحظية مبنية على اتجاه + زخم + حجم + ATR + مناطق S/R\n"
        f"🕘 تقرير يومي الساعة **{DAILY_REPORT_HOUR_LOCAL}** صباحًا (بتوقيت السعودية)\n"
        f"⚖️ إدارة مخاطرة صارمة وحد أقصى **{MAX_OPEN_TRADES}** صفقات مفتوحة\n\n"
        "💎 **خطط الاشتراك**\n"
        f"• أسبوعان: **{PRICE_2_WEEKS_USD}$**\n"
        f"• 4 أسابيع: **{PRICE_4_WEEKS_USD}$**\n"
        f"💳 (USDT TRC20): `{USDT_TRC20_WALLET}`\n\n"
        "🎁 **جرّبنا ليوم واحد مجانًا** بالضغط على الزر أدناه.\n"
        "━━━━━━━━━━━━━━\n"
        "_تذكير: أرسل /start للبوت أولًا ليستطيع مراسلتك بالرابط._"
    )

def format_signal(sig: dict) -> str:
    return (
        "🚀 **إشارة جديدة [BUY]**\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 العملة: **{sig['symbol']}**\n"
        f"💵 الدخول: `{sig['entry']}`\n"
        f"📉 وقف الخسارة: `{sig['sl']}`\n"
        f"🎯 الهدف 1: `{sig['tp1']}`\n"
        f"🎯 الهدف 2: `{sig['tp2']}`\n"
        f"⏰ الوقت: {sig['timestamp']}\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ لا تنسَ إدارة رأس المال."
    )

def teaser_from_signal(sig: dict) -> str:
    """ملخص تسويقي مختصر (اختياري) ينشر للقناة العامة."""
    return (
        "📢 **تنبيه سوقي**\n"
        f"رمز: **{sig['symbol']}**\n"
        "نوع: BUY ✅\n"
        "الدخول/الأهداف داخل قناة المشتركين الخاصة.\n"
        "اشترك أو جرّب ليوم مجانًا عبر التحدث مع البوت."
    )

async def init_exchange_and_symbols():
    """تحميل أسواق OKX وتصفية الرموز غير المدعومة لتقليل الأخطاء."""
    try:
        markets = await asyncio.get_event_loop().run_in_executor(None, exchange.load_markets)
        supported = set(markets.keys())
        use, skip = [], []
        for sym in SYMBOLS:
            if sym in supported:
                use.append(sym)
            else:
                skip.append(sym)
        ACTIVE_SYMBOLS.clear()
        ACTIVE_SYMBOLS.extend(use)
        logger.info(f"OKX markets loaded. Using {len(use)} symbols, skipped {len(skip)}: {skip[:12]}")
    except Exception as e:
        logger.exception(f"INIT EXCHANGE ERROR: {e}")

async def fetch_ohlcv(symbol: str, timeframe="5m", limit=150):
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
    except Exception as e:
        logger.warning(f"FETCH_OHLCV ERROR {symbol}: {e}")
        return []

# ============== أوامر المستخدم ==============
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="💳 طريقة الاشتراك", callback_data="subscribe_info")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="Markdown", reply_markup=kb.as_markup())

@dp.message(Command("help"))
async def cmd_help(m: Message):
    text_user = (
        "🤖 **أوامر المستخدم**\n"
        "/start — الترحيب\n"
        "/help — هذه القائمة\n"
        "/status — حالة اشتراكك\n"
        "/submit_tx `<tx_hash>` `<2w|4w>` — تفعيل تلقائي بعد الدفع\n"
        "/whoami — يظهر معرّفك"
    )
    text_admin = (
        "\n\n🛡️ **أوامر الأدمن**\n"
        "/approve `<user_id>` `<2w|4w>` `[tx_hash]` — تفعيل يدوي\n"
        "/ping_channel — اختبار إرسال للقناة الخاصة\n"
        "/broadcast `<نص>` — بث في القناة الخاصة\n"
        "/stats — إحصائيات مختصرة"
    ) if m.from_user.id in ADMIN_USER_IDS else ""
    await m.answer(text_user + text_admin, parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ اشتراكك **نشط**." if ok else "❌ لا تملك اشتراكًا نشطًا.", parse_mode="Markdown")

@dp.message(Command("whoami"))
async def cmd_whoami(m: Message):
    await m.answer(f"🪪 معرّفك: `{m.from_user.id}`", parse_mode="Markdown")

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)  # False لو سبق استخدم التجربة
    if ok:
        await q.message.edit_text("✅ تم تفعيل التجربة المجانية لمدة يوم واحد 🎁\nسنرسل لك رابط القناة الخاصة الآن.")
        await invite_user_to_channel(q.from_user.id, 1)
    else:
        await q.message.edit_text("ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\nيمكنك الاشتراك عبر الزر أدناه.")
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    text = (
        "💎 **الاشتراك**\n"
        f"• أسبوعان: **{PRICE_2_WEEKS_USD}$**\n"
        f"• 4 أسابيع: **{PRICE_4_WEEKS_USD}$**\n\n"
        f"أرسل USDT (TRC20) إلى:\n`{USDT_TRC20_WALLET}`\n\n"
        "بعد التحويل أرسل:\n/submit_tx `<TransactionHash>` `<2w|4w>`\n"
        "مثال: /submit_tx abcd1234... 2w"
    )
    await q.message.edit_text(text, parse_mode="Markdown")
    await q.answer()

@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
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
        await m.answer(
            f"✅ تم التحقق من المعاملة ({info} USDT) وتفعيل اشتراكك تلقائيًا.\n"
            f"⏳ صالح حتى: {end_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        # دعوة للقناة الخاصة حسب مدة الاشتراك
        await invite_user_to_channel(m.from_user.id, dur)
        return

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

# ============== أوامر الأدمن ==============
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
        await invite_user_to_channel(uid, dur)
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

@dp.message(Command("ping_channel"))
async def cmd_ping_channel(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    await send_channel("✅ اختبار: القناة متصلة.")

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    txt = m.text.partition(" ")[2].strip()
    if not txt:
        return await m.answer("استخدم: /broadcast <النص>")
    await send_channel(txt)
    await m.answer("تم الإرسال.")

@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return await m.answer("غير مصرح")
    with get_session() as s:
        open_tr = count_open_trades(s)
    await m.answer(
        "📊 **إحصائيات مختصرة**\n"
        f"- رموز مفعّلة: {len(ACTIVE_SYMBOLS)}\n"
        f"- صفقات مفتوحة (DB): {open_tr}\n"
        f"- حد أقصى صفقات: {MAX_OPEN_TRADES}",
        parse_mode="Markdown"
    )

# ============== المسح/الإشارات ==============
async def scan_and_dispatch():
    """يفحص الرموز الفعّالة، يطبق الاستراتيجية، ويرسل الإشارات فورًا للقناة الخاصة.
       ينشر (اختياريًا) ملخصًا للقناة العامة كتسويق."""
    for sym in ACTIVE_SYMBOLS:
        data5 = await fetch_ohlcv(sym, "5m", 150)
        if not data5:
            await asyncio.sleep(0.2)
            continue
        # (اختياري) يمكن تمرير 15m للتأكيد
        sig = check_signal(sym, data5)
        if sig:
            with get_session() as s:
                if count_open_trades(s) < MAX_OPEN_TRADES:
                    add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

            text = format_signal(sig)
            await send_channel(text)
            logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")

            if TEASER_TO_PUBLIC and MARKETING_CHANNEL_ID:
                # ننشر ملخصًا بعد 10 دقائق كتشويق
                async def delayed_teaser():
                    await asyncio.sleep(600)
                    await send_marketing(teaser_from_signal(sig))
                asyncio.create_task(delayed_teaser())

        await asyncio.sleep(0.35)  # تهدئة لتجنب rate limits

async def loop_signals():
    """حلقة فحص الإشارات (كل 5 دقائق)."""
    while True:
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        await asyncio.sleep(300)  # 5 دقائق

# ============== التقرير اليومي ==============
async def daily_report_loop():
    """يرسل تقريرًا يوميًا بسيطًا الساعة المحددة (بتوقيت الرياض)."""
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
                "📊 **تقرير الإشارات اليومي**\n"
                "— سيتم توسيع التقرير لاحقًا لعرض أداء الصفقات —\n"
                f"🕘 {DAILY_REPORT_HOUR_LOCAL}:00"
            )
            logger.info("Daily report sent.")
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e}")

# ============== انتهاء الاشتراكات ==============
async def check_expirations_loop():
    """مراجعة دورية لإخراج المنتهين من القناة الخاصة."""
    from database import Subscription, User  # استيراد متأخر لتفادي الدورات
    while True:
        try:
            with get_session() as s:
                now = datetime.now(timezone.utc)
                active = {uid for (uid,) in s.query(Subscription.user_id)
                                        .filter(Subscription.end_at >= now)
                                        .distinct().all()}
                all_users = [uid for (uid,) in s.query(User.id).all()]
            for uid in all_users:
                if uid not in active:
                    await kick_user_from_channel(uid)
        except Exception as e:
            logger.exception(f"EXPIRY LOOP ERROR: {e}")
        await asyncio.sleep(3600)  # كل ساعة

# ============== فحص الجاهزية ==============
async def check_bot_ready():
    """يتأكد من صحة التوكن، وصول القناة، وإمكانية مراسلة الأدمن."""
    # حذف Webhook قبل polling لمنع التعارض
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # القناة
    try:
        ch = await bot.get_chat(TELEGRAM_CHANNEL_ID)
        await bot.send_message(TELEGRAM_CHANNEL_ID, "✅ البوت جاهز للنشر.")
        logger.info(f"CHANNEL OK: {TELEGRAM_CHANNEL_ID} / {getattr(ch, 'title', 'Channel')}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")

    # تنبيه للأدمن
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ على Render — كل شيء تمام.")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت وتحقق من الـ ID.")

# ============== التشغيل ==============
async def main():
    logger.info("Initializing DB...")
    init_db()
    logger.info("DB initialized.")

    # تهيئة الأسواق والرموز
    await init_exchange_and_symbols()

    # فحص الجاهزية (توكن/قناة/أدمن)
    await check_bot_ready()

    # تشغيل المهام
    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(check_expirations_loop())

    try:
        await asyncio.gather(t1, t2, t3, t4)
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

# bot.py — مُشغِّل البوت (Aiogram v3) مع OKX + اشتراكات + دفع TRC20 (رقم المرجع TxID) + تقارير يومية + إشعارات إغلاق الصفقات
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

# قاعدة البيانات + النماذج + دوال المساعدة
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, close_trade,
    get_stats_24h, get_stats_7d, User, Trade
)

# الاستراتيجية + الرموز
from strategy import check_signal
from symbols import SYMBOLS

# الدفع TRON (رقم المرجع TxID)
from payments_tron import extract_txid, find_trc20_transfer_to_me, REFERENCE_HINT

# ---------------------------
# إعدادات عامة
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# استخدم OKX بدلاً من Binance (حظر جغرافي)
exchange = ccxt.okx({"enableRateLimit": True})
AVAILABLE_SYMBOLS: list[str] = []

SIGNAL_SCAN_INTERVAL_SEC = 300  # كل 5 دقائق
MONITOR_INTERVAL_SEC = 15       # مراقبة الصفقات المفتوحة
TIMEFRAME = "5m"

# ---------------------------
# أدوات مساعدة
# ---------------------------
def _h(s: str) -> str:
    """هروب HTML بسيط."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_channel(text: str):
    """إرسال رسالة إلى قناة الإشارات (HTML)."""
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def send_admins(text: str):
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"ADMIN NOTIFY ERROR: {e}")

def list_active_user_ids() -> list[int]:
    """جلب كل المشتَرِكين النشطين (لإرسال تنبيهات DM اختيارياً)."""
    try:
        with get_session() as s:
            now = datetime.now(timezone.utc)
            rows = s.query(User.tg_user_id).filter(User.end_at != None, User.end_at > now).all()  # noqa: E711
            return [r[0] for r in rows if r[0]]
    except Exception as e:
        logger.warning(f"list_active_user_ids warn: {e}")
        return []

async def notify_subscribers(text: str):
    """إشعار المشتركين (DM) + القناة."""
    await send_channel(text)
    uids = list_active_user_ids()
    for uid in uids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            await asyncio.sleep(0.02)  # تهدئة بسيطة
        except Exception:
            pass

async def welcome_text() -> str:
    return (
        "👋 أهلاً بك في <b>عالم الفرص</b> 🚀\n\n"
        "🔔 إشارات لحظية مبنية على استراتيجية احترافية\n"
        f"🕘 تقرير يومي الساعة <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)\n"
        "💰 إدارة مخاطر صارمة + حد صفقات نشطة محسوب\n\n"
        "خطط الاشتراك:\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>\n"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n"
        f"محفظة USDT (TRC20): <code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
        "✨ جرّبنا مجانًا لمدة <b>يوم واحد</b> عبر الزر.\n"
        "💳 بعد الدفع أرسل رقم المرجع (TxID) هكذا:\n"
        "<code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>"
    )

def format_signal_text(sig: dict) -> str:
    return (
        "🚀 <b>إشارة جديدة [BUY]</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 العملة: <b>{_h(sig['symbol'])}</b>\n"
        f"💵 الدخول: <code>{sig['entry']}</code>\n"
        f"📉 وقف الخسارة: <code>{sig['sl']}</code>\n"
        f"🎯 الهدف 1: <code>{sig['tp1']}</code>\n"
        f"🎯 الهدف 2: <code>{sig['tp2']}</code>\n"
        f"⏰ الوقت (UTC): <code>{_h(sig['timestamp'])}</code>\n"
        "━━━━━━━━━━━━━━\n⚡️ <i>تذكير: إدارة رأس المال واجبة قبل كل صفقة.</i>"
    )

def format_close_text(t: Trade) -> str:
    emoji = {"tp1": "🎯", "tp2": "🏁", "sl": "🛑"}.get(t.result or "", "ℹ️")
    result_label = {"tp1": "تحقق الهدف 1", "tp2": "تحقق الهدف 2", "sl": "ضرب وقف الخسارة"}.get(t.result or "", "إغلاق")
    return (
        f"{emoji} <b>تم إغلاق الصفقة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 العملة: <b>{_h(t.symbol)}</b>\n"
        f"💵 الدخول: <code>{t.entry}</code>\n"
        f"📉 الوقف: <code>{t.sl}</code>\n"
        f"🎯 TP1: <code>{t.tp1}</code> | 🎯 TP2: <code>{t.tp2}</code>\n"
        f"📌 النتيجة: <b>{result_label}</b>\n"
        f"⏰ الإغلاق (UTC): <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}</code>"
    )

# ---------------------------
# تبادل (OKX): تحميل الأسواق وتصفية الرموز غير المتاحة
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
async def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=150):
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
    except Exception as e:
        logger.warning(f"FETCH_OHLCV ERROR {symbol}: {e}")
        return []

async def fetch_ticker_price(symbol: str) -> float | None:
    try:
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, lambda: exchange.fetch_ticker(symbol))
        # نأخذ آخر سعر معروف
        price = ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("last")
        return float(price) if price is not None else None
    except Exception as e:
        logger.warning(f"FETCH_TICKER ERROR {symbol}: {e}")
        return None

# ---------------------------
# حلقة فحص الإشارات
# ---------------------------
async def scan_and_dispatch():
    """
    يفحص كل الرموز، يطبق الاستراتيجية، ويرسل الإشارات فورًا للقناة (و/أو DM للمشتركين).
    يسجل الصفقات في DB.
    """
    if not AVAILABLE_SYMBOLS:
        return
    for sym in AVAILABLE_SYMBOLS:
        data = await fetch_ohlcv(sym)
        if not data:
            await asyncio.sleep(0.15)
            continue
        sig = check_signal(sym, data)
        if sig:
            with get_session() as s:
                if count_open_trades(s) < MAX_OPEN_TRADES:
                    add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])
                    text = format_signal_text(sig)
                    await send_channel(text)
                    logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")
        await asyncio.sleep(0.2)  # تهدئة لتجنب rate limits

async def loop_signals():
    """حلقة فحص الإشارات (كل 5 دقائق)."""
    while True:
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        await asyncio.sleep(SIGNAL_SCAN_INTERVAL_SEC)

# ---------------------------
# مراقبة الصفقات المفتوحة وإغلاقها عند TP/SL
# ---------------------------
async def monitor_open_trades():
    """
    مراقبة مستمرة للصفقات المفتوحة:
    - إذا وصل السعر إلى TP2: نغلق على TP2
    - وإلا إذا وصل TP1: نغلق على TP1
    - وإذا كسر SL: نغلق على SL
    """
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
                    result = None
                    if hit_tp2:
                        result = "tp2"
                    elif hit_tp1:
                        result = "tp1"
                    elif hit_sl:
                        result = "sl"

                    if result:
                        close_trade(s, t.id, result)
                        await notify_subscribers(format_close_text(t))
                        await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"MONITOR ERROR: {e}")
        await asyncio.sleep(MONITOR_INTERVAL_SEC)

# ---------------------------
# التقرير اليومي
# ---------------------------
def _report_card(stats_24: dict, stats_7d: dict) -> str:
    return (
        "📊 <b>التقرير اليومي</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>آخر 24 ساعة</b>\n"
        f"• إشارات: <b>{stats_24['signals']}</b> | صفقات مفتوحة الآن: <b>{stats_24['open']}</b>\n"
        f"• أهداف محققة: <b>{stats_24['tp_total']}</b> (TP1: {stats_24['tp1']} | TP2: {stats_24['tp2']})\n"
        f"• وقف خسارة: <b>{stats_24['sl']}</b>\n"
        f"• معدل نجاح: <b>{stats_24['win_rate']}%</b>\n"
        f"• صافي R تقريبي: <b>{stats_24['r_sum']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>آخر 7 أيام</b>\n"
        f"• إشارات: <b>{stats_7d['signals']}</b> | أهداف محققة: <b>{stats_7d['tp_total']}</b> | SL: <b>{stats_7d['sl']}</b>\n"
        f"• معدل نجاح أسبوعي: <b>{stats_7d['win_rate']}%</b> | صافي R: <b>{stats_7d['r_sum']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ <i>انضم للتجربة المجانية ليوم واحد وراقب الأداء بنفسك.</i>"
    )

async def daily_report_loop():
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
            with get_session() as s:
                stats_24 = get_stats_24h(s)
                stats_7d = get_stats_7d(s)
            await send_channel(_report_card(stats_24, stats_7d))
            logger.info("Daily report sent.")
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e}")

# ---------------------------
# أوامر المستخدم
# ---------------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="الدفع (USDT TRC20) + رقم المرجع", callback_data="subscribe_info")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id)
    if ok:
        await q.message.edit_text(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b> 🎁\n"
            "ستصلك الإشارات والتقرير اليومي على القناة.",
            parse_mode="HTML"
        )
    else:
        await q.message.edit_text(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\n"
            "يمكنك الاشتراك عبر زر الدفع.",
            parse_mode="HTML"
        )
    await q.answer()

@dp.message(Command("help"))
async def cmd_help(m: Message):
    text = (
        "🤖 <b>أوامر المستخدم</b>\n"
        "• <code>/start</code> – البداية والقائمة الرئيسية\n"
        "• <code>/pay</code> – الدفع وشرح رقم المرجع (TxID)\n"
        "• <code>/submit_tx</code> – إرسال رقم المرجع لتفعيل الاشتراك\n"
        "• <code>/status</code> – حالة الاشتراك\n"
    )
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ <b>اشتراكك نشط.</b>" if ok else "❌ <b>لا تملك اشتراكًا نشطًا.</b>", parse_mode="HTML")

@dp.message(Command("pay"))
async def cmd_pay(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="طريقة الإرسال ورقم المرجع؟", callback_data="tx_help")
    kb.button(text="أسعار وخطط الاشتراك", callback_data="subscribe_info")
    kb.adjust(1)

    txt = (
        "💳 <b>الدفع عبر USDT (TRC20)</b>\n"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b>\n"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b>\n\n"
        f"أرسل إلى المحفظة:\n<code>{_h(USDT_TRC20_WALLET)}</code>\n\n"
        "بعد التحويل أرسل رقم المرجع (TxID) مع الخطة:\n"
        "<code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>\n\n"
        "✅ يدعم إلصاق <i>رابط Tronscan</i> مباشرة (سأستخرج رقم المرجع تلقائيًا)."
    )
    await m.answer(txt, parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "tx_help")
async def cb_tx_help(q: CallbackQuery):
    await q.message.answer(REFERENCE_HINT, parse_mode="Markdown")
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    await cmd_pay(q.message)
    await q.answer()

@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
    """
    التحقق التلقائي من معاملة TRC20 USDT باستخدام «رقم المرجع (TxID)» أو رابط Tronscan.
    إن نجح يتحول الاشتراك تلقائيًا؛ وإلا تُرسل تنبيهات للأدمن للمراجعة.
    """
    parts = (m.text or "").strip().split(maxsplit=2)
    # الصيغة: /submit_tx <رقم_المرجع_أو_الرابط> <2w|4w>
    if len(parts) != 3 or parts[2] not in ("2w", "4w"):
        return await m.answer(
            "استخدم: <code>/submit_tx رقم_المرجع 2w</code> أو <code>/submit_tx رقم_المرجع 4w</code>\n"
            "يمكنك أيضًا إلصاق <i>رابط Tronscan</i> بدل رقم المرجع.",
            parse_mode="HTML"
        )

    ref_or_url, plan = parts[1], parts[2]
    min_amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD

    txid = extract_txid(ref_or_url)
    ok, info = find_trc20_transfer_to_me(ref_or_url, min_amount)

    if ok:
        with get_session() as s:
            dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
            end_at = approve_paid(s, m.from_user.id, plan, dur, tx_hash=txid or ref_or_url)
        return await m.answer(
            "✅ <b>تم التحقق من الدفع</b>\n"
            f"المبلغ المستلم: <b>{info} USDT</b>\n"
            f"⏳ الاشتراك فعّال حتى: <code>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</code>",
            parse_mode="HTML"
        )

    # فشل التحقق التلقائي — ننبّه الأدمن للمراجعة اليدوية
    alert = (
        "🔔 <b>طلب تفعيل — فشل التحقق التلقائي</b>\n"
        f"User: <code>{m.from_user.id}</code>\n"
        f"Plan: <b>{plan}</b>\n"
        f"Reference: <code>{_h(ref_or_url)}</code>\n"
        f"Reason: {_h(info)}"
    )
    await send_admins(alert)
    await m.answer(
        "❗ لم أستطع التحقق تلقائيًا من الدفع.\n"
        "سيتم مراجعته يدويًا قريبًا من قبل الدعم.\n"
        "تلميح: تأكد أن الإرسال كان USDT على شبكة TRON (TRC20) وأن رقم المرجع صحيح.",
        parse_mode="HTML"
    )

# ---------------------------
# أوامر الإدارة (لا تظهر للمستخدمين)
# ---------------------------
@dp.message(Command("admin_help"))
async def cmd_admin_help(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    txt = (
        "🛠️ <b>أوامر الأدمن</b>\n"
        "• <code>/approve &lt;user_id&gt; &lt;2w|4w&gt; [reference]</code> – تفعيل يدوي\n"
        "• <code>/broadcast &lt;text&gt;</code> – (اختياري) بث للمشتركين\n"
        "• <code>/force_report</code> – إرسال تقرير فوري"
    )
    await m.answer(txt, parse_mode="HTML")

@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = (m.text or "").strip().split()
    if len(parts) not in (3, 4) or parts[2] not in ("2w", "4w"):
        return await m.answer("استخدم: /approve <user_id> <2w|4w> [reference]")
    uid = int(parts[1])
    plan = parts[2]
    txh = parts[3] if len(parts) == 4 else None
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
    await m.answer(f"تم التفعيل للمستخدم {uid}. صالح حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}.")
    try:
        await bot.send_message(uid, "✅ تم تفعيل اشتراكك. أهلاً بك!", parse_mode="HTML")
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    txt = m.text.partition(" ")[2].strip()
    if not txt:
        return await m.answer("استخدم: /broadcast <text>")
    uids = list_active_user_ids()
    sent = 0
    for uid in uids:
        try:
            await bot.send_message(uid, txt, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.02)
        except Exception:
            pass
    await m.answer(f"تم الإرسال إلى {sent} مشترك.")

@dp.message(Command("force_report"))
async def cmd_force_report(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    with get_session() as s:
        stats_24 = get_stats_24h(s)
        stats_7d = get_stats_7d(s)
    await send_channel(_report_card(stats_24, stats_7d))
    await m.answer("تم إرسال التقرير للقناة.")

# ---------------------------
# فحوصات التشغيل
# ---------------------------
async def check_channel_and_admin_dm():
    ok = True
    # تأكد من القناة
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, "🤖 البوت بدأ العمل (Polling).", parse_mode="HTML")
        logger.info(f"CHANNEL OK: {TELEGRAM_CHANNEL_ID} / Bot_AI")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")
        ok = False

    # DM للأدمن للتأكيد
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت يعمل الآن.", parse_mode="HTML")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت في الخاص وتحقق من الـ ID.")
    return ok

# ---------------------------
# التشغيل
# ---------------------------
async def main():
    # 1) قاعدة البيانات
    init_db()

    # 2) تبادل OKX + الأسواق
    await load_okx_markets_and_filter()

    # 3) حذف أي Webhook لأننا نستعمل polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # 4) فحص القناة وإشعار الأدمن
    await check_channel_and_admin_dm()

    # 5) إطلاق المهام المتوازية
    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(monitor_open_trades())

    try:
        await asyncio.gather(t1, t2, t3, t4)
    except Exception as e:
        logger.exception(f"FATAL ERROR: {e}")
        try:
            await send_admins(f"❌ تعطل البوت: <code>{_h(str(e))}</code>")
        except Exception:
            pass
        raise

if __name__ == "__main__":
    asyncio.run(main())

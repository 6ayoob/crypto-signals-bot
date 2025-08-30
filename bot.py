# bot.py — بوت الإشارات + اشتراكات + تقارير + مراقبة الصفقات

import asyncio
import logging
from datetime import datetime, timedelta
import pytz
import time

import ccxt
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, ADMIN_USER_IDS, USDT_TRC20_WALLET,
    MAX_OPEN_TRADES, TIMEZONE, DAILY_REPORT_HOUR_LOCAL,
    PRICE_2_WEEKS_USD, PRICE_4_WEEKS_USD, SUB_DURATION_2W, SUB_DURATION_4W
)
from database import (
    init_db, get_session, is_active, start_trial, approve_paid,
    count_open_trades, add_trade, list_trades_with_status, update_trade_status,
    trades_stats_last_24h
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
exchange = ccxt.okx({"enableRateLimit": True})

# سنحدّث هذه بعد تحميل الأسواق:
ACTIVE_SYMBOLS = []

# ---------------------------
# أدوات مساعدة
# ---------------------------
def _now_ts() -> int:
    return int(time.time())

async def send_channel(text: str):
    """إرسال رسالة إلى القناة الخاصة بالإشارات"""
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

def format_signal(sig: dict) -> str:
    return (
        "🚀 **إشارة جديدة [BUY]**\n"
        "━━━━━━━━━━━━━━\n"
        f"🔹 الرمز: `{sig['symbol']}`\n"
        f"💵 الدخول: `{sig['entry']}`\n"
        f"📉 وقف الخسارة: `{sig['sl']}`\n"
        f"🎯 الهدف 1: `{sig['tp1']}`\n"
        f"🎯 الهدف 2: `{sig['tp2']}`\n"
        f"🕒 الوقت: `{sig['timestamp']}`\n"
        "━━━━━━━━━━━━━━\n"
        "⚡️ تذكير: التزم بإدارة رأس المال."
    )

# دعوات القناة (تجربة/مدفوع)
async def create_one_time_invite_link(days: int, label: str = "") -> str | None:
    """ينشئ رابط دعوة لمرة واحدة بصلاحية محددة الأيام (يتطلب أن يكون البوت أدمن بالقناة)."""
    try:
        expire_date = _now_ts() + days * 86400
        link = await bot.create_chat_invite_link(
            chat_id=TELEGRAM_CHANNEL_ID,
            name=label or f"invite_{expire_date}",
            expire_date=expire_date,
            member_limit=1
        )
        return link.invite_link
    except Exception as e:
        logger.warning(f"INVITE LINK ERROR: {e}")
        return None

async def invite_user_to_channel(user_id: int, days: int):
    link = await create_one_time_invite_link(days, f"user_{user_id}")
    if link:
        try:
            await bot.send_message(user_id, f"🎟️ رابط الدخول للقناة الخاصة (صالح {days} يوم):\n{link}")
        except Exception as e:
            logger.warning(f"USER DM INVITE ERROR: {e}")

async def kick_user_from_channel(user_id: int):
    """محاولة سحب الدخول (قد لا تنجح على القنوات دومًا)."""
    try:
        await bot.ban_chat_member(TELEGRAM_CHANNEL_ID, user_id)
        await bot.unban_chat_member(TELEGRAM_CHANNEL_ID, user_id)  # للسماح بإعادة الانضمام مستقبلًا
    except Exception as e:
        logger.warning(f"REVOKE FAIL (ignore if channel): {e}")

# ---------------------------
# Runtime toggles / limits
# ---------------------------
SCAN_PAUSED = False
MAX_OPEN_TRADES_RT = MAX_OPEN_TRADES

# مضاد سبام للإشارات
LAST_SIG_TS = {}                 # {symbol: last_sent_epoch}
PER_SYMBOL_COOLDOWN = 15 * 60    # 15 دقيقة
GLOBAL_SIG_BUCKET = []           # قائمة timestamps للإشارات آخر ساعة
GLOBAL_SIG_BUCKET_WINDOW = 3600
GLOBAL_SIG_BUCKET_MAX = 8

# تبسيط الدفع
PENDING_PLAN: dict[int, str] = {}  # {user_id: "2w"|"4w"}

def _env_sanity_checks():
    warn = []
    if not USDT_TRC20_WALLET:
        warn.append("USDT_TRC20_WALLET مفقود — لن يتمكن المستخدم من الدفع.")
    for w in warn:
        logger.warning(f"[SANITY] {w}")
    return warn

# ---------------------------
# أوامر عامة للمستخدم
# ---------------------------
async def welcome_text() -> str:
    return (
        "👋 **مرحبًا بك في عالم الفرص!** 🚀\n\n"
        "🔔 إشارات لحظية مبنية على توليفة اتجاه + زخم + حجم + ATR + مستويات S/R\n"
        f"📊 تقرير يومي الساعة **{DAILY_REPORT_HOUR_LOCAL}** صباحًا (بتوقيت السعودية)\n"
        f"⏱️ حد أقصى للصفقات المفتوحة: **{MAX_OPEN_TRADES_RT}**\n"
        "💰 إدارة مخاطرة منضبطة دومًا\n\n"
        "**خطط الاشتراك:**\n"
        f"• أسبوعان: **{PRICE_2_WEEKS_USD} USDT**\n"
        f"• 4 أسابيع: **{PRICE_4_WEEKS_USD} USDT**\n"
        f"محفظة الدفع (TRC20): `{USDT_TRC20_WALLET}`\n\n"
        "✨ جرّبنا مجانًا ليوم واحد بالضغط على الزر بالأسفل."
    )

@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 ابدأ التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="💳 الاشتراك والدفع", callback_data="subscribe_info")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="Markdown", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id, days=1)
    if ok:
        await q.message.edit_text("✅ تم تفعيل التجربة المجانية ليوم واحد.\nسندعوك للقناة الخاصة الآن…")
        await invite_user_to_channel(q.from_user.id, days=1)
    else:
        await q.message.edit_text("ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\nيمكنك الاشتراك عبر الزر أدناه أو الأمر /pay .")
    await q.answer()

@dp.callback_query(F.data == "subscribe_info")
async def cb_sub_info(q: CallbackQuery):
    await cmd_pay(q.message)
    await q.answer()

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ اشتراكك **نشط**." if ok else "❌ لا تملك اشتراكًا نشطًا.", parse_mode="Markdown")

@dp.message(Command("whoami"))
async def cmd_whoami(m: Message):
    await m.answer(f"🆔 معرّفك: `{m.from_user.id}`", parse_mode="Markdown")

# ===== تبسيط الدفع =====
@dp.message(Command("pay"))
async def cmd_pay(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"اشترك أسبوعين — {PRICE_2_WEEKS_USD} USDT", callback_data="pay_plan:2w")
    kb.button(text=f"اشترك 4 أسابيع — {PRICE_4_WEEKS_USD} USDT", callback_data="pay_plan:4w")
    kb.adjust(1)
    txt = (
        "💳 **الدفع بـ USDT (TRC20)**\n"
        "اختر خطتك أولًا، ثم سنرسل لك العنوان وخطوة إرسال الهاش.\n\n"
        "_لن تحتاج لكتابة الخطة داخل /submit_tx بعد الآن._"
    )
    await m.answer(txt, parse_mode="Markdown", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("pay_plan:"))
async def cb_pay_plan(q: CallbackQuery):
    plan = q.data.split(":")[1]  # 2w | 4w
    PENDING_PLAN[q.from_user.id] = plan
    amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD
    txt = (
        "💳 **خطوتان فقط**\n"
        f"1) أرسل **{amount} USDT** إلى محفظتنا (TRC20):\n`{USDT_TRC20_WALLET}`\n"
        "2) بعدها أرسل هاش التحويل عبر:\n`/submit_tx <txid>`\n\n"
        "ℹ️ كيف أجد الهاش؟ افتح تفاصيل المعاملة في محفظتك/TronScan وانسخ **Transaction Hash**."
    )
    await q.message.edit_text(txt, parse_mode="Markdown")
    await q.answer()

@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
    parts = m.text.strip().split()

    if len(parts) == 2 and m.from_user.id in PENDING_PLAN:
        txh = parts[1]
        plan = PENDING_PLAN[m.from_user.id]
    elif len(parts) == 3 and parts[2] in ("2w", "4w"):  # توافق مع الصيغة القديمة
        txh, plan = parts[1], parts[2]
    else:
        return await m.answer("استخدم: /submit_tx <txid>\n(اختر الخطة أولاً من /pay)")

    min_amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD
    ok, info = find_trc20_transfer_to_me(txh, min_amount)
    if ok:
        with get_session() as s:
            dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
            end_at = approve_paid(s, m.from_user.id, plan, dur, tx_hash=txh)
        PENDING_PLAN.pop(m.from_user.id, None)
        await m.answer(
            f"✅ تم التحقق من المعاملة ({info} USDT) وتفعيل اشتراكك تلقائيًا.\n"
            f"⏳ صالح حتى: `{end_at.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            "🎟️ نرسل لك رابط القناة الخاصة الآن…",
            parse_mode="Markdown"
        )
        # أرسل دعوة للقناة الخاصة لمدّة الاشتراك
        await invite_user_to_channel(m.from_user.id, days=(SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W))
        return

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
# أوامر الإدارة (لن تظهر للمستخدمين في قائمة الأوامر)
# ---------------------------
@dp.message(Command("approve"))
async def cmd_approve(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = m.text.strip().split()
    if len(parts) not in (3, 4):
        return await m.answer("استخدم: /approve <user_id> <2w|4w> [tx_hash]")
    uid = int(parts[1]); plan = parts[2]
    dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W
    txh = parts[3] if len(parts) == 4 else None
    with get_session() as s:
        end_at = approve_paid(s, uid, plan, dur, tx_hash=txh)
    await m.answer(f"تم التفعيل للمستخدم {uid}. حتى {end_at.strftime('%Y-%m-%d %H:%M UTC')}.")
    try:
        await bot.send_message(uid, "✅ تم تفعيل اشتراكك. أهلاً بك!")
        await invite_user_to_channel(uid, days=dur)
    except Exception as e:
        logger.warning(f"USER DM ERROR: {e}")

@dp.message(Command("ping_channel"))
async def cmd_ping_channel(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    try:
        await send_channel("✅ اختبار: القناة الخاصة متصلة.")
        await m.answer("أُرسلت رسالة اختبار للقناة.")
    except Exception as e:
        await m.answer(f"فشل الإرسال: {e}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) != 2:
        return await m.answer("استخدم: /broadcast <نص>")
    await send_channel(parts[1])
    await m.answer("تم الإرسال.")

@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    global SCAN_PAUSED
    SCAN_PAUSED = True
    await m.answer("⏸️ تم إيقاف حلقة المسح.")

@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    global SCAN_PAUSED
    SCAN_PAUSED = False
    await m.answer("▶️ تم استئناف حلقة المسح.")

@dp.message(Command("set_maxtrades"))
async def cmd_set_maxtrades(m: Message):
    if m.from_user.id not in ADMIN_USER_IDS:
        return
    parts = m.text.strip().split()
    if len(parts) != 2:
        return await m.answer("استخدم: /set_maxtrades <n>")
    global MAX_OPEN_TRADES_RT
    MAX_OPEN_TRADES_RT = max(0, int(parts[1]))
    await m.answer(f"تم ضبط حد الصفقات المفتوحة إلى {MAX_OPEN_TRADES_RT}.")

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

async def fetch_last_price(symbol: str) -> float | None:
    try:
        loop = asyncio.get_event_loop()
        ticker = await loop.run_in_executor(None, lambda: exchange.fetch_ticker(symbol))
        return float(ticker.get("last") or ticker.get("close") or 0.0)
    except Exception as e:
        logger.warning(f"FETCH_TICKER ERROR {symbol}: {e}")
        return None

async def scan_and_dispatch():
    """يمسح الرموز ويدفع الإشارات مع حدود مضاد سبام واحترام الإيقاف."""
    if SCAN_PAUSED:
        await asyncio.sleep(2)
        return

    now_ts = _now_ts()
    global GLOBAL_SIG_BUCKET
    GLOBAL_SIG_BUCKET = [t for t in GLOBAL_SIG_BUCKET if now_ts - t <= GLOBAL_SIG_BUCKET_WINDOW]

    for sym in ACTIVE_SYMBOLS:
        if len(GLOBAL_SIG_BUCKET) >= GLOBAL_SIG_BUCKET_MAX:
            logger.info("GLOBAL SIGNAL LIMIT reached; skipping remaining symbols this cycle.")
            break

        data5 = await fetch_ohlcv(sym, "5m", 150)
        if not data5:
            await asyncio.sleep(0.2)
            continue

        sig = check_signal(sym, data5)
        if sig:
            last = LAST_SIG_TS.get(sym, 0)
            if now_ts - last < PER_SYMBOL_COOLDOWN:
                await asyncio.sleep(0.1)
                continue

            with get_session() as s:
                if count_open_trades(s) < MAX_OPEN_TRADES_RT:
                    t = add_trade(s, sig["symbol"], sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])
                else:
                    logger.info(f"Max open trades {MAX_OPEN_TRADES_RT} reached; signal skipped for {sym}.")
                    await asyncio.sleep(0.2)
                    continue

            text = format_signal(sig)
            await send_channel(text)
            logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")

            LAST_SIG_TS[sym] = now_ts
            GLOBAL_SIG_BUCKET.append(now_ts)

        await asyncio.sleep(0.25)

async def loop_signals():
    """حلقة فحص الإشارات (كل 5 دقائق)."""
    while True:
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        await asyncio.sleep(300)

# ---------------------------
# مراقبة الصفقات (TP/SL)
# ---------------------------
def format_tp1(trade) -> str:
    return (
        "🎯 **TP1 تحقق**\n"
        f"🔹 {trade.symbol}\n"
        f"📥 الدخول: `{trade.entry}`\n"
        f"🎯 TP1: `{trade.tp1}`\n"
        f"📉 SL: `{trade.sl}`\n"
        "— تم تأمين جزء من الربح. نواصل المتابعة."
    )

def format_closed(trade, reason: str) -> str:
    emoji = "🏁" if reason == "TP2" else "🛑"
    line  = "الهدف النهائي تحقق ✅" if reason == "TP2" else "وقف الخسارة ضُرب ❌"
    return (
        f"{emoji} **إغلاق الصفقة — {line}**\n"
        f"🔹 {trade.symbol}\n"
        f"📥 الدخول: `{trade.entry}`\n"
        f"🎯 TP2: `{trade.tp2}` | 📉 SL: `{trade.sl}`\n"
        "شكراً للانضباط 👌"
    )

async def monitor_trades_loop():
    """مراقبة الصفقات المفتوحة وإرسال تنبيهات TP1/TP2/SL وإغلاق."""
    while True:
        try:
            with get_session() as s:
                trades = list_trades_with_status(s, ("open", "tp1"))
                for tr in trades:
                    price = await fetch_last_price(tr.symbol)
                    if not price or price <= 0:
                        await asyncio.sleep(0.05)
                        continue

                    if tr.side == "BUY":
                        if price <= tr.sl:
                            update_trade_status(s, tr.id, "closed_sl")
                            await send_channel(format_closed(tr, "SL"))
                        elif price >= tr.tp2:
                            update_trade_status(s, tr.id, "closed_tp2")
                            await send_channel(format_closed(tr, "TP2"))
                        elif price >= tr.tp1 and tr.status == "open":
                            update_trade_status(s, tr.id, "tp1")
                            await send_channel(format_tp1(tr))

                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.exception(f"MONITOR_LOOP ERROR: {e}")

        await asyncio.sleep(60)

# ---------------------------
# التقرير اليومي (صيغة مُحسّنة)
# ---------------------------
def format_daily_report(stats: dict, tzname: str) -> str:
    total = stats["total"]
    tp2   = stats["closed_tp2"]
    sl    = stats["closed_sl"]
    tp1   = stats["tp1_only"]
    open_ = stats["open"]

    win = tp2
    lose = sl
    wr = (win / max(1, (win + lose))) * 100

    lines = [
        "📊 **ملخّص إشارات آخر 24 ساعة**",
        "━━━━━━━━━━━━━━━━━━",
        f"🔔 إجمالي الإشارات: **{total}**",
        f"🏁 TP2 (إغلاق ربح كامل): **{tp2}**",
        f"🎯 TP1 (ربح جزئي): **{tp1}**",
        f"🛑 SL (إغلاق عند الوقف): **{sl}**",
        f"⏳ صفقات لا تزال مفتوحة: **{open_}**",
        "—",
        f"✅ **نسبة الفوز التقريبية**: **{wr:.0f}%**",
        "━━━━━━━━━━━━━━━━━━",
        "💡 تذكير: الالتزام بالخطة وإدارة رأس المال سرّ الاستمرارية.",
        f"🕘 التقرير يُرسل يوميًا الساعة {DAILY_REPORT_HOUR_LOCAL} صباحًا ({tzname})",
    ]
    return "\n".join(lines)

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
                stats = trades_stats_last_24h(s)
            txt = format_daily_report(stats, TIMEZONE)
            await send_channel(txt)
            logger.info("Daily report sent.")
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e}")

# ---------------------------
# إعداد قائمة الأوامر (إخفاء أوامر الأدمن عن الجميع)
# ---------------------------
async def setup_bot_commands():
    user_cmds = [
        BotCommand(command="start", description="بدء الاستخدام"),
        BotCommand(command="help", description="قائمة الأوامر"),
        BotCommand(command="status", description="حالة اشتراكك"),
        BotCommand(command="pay", description="الاشتراك والدفع"),
        BotCommand(command="submit_tx", description="تأكيد الدفع بالهاش"),
        BotCommand(command="whoami", description="عرض معرفك"),
    ]
    await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())

    admin_extra = [
        BotCommand(command="approve", description="تفعيل اشتراك يدوي"),
        BotCommand(command="ping_channel", description="فحص القناة الخاصة"),
        BotCommand(command="broadcast", description="بث في القناة"),
        BotCommand(command="pause", description="إيقاف المسح"),
        BotCommand(command="resume", description="استئناف المسح"),
        BotCommand(command="set_maxtrades", description="تغيير حد الصفقات"),
    ]
    for uid in ADMIN_USER_IDS:
        try:
            await bot.set_my_commands(user_cmds + admin_extra, scope=BotCommandScopeChat(chat_id=uid))
        except Exception as e:
            logger.warning(f"set_my_commands admin {uid} failed: {e}")

# ---------------------------
# التشغيل
# ---------------------------
async def main():
    logger.info("Initializing DB...")
    init_db()
    logger.info("DB initialized.")
    _env_sanity_checks()

    # حذف أي Webhook لأننا نستعمل polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # تحميل أسواق OKX وتصفية الرموز
    try:
        markets = exchange.load_markets()
        available = set(markets.keys())
        global ACTIVE_SYMBOLS
        ACTIVE_SYMBOLS = [s for s in SYMBOLS if s in available]
        skipped = [s for s in SYMBOLS if s not in available]
        logger.info(f"OKX markets loaded. Using {len(ACTIVE_SYMBOLS)} symbols, skipped {len(skipped)}: {skipped}")
    except Exception as e:
        logger.exception(f"LOAD MARKETS ERROR: {e}")
        return

    # فحص القناة الخاصة + DM للأدمن
    try:
        ch = await bot.get_chat(TELEGRAM_CHANNEL_ID)
        logger.info(f"CHANNEL OK: {TELEGRAM_CHANNEL_ID} / {ch.title or ch.username}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")

    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ العمل على Render (polling).")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت في الخاص وتحقق من الـ ID.")

    # إعداد قائمة الأوامر
    await setup_bot_commands()

    # تشغيل المهام
    t1 = asyncio.create_task(dp.start_polling(bot))
    t2 = asyncio.create_task(loop_signals())
    t3 = asyncio.create_task(daily_report_loop())
    t4 = asyncio.create_task(monitor_trades_loop())

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

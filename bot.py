# bot.py
import asyncio
import logging
import html as _html
from io import BytesIO

import ccxt
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
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
    count_open_trades, add_trade,
    create_invoice, get_invoice, list_pending_invoices, mark_invoice_paid
)
from strategy import check_signal
from symbols import SYMBOLS
from payments_tron import find_trc20_transfer_to_me, list_recent_incoming_usdt, match_invoices_by_amount

# ---------- Optional QR ----------
try:
    import qrcode
    HAVE_QR = True
except Exception:
    HAVE_QR = False

def esc(x: str) -> str: return _html.escape(str(x))

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ---------------- Bot / Exchange ----------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# استخدم OKX لتفادي قيود باينانس في بعض المناطق
exchange = ccxt.okx({"enableRateLimit": True})

OKX_MARKETS = []
VALID_SYMBOLS = set()

async def load_okx_markets():
    global OKX_MARKETS, VALID_SYMBOLS
    loop = asyncio.get_event_loop()
    markets = await loop.run_in_executor(None, exchange.load_markets)
    OKX_MARKETS = list(markets.keys())
    # رموزنا على شكل XXX/USDT - نتحقق من وجودها في OKX باسم XXX-USDT
    skipped = []
    used = []
    for s in SYMBOLS:
        inst = s.replace("/", "-")
        if inst in markets:
            used.append(s)
        else:
            skipped.append(s)
    VALID_SYMBOLS = set(used)
    logger.info(f"OKX markets loaded. Using {len(used)} symbols, skipped {len(skipped)}: {skipped}")

# ---------------- Helpers ----------------
async def send_channel(text: str):
    try:
        await bot.send_message(TELEGRAM_CHANNEL_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_channel error: {e}")

async def welcome_text() -> str:
    return (
        "👋 <b>أهلًا بك في عالم الفرص</b> 🚀<br><br>"
        "🔔 إشارات لحظية بإستراتيجية متعددة العوامل (اتجاه + زخم + حجم + S/R + ATR)<br>"
        f"📊 تقرير يومي الساعة <b>{DAILY_REPORT_HOUR_LOCAL}</b> صباحًا (بتوقيت السعودية)<br>"
        f"⏱ حد أقصى <b>{MAX_OPEN_TRADES}</b> صفقات مفتوحة<br>"
        "💰 إدارة مخاطرة صارمة<br><br>"
        "<b>الخطط:</b><br>"
        f"• أسبوعان: <b>{PRICE_2_WEEKS_USD}$</b><br>"
        f"• 4 أسابيع: <b>{PRICE_4_WEEKS_USD}$</b><br>"
        f"(USDT TRC20): <code>{esc(USDT_TRC20_WALLET)}</code><br><br>"
        "✨ جرّبنا مجانًا لمدة <b>يوم واحد</b> بالضغط على الزر."
    )

def build_pay_kb(invoice_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تم التحويل • تحقق الآن", callback_data=f"inv_verify:{invoice_id}")
    kb.button(text="❌ إلغاء الفاتورة", callback_data=f"inv_cancel:{invoice_id}")
    kb.adjust(1)
    return kb

# ---------------- Public Commands ----------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 التجربة المجانية (يوم واحد)", callback_data="start_trial")
    kb.button(text="💎 اشترك الآن", callback_data="subscribe")
    kb.adjust(1)
    await m.answer(await welcome_text(), parse_mode="HTML", reply_markup=kb.as_markup(), disable_web_page_preview=True)

@dp.callback_query(F.data == "start_trial")
async def cb_trial(q: CallbackQuery):
    with get_session() as s:
        ok = start_trial(s, q.from_user.id, days=1)
    if ok:
        await q.message.edit_text(
            "✅ تم تفعيل التجربة المجانية لمدة <b>يوم واحد</b>.\n"
            "ستصلك الإشارات والتقرير اليومي في القناة الخاصة.",
            parse_mode="HTML"
        )
    else:
        await q.message.edit_text(
            "ℹ️ لقد استخدمت التجربة المجانية مسبقًا.\n"
            "يمكنك الاشتراك عبر الزر أدناه.",
            parse_mode="HTML"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 اشترك الآن", callback_data="subscribe")
    await q.message.edit_reply_markup(kb.as_markup())
    await q.answer()

@dp.message(Command("status"))
async def cmd_status(m: Message):
    with get_session() as s:
        ok = is_active(s, m.from_user.id)
    await m.answer("✅ اشتراكك <b>نشط</b>." if ok else "❌ لا تملك اشتراكًا نشطًا.", parse_mode="HTML")

# ------------- اشتراك مبسّط بفاتورة -------------
@dp.callback_query(F.data == "subscribe")
async def cb_subscribe(q: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔹 أسبوعان ({PRICE_2_WEEKS_USD}$)", callback_data="plan:2w")
    kb.button(text=f"🔹 4 أسابيع ({PRICE_4_WEEKS_USD}$)", callback_data="plan:4w")
    kb.adjust(1)
    await q.message.edit_text(
        "اختر خطتك 👇",
        reply_markup=kb.as_markup()
    )
    await q.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(q: CallbackQuery):
    plan = q.data.split(":")[1]
    base = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD
    with get_session() as s:
        inv = create_invoice(s, q.from_user.id, plan, base_amount=base, validity_minutes=180)
    amount = inv.expected_amount

    txt = (
        "🧾 <b>فاتورة اشتراك</b><br>"
        f"المدة: <b>{'أسبوعان' if plan=='2w' else '4 أسابيع'}</b><br>"
        f"المبلغ المطلوب: <code>{amount:.6f} USDT</code><br>"
        f"المحفظة (TRC20):<br><code>{esc(USDT_TRC20_WALLET)}</code><br><br>"
        "حوّل <b>نفس المبلغ بالضبط</b> ثم اضغط زر <b>تحقق الآن</b> أدناه.<br>"
        "تنتهي صلاحية الفاتورة خلال <b>3 ساعات</b>."
    )
    kb = build_pay_kb(inv.id)

    if HAVE_QR:
        # اصنع QR لعنوان المحفظة فقط (أبسط وأضمن التوافق)
        img = qrcode.make(USDT_TRC20_WALLET)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        photo = FSInputFile(buf, filename=f"wallet_{inv.id}.png")
        try:
            await q.message.answer_photo(
                photo=photo,
                caption=txt,
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )
            await q.answer()
            return
        except Exception as e:
            logger.warning(f"QR send failed: {e}")

    await q.message.edit_text(txt, parse_mode="HTML", reply_markup=kb.as_markup())
    await q.answer()

@dp.callback_query(F.data.startswith("inv_verify:"))
async def cb_inv_verify(q: CallbackQuery):
    invoice_id = int(q.data.split(":")[1])
    with get_session() as s:
        inv = get_invoice(s, invoice_id)
        if not inv or inv.status != "pending":
            await q.answer("هذه الفاتورة غير متاحة.", show_alert=True)
            return
    # اسحب آخر التحويلات واطابق
    try:
        transfers = list_recent_incoming_usdt(limit=120)
    except Exception as e:
        await q.answer(f"تعذّر الفحص الآن: {e}", show_alert=True)
        return

    # شكل مبسّط لتمرير الفاتورة إلى الماتشَر
    mapping = match_invoices_by_amount(
        invoices=[{"id": inv.id, "expected_amount": inv.expected_amount}],
        incoming=transfers,
        tol=0.000001
    )
    if invoice_id not in mapping:
        await q.answer("لم أعثر على تحويل بالمبلغ المطلوب حتى الآن.\nجرّب بعد دقيقة.", show_alert=True)
        return

    hit = mapping[invoice_id]
    with get_session() as s:
        mark_invoice_paid(s, invoice_id, hit["txid"], hit["amount"])
        # طبق المدة
        dur = SUB_DURATION_2W if inv.plan == "2w" else SUB_DURATION_4W
        end_at = approve_paid(s, inv.tg_user_id, inv.plan, dur, tx_hash=hit["txid"])
    await q.message.edit_text(
        f"✅ تم تأكيد الدفع ({hit['amount']:.6f} USDT)\n"
        f"⏳ اشتراكك صالح حتى: <b>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</b>",
        parse_mode="HTML"
    )
    await q.answer()

@dp.callback_query(F.data.startswith("inv_cancel:"))
async def cb_inv_cancel(q: CallbackQuery):
    invoice_id = int(q.data.split(":")[1])
    with get_session() as s:
        inv = get_invoice(s, invoice_id)
        if inv and inv.status == "pending":
            inv.status = "cancelled"
            s.add(inv)
    await q.message.edit_text("تم إلغاء الفاتورة.", parse_mode="HTML")
    await q.answer()

# --------- احتياطي: إدخال هاش يدوي (إن أردت الاحتفاظ به) ----------
@dp.message(Command("submit_tx"))
async def cmd_submit(m: Message):
    parts = m.text.strip().split()
    if len(parts) not in (2,3):
        return await m.answer("استخدم: <code>/submit_tx &lt;tx_hash&gt; [2w|4w]</code>", parse_mode="HTML")
    txh = parts[1]
    plan = parts[2] if len(parts) == 3 and parts[2] in ("2w","4w") else None
    min_amount = PRICE_2_WEEKS_USD if plan == "2w" else PRICE_4_WEEKS_USD if plan else 0

    ok, info = find_trc20_transfer_to_me(txh, min_amount or 0.01)
    if not ok:
        return await m.answer(f"❗ لم أستطع التحقق تلقائيًا: {esc(info)}", parse_mode="HTML")

    with get_session() as s:
        # لو يوجد فاتورة معلّقة لنفس المستخدم بنفس المبلغ قرّبها
        inv = None
        if plan:
            inv = s.query(type(get_invoice.__annotations__.get('return'))).filter(False).first()  # dummy; نتجاهل
        dur = SUB_DURATION_2W if plan == "2w" else SUB_DURATION_4W if plan else SUB_DURATION_2W
        end_at = approve_paid(s, m.from_user.id, plan or "2w", dur, tx_hash=txh)
    await m.answer(
        f"✅ تم التحقق من المعاملة ({info} USDT) وتفعيل اشتراكك.\n"
        f"⏳ صالح حتى: <b>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</b>",
        parse_mode="HTML"
    )

# ---------------- Signals scanning ----------------
async def fetch_ohlcv(symbol: str, timeframe="5m", limit=150):
    try:
        loop = asyncio.get_event_loop()
        inst = symbol.replace("/", "-")  # OKX format
        return await loop.run_in_executor(
            None, lambda: exchange.fetch_ohlcv(inst, timeframe=timeframe, limit=limit)
        )
    except Exception as e:
        logger.warning(f"FETCH_OHLCV ERROR {symbol}: {e}")
        return []

async def scan_and_dispatch():
    for sym in list(VALID_SYMBOLS or SYMBOLS):
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
                f"💵 سعر الدخول: <code>{sig['entry']}</code>\n"
                f"📉 وقف الخسارة: <code>{sig['sl']}</code>\n"
                f"🎯 الهدف 1: <code>{sig['tp1']}</code>\n"
                f"🎯 الهدف 2: <code>{sig['tp2']}</code>\n"
                f"⏰ الوقت: <code>{sig['timestamp']}</code>\n"
                "━━━━━━━━━━━━━━\n⚡️ إدارة رأس المال قبل كل صفقة."
            )
            await send_channel(text)
            logger.info(f"SIGNAL SENT: {sig['symbol']} entry={sig['entry']} tp1={sig['tp1']} tp2={sig['tp2']}")
        await asyncio.sleep(0.4)

async def loop_signals():
    while True:
        try:
            await scan_and_dispatch()
        except Exception as e:
            logger.exception(f"SCAN_LOOP ERROR: {e}")
        await asyncio.sleep(300)

# ---------------- Daily report ----------------
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
            # مبدئيًا: تقرير بسيط ومحفّز
            await send_channel(
                "📊 <b>التقرير اليومي</b>\n"
                "━━━━━━━━━━━━━━\n"
                "• تم نشر إشارات مدروسة خلال آخر 24 ساعة.\n"
                "• إدارة مخاطرة منضبطة وحد أقصى للصفقات.\n"
                "• استمر بالالتزام بالخطة واستفد من الفرص.\n"
                "━━━━━━━━━━━━━━\n"
                f"🕘 {DAILY_REPORT_HOUR_LOCAL}:00 صباحًا (بتوقيت السعودية)"
            )
            logger.info("Daily report sent.")
        except Exception as e:
            logger.exception(f"DAILY_REPORT ERROR: {e}")

# -------------- Payments auto checker --------------
async def payments_auto_loop():
    """
    كل 30 ثانية:
      - يجلب التحويلات الحديثة لمحفظتنا
      - يطابق فواتير pending حسب المبلغ المميّز
      - يفعّل الاشتراك تلقائيًا
    """
    while True:
        try:
            with get_session() as s:
                pend = list_pending_invoices(s, within_hours=24)
                if not pend:
                    await asyncio.sleep(30)
                    continue
                invs = [{"id": i.id, "expected_amount": i.expected_amount, "plan": i.plan, "tg_user_id": i.tg_user_id} for i in pend]
            incoming = list_recent_incoming_usdt(limit=200)
            mapping = match_invoices_by_amount(invoices=invs, incoming=incoming, tol=0.000001)
            if not mapping:
                await asyncio.sleep(30)
                continue
            for inv_id, hit in mapping.items():
                with get_session() as s:
                    inv = get_invoice(s, inv_id)
                    if not inv or inv.status != "pending":
                        continue
                    mark_invoice_paid(s, inv_id, hit["txid"], hit["amount"])
                    dur = SUB_DURATION_2W if inv.plan == "2w" else SUB_DURATION_4W
                    end_at = approve_paid(s, inv.tg_user_id, inv.plan, dur, tx_hash=hit["txid"])
                # أخبر المستخدم
                try:
                    await bot.send_message(
                        inv.tg_user_id,
                        f"✅ تم تأكيد الدفع ({hit['amount']:.6f} USDT)\n"
                        f"⏳ اشتراكك صالح حتى: <b>{end_at.strftime('%Y-%m-%d %H:%M UTC')}</b>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(f"USER DM error after auto-approve: {e}")
        except Exception as e:
            logger.exception(f"PAYMENTS LOOP ERROR: {e}")
        await asyncio.sleep(30)

# -------------- Admin-only guard --------------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_USER_IDS

# -------------- Startup --------------
async def main():
    logger.info("Initializing DB...")
    init_db()
    logger.info("DB initialized.")

    # احذف أي Webhook لأننا نستعمل polling
    try:
        await bot.session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "true"}
        )
        logger.info("Webhook deleted; starting polling.")
    except Exception as e:
        logger.warning(f"DELETE_WEBHOOK WARN: {e}")

    # تحقق من القناة/الأدمن
    try:
        ch = await bot.get_chat(TELEGRAM_CHANNEL_ID)
        logger.info(f"CHANNEL OK: {TELEGRAM_CHANNEL_ID} / {ch.title or ch.full_name or ch.id}")
    except Exception as e:
        logger.error(f"CHANNEL CHECK FAILED: {e} — تأكد من إضافة البوت كمشرف وضبط TELEGRAM_CHANNEL_ID.")

    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, "✅ البوت بدأ العمل.")
            logger.info(f"ADMIN DM OK: {admin_id}")
        except Exception as e:
            logger.warning(f"ADMIN DM FAILED for {admin_id}: {e} — أرسل /start للبوت في الخاص وتحقق من الـ ID.")

    await load_okx_markets()

    # شغّل المهام
    t_poll = asyncio.create_task(dp.start_polling(bot))
    t_signals = asyncio.create_task(loop_signals())
    t_reports = asyncio.create_task(daily_report_loop())
    t_pay = asyncio.create_task(payments_auto_loop())

    try:
        await asyncio.gather(t_poll, t_signals, t_reports, t_pay)
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

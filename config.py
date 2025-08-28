# config.py — إعدادات المشروع (اقرأ من متغيرات البيئة على Render)
import os
from datetime import timedelta

def _required(key: str) -> str:
    """اقرأ متغيرًا ضروريًا وإلا ارفع خطأ واضح في اللوج."""
    val = os.getenv(key)
    if not val:
        raise ValueError(f"Missing required environment variable: {key}")
    return val

# ---------------------------
# مفاتيح تيليجرام
# ---------------------------
# ⚠️ أمنيًا: لا تضع قيمة افتراضية للتوكن — يجب ضبطه من لوحة Render
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")

# معرف القناة بصيغة int (مثال: -1002800980577)
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))

# قائمة الأدمن — أرقام مفصولة بفواصل
# مثال في Render: ADMIN_USER_IDS="658712542"
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",") if x.strip()}

# ---------------------------
# الدفع و TronGrid
# ---------------------------
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "TJC9gbV1AJ2Y9S8uq8gj1xcd3hs2T53VpD")

# مفاتيح TronGrid للتحقق التلقائي (المفتاح مطلوب)
TRONGRID_API_KEY = _required("TRONGRID_API_KEY")
TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io")
# عقد USDT (اختياري — يمكن تركه فارغًا لو نتحقق عبر الحدث والوجهة فقط)
USDT_TRC20_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "").strip()

# ---------------------------
# قاعدة البيانات
# ---------------------------
# على Render: اضبط DATABASE_URL من خدمة Postgres
# محليًا يمكن ترك SQLite للتجربة
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local.db")

# ---------------------------
# الإعدادات الزمنية والتقارير
# ---------------------------
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))

# ---------------------------
# حدود وإعدادات التداول
# ---------------------------
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "8"))

# الأسعار/الخطط
PRICE_2_WEEKS_USD = float(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = float(os.getenv("PRICE_4_WEEKS_USD", "60"))

# التجربة المجانية
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "2"))

# مدد الاشتراك (timedelta) — متوافقة مع استخدامك في bot.py
SUB_DURATION_2W = timedelta(days=14)
SUB_DURATION_4W = timedelta(days=28)

# config.py — إعدادات المشروع (استخدم المتغيرات البيئية على Render)
import os
from datetime import timedelta

# توكن البوت والقناة (يفضّل وضعها كمتغيرات بيئية على Render)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8483309506:AAEe3bA4DTrLOXXPDNJS3W3Gnttau8LEXQg")
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))  # قناتك @crypto_AI_signals_2

# أدمن (أنت فقط)
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",") if x.strip()]

# محفظة USDT TRC20 للدفع
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "TJC9gbV1AJ2Y9S8uq8gj1xcd3hs2T53VpD")

# قاعدة البيانات (Postgres على Render)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local.db")

# إعدادات الزمن والتقرير اليومي
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))

# حدود الصفقات
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "8"))

# التسعير
PRICE_2_WEEKS_USD = float(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = float(os.getenv("PRICE_4_WEEKS_USD", "60"))

# التجربة المجانية
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "2"))

# مدد الاشتراك
SUB_DURATION_2W = timedelta(days=14)
SUB_DURATION_4W = timedelta(days=28)

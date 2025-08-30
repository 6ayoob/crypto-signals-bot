# config.py — يُفضّل تمرير الإعدادات عبر متغيرات البيئة في Render

import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # ضعه في Render
# مثال: -1002800980577
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))

# يمكن تمرير عدة مُدراء مفصولين بفواصل: "111,222,333"
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",")]

USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")  # عنوان محفظة TRC20

# إعدادات عامة
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "8"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))  # 9 صباحاً

# خطط الاشتراك
PRICE_2_WEEKS_USD = int(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = int(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W = int(os.getenv("SUB_DURATION_2W", "14"))
SUB_DURATION_4W = int(os.getenv("SUB_DURATION_4W", "28"))

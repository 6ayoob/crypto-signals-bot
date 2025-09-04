# config.py — إعدادات مبسطة مع مفاتيح الإحالة والرسالة اليومية

import os

def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

# ========= تلغرام =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",")]

# دعم تواصل خاص (اختياري)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))

# ========= دفع/سعر/محفظة =========
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")
PRICE_2_WEEKS_USD = int(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = int(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W   = int(os.getenv("SUB_DURATION_2W", "14"))
SUB_DURATION_4W   = int(os.getenv("SUB_DURATION_4W", "28"))

# ========= المنطقة الزمنية والتقارير =========
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))

# رسالة تحفيزية يومية (مرّة/يوم)
DAILY_PEP_MSG_ENABLED = _as_bool(os.getenv("DAILY_PEP_MSG_ENABLED", "1"), True)
DAILY_PEP_MSG_HOUR_LOCAL = int(os.getenv("DAILY_PEP_MSG_HOUR_LOCAL", "10"))
DAILY_PEP_MSG_TEXT = os.getenv(
    "DAILY_PEP_MSG_TEXT",
    "🌤️ صباح الخير—هدفنا اليوم الربح التراكمي. رجاء الالتزام بـ TP1 / SL والانضباط: لا مطاردة للشموع، والتعادل بعد TP1.\nخطوة اليوم تبني مكسب بكرة. شدّوا حيلكم 👊✨"
)

# ========= الإشارات والمتابعة =========
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))
MONITOR_INTERVAL_SEC     = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME                = os.getenv("TIMEFRAME", "5m")
SCAN_BATCH_SIZE          = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY          = int(os.getenv("MAX_CONCURRENCY", "5"))

# ========= حدود/تكرار =========
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES", "8"))
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))

# ========= إدارة المخاطر =========
MAX_DAILY_LOSS_R   = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK  = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS     = int(os.getenv("COOLDOWN_HOURS", "6"))
COOLDOWN_ALERT_ADMIN_ONLY = _as_bool(os.getenv("COOLDOWN_ALERT_ADMIN_ONLY", "1"), True)

# ========= OKX Rate Limiter =========
OKX_PUBLIC_RATE_MAX    = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))
OKX_PUBLIC_RATE_WINDOW = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))

# ========= قفل القيادة =========
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL       = int(os.getenv("LEADER_TTL", "300"))

# ========= قفل ملف محلّي =========
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", "/tmp/mk1_ai_bot.lock")

# ========= الإحالات =========
# مكافأة المُحيل: يوم مجاني عند تفعيل المُحال بخطة مدفوعة
REF_REWARD_MODE = os.getenv("REF_REWARD_MODE", "paid")  # "paid" | "trial_or_paid"
GIFT_ONE_DAY_HOURS = int(os.getenv("GIFT_ONE_DAY_HOURS", "24"))

# ========= فحوصات =========
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN مفقود! ضعه في بيئة التشغيل.")

# config.py — تهيئة عبر متغيرات البيئة (Render) مع قيم افتراضية معقولة

import os

def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None: 
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

# ========= تلغرام أساسي =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # إلزامي
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",") if x.strip()]

# تواصل خاص مع الأدمن (اختياري)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")      # بدون @
SUPPORT_CHAT_ID  = int(os.getenv("SUPPORT_CHAT_ID", "0")) # tg user id

# ========= الدفع/الدعوات =========
USDT_TRC20_WALLET   = os.getenv("USDT_TRC20_WALLET", "")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "")  # إن وفّرته، لن ينشئ روابط مؤقتة
TRIAL_INVITE_HOURS  = int(os.getenv("TRIAL_INVITE_HOURS", "24"))

# ========= الإشارات والتقارير =========
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))  # 9 صباحاً بتوقيت الرياض

# ========= رسالة صباحية تحفيزية (pep message) =========
DAILY_PEP_MSG_ENABLED    = _as_bool(os.getenv("DAILY_PEP_MSG_ENABLED", "1"), True)
DAILY_PEP_MSG_HOUR_LOCAL = int(os.getenv("DAILY_PEP_MSG_HOUR_LOCAL", "9"))
DAILY_PEP_MSG_TEXT = os.getenv(
    "DAILY_PEP_MSG_TEXT",
    "🌞 صباح الخير يا أبطال! هدفنا اليوم هو <b>الربح التراكمي</b>.\n"
    "رجاءً الالتزام بـ <b>TP1/SL</b>، والانضباط قبل كل شيء. يوم موفّق للجميع 🚀"
)

# ========= خطط الاشتراك =========
PRICE_2_WEEKS_USD = int(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = int(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W   = int(os.getenv("SUB_DURATION_2W", "14"))  # بالأيام
SUB_DURATION_4W   = int(os.getenv("SUB_DURATION_4W", "28"))  # بالأيام

# ========= ضبط مسح الإشارات/المتابعة =========
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))  # كل 5 دقائق
MONITOR_INTERVAL_SEC     = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME                = os.getenv("TIMEFRAME", "5m")
SCAN_BATCH_SIZE          = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY          = int(os.getenv("MAX_CONCURRENCY", "5"))

# ========= حدود ومنع تكرار =========
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES", "8"))
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))

# ========= إدارة المخاطر =========
MAX_DAILY_LOSS_R  = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))  # حد خسارة يومي (R-)
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS    = int(os.getenv("COOLDOWN_HOURS", "6"))
# لو 1: تنبيه الإيقاف المؤقّت يُرسل للآدمن فقط (كما طلبت)
COOLDOWN_ALERT_ADMIN_ONLY = _as_bool(os.getenv("COOLDOWN_ALERT_ADMIN_ONLY", "1"), True)

# ========= نظام الإحالة =========
# "paid": كافئ المُحيل فقط إذا فعّل المُحال خطة مدفوعة. "any": كافئ عند أي تفعيل.
REF_REWARD_MODE    = os.getenv("REF_REWARD_MODE", "paid")
GIFT_ONE_DAY_HOURS = int(os.getenv("GIFT_ONE_DAY_HOURS", "24"))  # مكافأة الإحالة (بالساعات)

# ========= OKX Rate Limiter =========
OKX_PUBLIC_RATE_MAX    = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))
OKX_PUBLIC_RATE_WINDOW = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))

# ========= قفل/قيادة (لمنع ازدواج العمال) =========
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL       = int(os.getenv("LEADER_TTL", "300"))

# ========= قفل ملف محلي (اختياري) =========
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", "/tmp/mk1_ai_bot.lock")

# ========= فحوصات =========
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN مفقود! ضعه في متغيرات البيئة على Render.")

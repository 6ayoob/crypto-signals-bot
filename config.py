# config.py — تهيئة عبر متغيرات البيئة (Render) مع قيم افتراضية معقولة
# مضاف: إعدادات نظام الإحالة + رسالة الصباح التحفيزية

import os

def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

# ========= تلغرام أساسي =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # إلزامي
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",")]

# ========= تواصل خاص مع الأدمن =========
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")         # بدون @
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))     # tg id

# ========= إعدادات الدفع/الصورة =========
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")
PAY_GUIDE_LOCAL_PATH = os.getenv("PAY_GUIDE_LOCAL_PATH", "")
PAY_GUIDE_FILE_ID   = os.getenv("PAY_GUIDE_FILE_ID", "")
PAY_GUIDE_URL       = os.getenv("PAY_GUIDE_URL", "")

# ========= التوقيت والتقارير =========
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))

# ========= رسالة الصباح التحفيزية (مرة يوميًّا) =========
MORNING_MSG_ENABLED = _as_bool(os.getenv("MORNING_MSG_ENABLED", "1"), True)
MORNING_MSG_HOUR_LOCAL = int(os.getenv("MORNING_MSG_HOUR_LOCAL", "8"))
# النص الافتراضي باللهجة العامية:
MORNING_MSG_TEXT = os.getenv(
    "MORNING_MSG_TEXT",
    "☀️ صباح الخير يا أبطال! هدفنا اليوم هو الربح التراكمي. نرجو الالتزام بـ TP1 / SL — والرزق على الله 🙏"
)

# ========= خطط الاشتراك =========
PRICE_2_WEEKS_USD = int(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = int(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W   = int(os.getenv("SUB_DURATION_2W", "14"))
SUB_DURATION_4W   = int(os.getenv("SUB_DURATION_4W", "28"))

# ========= ضبط مسح الإشارات/المتابعة =========
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))
MONITOR_INTERVAL_SEC     = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
TIMEFRAME                = os.getenv("TIMEFRAME", "5m")
SCAN_BATCH_SIZE          = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY          = int(os.getenv("MAX_CONCURRENCY", "5"))

# ========= حدود ومنع تكرار =========
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES", "8"))
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))

# ========= إدارة المخاطر V2 =========
MAX_DAILY_LOSS_R   = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))
MAX_LOSSES_STREAK  = int(os.getenv("MAX_LOSSES_STREAK", "3"))
COOLDOWN_HOURS     = int(os.getenv("COOLDOWN_HOURS", "6"))

# إرسال تنبيه التهدئة (Cooldown) للآدمن فقط
SEND_COOLDOWN_TO_ADMINS_ONLY = _as_bool(os.getenv("SEND_COOLDOWN_TO_ADMINS_ONLY", "1"), True)

# ========= OKX Rate Limiter =========
OKX_PUBLIC_RATE_MAX    = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))
OKX_PUBLIC_RATE_WINDOW = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"))

# ========= قفل/قيادة =========
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL       = int(os.getenv("LEADER_TTL", "300"))

# ========= قفل ملف محلي =========
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", "/tmp/mk1_ai_bot.lock")

# ========= نظام الإحالة =========
# يمنح المُحيل REF_BONUS_HOURS ساعة عند تفعيل المدعو اشتراكًا مدفوعًا (2w/4w).
REF_BONUS_HOURS = int(os.getenv("REF_BONUS_HOURS", "24"))     # ساعات المكافأة (افتراضي يوم)
ALLOW_SELF_REFERRAL = _as_bool(os.getenv("ALLOW_SELF_REFERRAL", "0"), False)
# تلميح: سيتم توليد ref_code لكل مستخدم تلقائيًا (مثل R3F9K2).

# ========= فحوصات بسيطة =========
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN مفقود! ضعه في متغيرات البيئة على Render.")

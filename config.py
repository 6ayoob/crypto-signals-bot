# config.py — تهيئة عبر متغيرات البيئة (Render) مع قيم افتراضية معقولة
# ملاحظة: أي قيمة حساسة (مثل التوكن) يجب ضبطها في Environment وليس هنا.

import os
from datetime import timedelta

def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

# ========= تلغرام أساسي =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # ضعها في Render (إلزامي)
# مثال لقنوات تيليجرام: -100xxxxxxxxxx
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"))

# عدة مدراء مفصولين بفواصل: "111,222,333"
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",")]

# ========= تواصل خاص مع الأدمن (اختياري) =========
# استخدم واحدًا منها أو كليهما (يُقرأان مباشرة أيضًا في bot.py من البيئة):
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")      # بدون @ (مثال: tayyib_pro)
SUPPORT_CHAT_ID  = int(os.getenv("SUPPORT_CHAT_ID", "0")) # معرّف الأدمن (tg://user?id=...)

# ========= إعدادات الدفع/الصورة =========
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")   # عنوان محفظة TRC20

# أولوية الإرسال في البوت: ملف محلي ← file_id ← URL
PAY_GUIDE_LOCAL_PATH = os.getenv("PAY_GUIDE_LOCAL_PATH", "")  # مثال: assets/payment_guide.jpg
PAY_GUIDE_FILE_ID   = os.getenv("PAY_GUIDE_FILE_ID", "")      # ضع file_id الذي استخرجناه
PAY_GUIDE_URL       = os.getenv("PAY_GUIDE_URL", "")          # رابط مباشر للصورة (اختياري)

# ========= الإشارات والتقارير =========
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))  # 9 صباحاً

# ========= خطط الاشتراك (timedelta) =========
PRICE_2_WEEKS_USD = int(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = int(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W   = timedelta(days=int(os.getenv("SUB_DURATION_2W", "14")))
SUB_DURATION_4W   = timedelta(days=int(os.getenv("SUB_DURATION_4W", "28")))

# ========= خيارات دعوات/تذكير (اختيارية) =========
CHANNEL_INVITE_LINK     = os.getenv("CHANNEL_INVITE_LINK", "")  # رابط دعوة ثابت للقناة (إن رغبت)
TRIAL_INVITE_HOURS      = int(os.getenv("TRIAL_INVITE_HOURS", "24"))
KICK_CHECK_INTERVAL_SEC = int(os.getenv("KICK_CHECK_INTERVAL_SEC", "3600"))
REMINDER_BEFORE_HOURS   = int(os.getenv("REMINDER_BEFORE_HOURS", "4"))
GIFT_ONE_DAY_HOURS      = int(os.getenv("GIFT_ONE_DAY_HOURS", "24"))

# ========= ضبط مسح الإشارات/المتابعة =========
SIGNAL_SCAN_INTERVAL_SEC = int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"))  # كل 5 دقائق
MONITOR_INTERVAL_SEC     = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))       # متابعة الصفقات
TIMEFRAME                = os.getenv("TIMEFRAME", "5m")
SCAN_BATCH_SIZE          = int(os.getenv("SCAN_BATCH_SIZE", "10"))
MAX_CONCURRENCY          = int(os.getenv("MAX_CONCURRENCY", "5"))

# ========= حدود ومنع تكرار =========
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES", "8"))
DEDUPE_WINDOW_MIN = int(os.getenv("DEDUPE_WINDOW_MIN", "90"))  # منع تكرار إشارة لنفس الرمز خلال 90 دقيقة

# ========= إدارة المخاطر V2 =========
MAX_DAILY_LOSS_R  = float(os.getenv("MAX_DAILY_LOSS_R", "2.0"))  # حد خسارة يومي (R-)
MAX_LOSSES_STREAK = int(os.getenv("MAX_LOSSES_STREAK", "3"))     # خسائر متتالية قبل التهدئة
COOLDOWN_HOURS    = int(os.getenv("COOLDOWN_HOURS", "6"))        # مدة التهدئة بالساعات

# ========= OKX Rate Limiter =========
OKX_PUBLIC_RATE_MAX    = int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"))     # طلبات لكل نافذة
OKX_PUBLIC_RATE_WINDOW = float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2")) # مدة النافذة بالثواني

# ========= قفل/قيادة (لمنع ازدواج العمال) =========
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL       = int(os.getenv("LEADER_TTL", "300"))  # ثوانٍ

# ========= قفل ملف محلي (اختياري) =========
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", "/tmp/mk1_ai_bot.lock")  # على ويندوز يُنشئ mk1_ai_bot.lock بالمجلد

# ========= فحوصات بسيطة =========
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN مفقود! ضعه في متغيرات البيئة على Render.")

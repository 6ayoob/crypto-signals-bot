# config.py — تهيئة عبر متغيرات البيئة (Render) مع قيم افتراضية معقولة
# ملاحظة: أي قيمة حساسة (مثل TELEGRAM_BOT_TOKEN) يجب ضبطها في Environment وليس هنا.

from pathlib import Path
import os

# ===== أدوات مساعدة =====
def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _as_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default

# ===== مسارات تشغيل موحّدة =====
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ملف الرموز (قابل للكتابة داخل APP_DATA_DIR)
STRATEGY_SYMBOLS_FILENAME = os.getenv("STRATEGY_SYMBOLS_FILENAME", "strategy_crypto_v2_5_symbols.py")
STRATEGY_SYMBOLS_PATH = APP_DATA_DIR / STRATEGY_SYMBOLS_FILENAME

# ===== تيليجرام =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # إلزامي عبر البيئة
TELEGRAM_CHANNEL_ID = _as_int("TELEGRAM_CHANNEL_ID", -1002800980577)
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "658712542").split(",") if x.strip()]

# قناة/يوزر دعم (اختياري)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")      # بدون @
SUPPORT_CHAT_ID = _as_int("SUPPORT_CHAT_ID", 0)           # tg://user?id=...

# ===== الدفع/الصورة =====
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")

# أولوية: ملف محلي ← file_id ← URL
PAY_GUIDE_LOCAL_PATH = os.getenv("PAY_GUIDE_LOCAL_PATH", "")
PAY_GUIDE_FILE_ID    = os.getenv("PAY_GUIDE_FILE_ID", "")
PAY_GUIDE_URL        = os.getenv("PAY_GUIDE_URL", "")

# ===== الإشارات والتقارير =====
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = _as_int("DAILY_REPORT_HOUR_LOCAL", 9)

# ===== الخطط والأسعار =====
PRICE_2_WEEKS_USD = _as_int("PRICE_2_WEEKS_USD", 30)
PRICE_4_WEEKS_USD = _as_int("PRICE_4_WEEKS_USD", 60)
SUB_DURATION_2W   = _as_int("SUB_DURATION_2W", 14)
SUB_DURATION_4W   = _as_int("SUB_DURATION_4W", 28)

# ===== مسح الإشارات/المتابعة =====
SIGNAL_SCAN_INTERVAL_SEC = _as_int("SIGNAL_SCAN_INTERVAL_SEC", 300)  # كل 5 دقائق
MONITOR_INTERVAL_SEC     = _as_int("MONITOR_INTERVAL_SEC", 15)       # متابعة الصفقات
TIMEFRAME                = os.getenv("TIMEFRAME", "5m")
SCAN_BATCH_SIZE          = _as_int("SCAN_BATCH_SIZE", 10)
MAX_CONCURRENCY          = _as_int("MAX_CONCURRENCY", 5)

# ===== حدود ومنع تكرار =====
MAX_OPEN_TRADES   = _as_int("MAX_OPEN_TRADES", 8)
DEDUPE_WINDOW_MIN = _as_int("DEDUPE_WINDOW_MIN", 90)

# ===== إدارة المخاطر V2 =====
MAX_DAILY_LOSS_R  = _as_float("MAX_DAILY_LOSS_R", 2.0)
MAX_LOSSES_STREAK = _as_int("MAX_LOSSES_STREAK", 3)
COOLDOWN_HOURS    = _as_int("COOLDOWN_HOURS", 6)

# ===== OKX Rate Limiter =====
OKX_PUBLIC_RATE_MAX    = _as_int("OKX_PUBLIC_RATE_MAX", 18)
OKX_PUBLIC_RATE_WINDOW = _as_float("OKX_PUBLIC_RATE_WINDOW", 2.0)

# ===== Leader/Locks =====
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller")
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc")
LEADER_TTL       = _as_int("LEADER_TTL", 300)

# ===== قفل ملف محلي (اختياري) =====
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", "/tmp/mk1_ai_bot.lock")

# ===== فحص أساسي =====
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN مفقود! ضعه في متغيرات البيئة على Render.")

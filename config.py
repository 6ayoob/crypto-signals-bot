# -*- coding: utf-8 -*-
"""
config.py — تهيئة عبر متغيرات البيئة (متوافق مع Render) مع قيم افتراضية معقولة.
ملاحظة: أي قيمة حساسة (مثل التوكن) يجب ضبطها في Environment وليس في الكود.
"""

from __future__ import annotations
import os
from pathlib import Path

APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR") or "/tmp/market-watchdog").resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = os.getenv("STRATEGY_STATE_FILE") or str(APP_DATA_DIR / "strategy_state.json")

# ========= Helpers =========
def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _as_int(v: str | None, default: int) -> int:
    try:
        return int(str(v).strip()) if v is not None else int(default)
    except Exception:
        return int(default)

def _as_float(v: str | None, default: float) -> float:
    try:
        return float(str(v).strip()) if v is not None else float(default)
    except Exception:
        return float(default)

def _as_list_int(csv_: str | None, default_csv: str = "") -> list[int]:
    raw = csv_ if (csv_ is not None and str(csv_).strip() != "") else default_csv
    out = []
    for p in str(raw).split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except Exception:
            # تجاهل القطع غير القابلة للتحويل
            continue
    return out

# ========= مسارات تشغيل موحّدة =========
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ملف الرموز: يُخزَّن داخل مجلد البيانات لضمان الكتابة على Render
STRATEGY_SYMBOLS_FILENAME = os.getenv("STRATEGY_SYMBOLS_FILENAME", "strategy_crypto_v2_5_symbols.py")
STRATEGY_SYMBOLS_PATH = APP_DATA_DIR / STRATEGY_SYMBOLS_FILENAME

# ========= تلغرام =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # إلزامي: ضعه في Render
TELEGRAM_CHANNEL_ID = _as_int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002800980577"), -1000000000000)

# عدة مدراء مفصولين بفواصل: "111,222,333"
ADMIN_USER_IDS = _as_list_int(os.getenv("ADMIN_USER_IDS", "658712542"), "658712542")

# ========= تواصل خاص مع الأدمن =========
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip()     # بدون @
SUPPORT_CHAT_ID  = _as_int(os.getenv("SUPPORT_CHAT_ID", "0"), 0) # tg://user?id=...

# ========= إعدادات الدفع/الصورة =========
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "").strip()

# أولوية الإرسال في البوت: ملف محلي ← file_id ← URL
PAY_GUIDE_LOCAL_PATH = os.getenv("PAY_GUIDE_LOCAL_PATH", "").strip()
PAY_GUIDE_FILE_ID    = os.getenv("PAY_GUIDE_FILE_ID", "").strip()
PAY_GUIDE_URL        = os.getenv("PAY_GUIDE_URL", "").strip()

# ========= الإشارات والتقارير =========
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh").strip()   # اسم منطقة IANA
DAILY_REPORT_HOUR_LOCAL = _as_int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"), 9)  # 9 صباحاً

# ========= خطط الاشتراك =========
PRICE_2_WEEKS_USD = _as_int(os.getenv("PRICE_2_WEEKS_USD", "30"), 30)
PRICE_4_WEEKS_USD = _as_int(os.getenv("PRICE_4_WEEKS_USD", "60"), 60)
SUB_DURATION_2W   = _as_int(os.getenv("SUB_DURATION_2W", "14"), 14)
SUB_DURATION_4W   = _as_int(os.getenv("SUB_DURATION_4W", "28"), 28)

# ========= ضبط مسح الإشارات/المتابعة =========
SIGNAL_SCAN_INTERVAL_SEC = _as_int(os.getenv("SIGNAL_SCAN_INTERVAL_SEC", "300"), 300)  # كل 5 دقائق
MONITOR_INTERVAL_SEC     = _as_int(os.getenv("MONITOR_INTERVAL_SEC", "15"), 15)       # متابعة الصفقات
TIMEFRAME                = os.getenv("TIMEFRAME", "5m").strip()
SCAN_BATCH_SIZE          = _as_int(os.getenv("SCAN_BATCH_SIZE", "10"), 10)
MAX_CONCURRENCY          = _as_int(os.getenv("MAX_CONCURRENCY", "5"), 5)

# ========= حدود ومنع تكرار =========
MAX_OPEN_TRADES   = _as_int(os.getenv("MAX_OPEN_TRADES", "8"), 8)
DEDUPE_WINDOW_MIN = _as_int(os.getenv("DEDUPE_WINDOW_MIN", "90"), 90)  # منع تكرار إشارة لنفس الرمز خلال 90 دقيقة

# ========= إدارة المخاطر V2 =========
MAX_DAILY_LOSS_R   = _as_float(os.getenv("MAX_DAILY_LOSS_R", "2.0"), 2.0)  # حد خسارة يومي (R-)
MAX_LOSSES_STREAK  = _as_int(os.getenv("MAX_LOSSES_STREAK", "3"), 3)      # خسائر متتالية قبل التهدئة
COOLDOWN_HOURS     = _as_int(os.getenv("COOLDOWN_HOURS", "6"), 6)         # مدة التهدئة بالساعات

# ========= OKX Rate Limiter =========
OKX_PUBLIC_RATE_MAX    = _as_int(os.getenv("OKX_PUBLIC_RATE_MAX", "18"), 18)       # طلبات لكل نافذة
OKX_PUBLIC_RATE_WINDOW = _as_float(os.getenv("OKX_PUBLIC_RATE_WINDOW", "2"), 2.0)  # مدة النافذة بالثواني

# ========= قفل/قيادة (لمنع ازدواج العمال) =========
ENABLE_DB_LOCK   = _as_bool(os.getenv("ENABLE_DB_LOCK", "1"), True)
LEADER_LOCK_NAME = os.getenv("LEADER_LOCK_NAME", "telebot_poller").strip()
SERVICE_NAME     = os.getenv("SERVICE_NAME", "svc").strip()
LEADER_TTL       = _as_int(os.getenv("LEADER_TTL", "300"), 300)  # ثوانٍ

# ========= قفل ملف محلي (اختياري) =========
BOT_INSTANCE_LOCK = os.getenv("BOT_INSTANCE_LOCK", str(APP_DATA_DIR / "mk1_ai_bot.lock")).strip()

# ========= فحوصات بسيطة =========
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN مفقود! ضعه في متغيرات البيئة على Render (Dashboard → Environment)."
    )

# تشخيصات خفيفة (اختيارية)
try:
    print(f"[config] DATA_DIR={APP_DATA_DIR}  | CHANNEL_ID={TELEGRAM_CHANNEL_ID} | ADMINS={ADMIN_USER_IDS}")
except Exception:
    pass

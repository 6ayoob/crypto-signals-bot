# config_check.py
import os, sys, re
from typing import Tuple, Optional

# --- أدوات مساعدة ---
TRUE_SET  = {"1","true","yes","y","on"}
FALSE_SET = {"0","false","no","n","off"}

def parse_bool(k: str, default: Optional[bool]=None) -> Tuple[Optional[bool], Optional[str]]:
    v = os.getenv(k)
    if v is None:
        return default, None
    lv = v.strip().lower()
    if lv in TRUE_SET:  return True, None
    if lv in FALSE_SET: return False, None
    return default, f"{k}: قيمة بوليانية غير صالحة '{v}' (استخدم true/false أو 1/0)"

def parse_float(k: str, default: Optional[float]=None) -> Tuple[Optional[float], Optional[str]]:
    v = os.getenv(k)
    if v is None:
        return default, None
    raw = v.strip()
    if "," in raw:
        return default, f"{k}: استخدمت فاصلة ',' كفاصل عشري '{raw}' — استبدلها بنقطة '.' (مثال 0.020)"
    try:
        return float(raw), None
    except ValueError:
        return default, f"{k}: قيمة رقمية غير صالحة '{raw}'"

def parse_int(k: str, default: Optional[int]=None) -> Tuple[Optional[int], Optional[str]]:
    v = os.getenv(k)
    if v is None:
        return default, None
    try:
        return int(v.strip()), None
    except ValueError:
        return default, f"{k}: قيمة عدد صحيح غير صالحة '{v}'"

def present(k: str) -> bool:
    return os.getenv(k) is not None

# --- خرائط التطبيع/التعارض ---
ALIASES = {
    # فضّل نسخة PACKA لو عندك منطق نظام يعتمدها
    "EMA_VWAP_TWO_OF_THREE": "PACKA_REGIME_EMA_VWAP_TWO_OF_THREE",
    # لو النظام يستخدم USE_AVWAP فقط، وحضرتك تملك USE_ANCHORED_VWAP
    # يمكن اعتبارهما متعارضين وظيفيًا، سنبلغ عنهما.
}

PREFER_TRUE_FALSE = [
    "EMA_VWAP_TWO_OF_THREE",
    "PACKA_REGIME_EMA_VWAP_TWO_OF_THREE",
    "USE_VWAP",
    "USE_ANCHORED_VWAP",
    "PACKA_REGIME_USE_AVWAP",
]

FLOAT_KEYS = [
    "ATR_EPS_ABS_ADD","ATR_EPS_REL_ADD","ATR_EXTRA_EXPAND","ATR_LOWER_FLOOR",
    "ATR_PCT_MAX","ATR_PCT_MIN","ATR_WIDEN_HI","ATR_WIDEN_LO",
    "AVWAP_CONFLUENCE_EPS","BREAKOUT_BUFFER","MAX_BRK_DIST_ATR",
    "MIN_T1_GAP_FLOOR","PACKA_ATR_GATE_EPS_ABS_ADD","PACKA_ATR_GATE_EPS_REL_ADD",
    "PACKA_CONFLUENCE_LOCATION_EPS_PCT","PACKA_PROTECTIONS_SLIPPAGE_MAX_PCT",
    "PACKA_PROTECTIONS_SPREAD_MAX_PCT","PACKA_PROTECTIONS_STOP_ATR_MULT",
    "QV_THR_SCALE","SLIPPAGE_MAX_BP","SPREAD_MAX_BP","VWAP_MAX_DIST_PCT",
    "VWAP_TOL_BELOW","TP1_R","T1_ENTRY_GAP_MIN"
]

INT_KEYS = [
    "ADMIN_USER_IDS","AUTO_RELAX_AFTER_HRS_1","AUTO_RELAX_AFTER_HRS_2",
    "BRK_HOUR_START","BRK_HOUR_END","GIFT_ONE_DAY_HOURS",
    "LEADER_TTL","MAX_BARS_TO_TP1_BASE","MAX_OPEN_TRADES",
    "MINBAR_FLOOR_USD","MINBAR_SOFTEN_AFTER_MIN","MIN_24H_USD_VOL",
    "MIN_BAR_QUOTE_VOL_USD","QUOTE_VOL_MIN","RVOL_MIN","RVOL_MIN_FLOOR",
    "SIGNAL_SCAN_INTERVAL_SEC","SILENCE_SOFTEN_HOURS","SWEEP_BUFFER_TICKS",
    "TARGET_SIGNALS_PER_DAY","TARGET_SYMBOLS_COUNT","TIME_EXIT_DEFAULT_BARS",
    "TRIAL_DAYS","INSTANCES"
]

KNOWN_KEYS = set(FLOAT_KEYS + INT_KEYS + [
    # مفاتيح نصية/أخرى
    "ALLOW_RED_SETUP","AUTO_EXPAND_SYMBOLS","AVWAP_CONFLUENCE_EPS",
    "BREADTH_MIN_RATIO","COOLDOWN_ALERT_ADMIN_ONLY","DAILY_PEP_MSG_ENABLED",
    "DAILY_PEP_MSG_HOUR_LOCAL","DAILY_REPORT_HOUR_LOCAL","DATABASE_URL",
    "DEBUG_SYMBOLS","EMA_VWAP_ALIGN_SOFT","ENABLE_DB_LOCK","EXCHANGE",
    "INST_TYPE","LEADER_LOCK_NAME","MACD_COOLING_ALLOW","MARKET_MODE",
    "MAX_ADVERSE_EXCURSION_R","OUTLIER_Z_MAX","PACKA_ATR_GATE_ENABLED",
    "PACKA_ATR_GATE_HYSTERESIS_BPS","PACKA_CONFLUENCE_AGREE_TWO_OF_THREE",
    "PACKA_CONFLUENCE_ENABLED","PACKA_CONFLUENCE_REQUIRE_TREND_OR_LOCATION",
    "PACKA_CONFLUENCE_TREND_SOURCES","PACKA_PROTECTIONS_DEPTH_5BPS_MIN_USD",
    "PACKA_PROTECTIONS_STOP_TYPE","PACKA_REGIME_ENABLED",
    "PACKA_REGIME_RVOL_MIN_FLOOR","PACKA_REGIME_USE_AVWAP",
    "PAY_GUIDE_FILE_ID","PIP_PREFER_BINARY","PRICE_2_WEEKS_USD","PRICE_4_WEEKS_USD",
    "PYTHON_VERSION","QV_THR_SCALE","RECLAIM_METHOD","REF_BONUS_DAYS",
    "REF_REWARD_MODE","REQUIRE_RECLAIM_FOR_HARD_STOP","RISK_MODE","RVOL_MIN",
    "SELECTIVITY_MODE","SERVICE_NAME","SHOW_REF_IN_START","STRATEGY_LOG_REJECTS",
    "SUPPORT_USERNAME","SWEEP_STOP_K","SYMBOLS_REFRESH_HOURS","TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID","TIMEFRAME","TIMEZONE","TRAIL_AFTER_TP1","TRAIL_AFTER_TP2",
    "TRAIL_ATR_MULT_TP1","TRAIL_ATR_MULT_TP2","TRONGRID_API_KEY","TRONGRID_BASE",
    "USDT_TRC20_WALLET","USE_SOFT_STOP_SECONDS","USE_SYMBOLS_CACHE","USE_VWAP",
    "VWAP_MAX_DIST_PCT","VWAP_TOL_BELOW","USE_ANCHORED_VWAP",
    # مفاتيح قد تظهر بصيغة أخرى
    "Instances", # سنبلغ بتحويلها إلى INSTANCES
    "PACKA_REGIME_EMA_VWAP_TWO_OF_THREE","EMA_VWAP_TWO_OF_THREE",
])

# --- الفحص ---
def check():
    errors = []
    warns  = []
    info   = []

    # 1) فواصل عشرية خاطئة
    for k in FLOAT_KEYS:
        _, err = parse_float(k)
        if err: errors.append(err)

    # 2) أعداد صحيحة
    for k in INT_KEYS:
        _, err = parse_int(k)
        if err: errors.append(err)

    # 3) بوليان مفضّل كنص
    for k in PREFER_TRUE_FALSE:
        v = os.getenv(k)
        if v is None: 
            continue
        lv = v.strip().lower()
        if lv not in TRUE_SET | FALSE_SET:
            warns.append(f"{k}: يُفضّل استخدام true/false (الحالي='{v}')")

    # 4) حالات خاصة ذكرتها لك
    if present("Instances"):
        warns.append("Instances موجودة: يُفضّل استخدام INSTANCES بدلًا منها")
    if present("EMA_VWAP_TWO_OF_THREE") and present("PACKA_REGIME_EMA_VWAP_TWO_OF_THREE"):
        warns.append("تعارض: كلتا EMA_VWAP_TWO_OF_THREE و PACKA_REGIME_EMA_VWAP_TWO_OF_THREE موجودتان — أبقِ واحدة فقط")
    if present("USE_ANCHORED_VWAP") and present("PACKA_REGIME_USE_AVWAP"):
        warns.append("تنبيه: USE_ANCHORED_VWAP و PACKA_REGIME_USE_AVWAP قد يتداخلان وظيفيًا — ثبّت سياسة واحدة")
    if present("QUOTE_VOL_MIN") and present("MIN_BAR_QUOTE_VOL_USD"):
        warns.append("QUOTE_VOL_MIN زائد غالبًا بوجود MIN_BAR_QUOTE_VOL_USD — احذفه إن لم يكن مستخدمًا")

    # 5) مفاتيح غير معروفة
    env_keys = set(os.environ.keys())
    unknown  = sorted(k for k in env_keys if k.isupper() and k not in KNOWN_KEYS)
    if unknown:
        warns.append("مفاتيح غير معروفة: " + ", ".join(unknown))

    # 6) اقتراحات التطبيع
    if os.getenv("ATR_PCT_MAX") and "," in os.getenv("ATR_PCT_MAX",""):
        errors.append("ATR_PCT_MAX: استبدل الفاصلة إلى ATR_PCT_MAX=0.020")

    if present("Instances") and not present("INSTANCES"):
        info.append("سياسة: أنشئ INSTANCES=1 واحذف Instances")

    # 7) طباعة ملخّص مقروء
    print("=== ENV CHECK REPORT ===")
    if info:
        print("\n[INFO]")
        for m in info: print("- " + m)
    if warns:
        print("\n[WARN]")
        for m in warns: print("- " + m)
    if errors:
        print("\n[ERROR]")
        for m in errors: print("- " + m)

    # اعتبر الأخطاء قاتلة
    if errors:
        print("\nهناك أخطاء يجب إصلاحها قبل التشغيل.")
        sys.exit(1)
    else:
        print("\nلا توجد أخطاء قاتلة. يمكن المتابعة.")

if __name__ == "__main__":
    check()

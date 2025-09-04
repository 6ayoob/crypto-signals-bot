# symbols.py — قائمة أزواج التداول + توسّع تلقائي من OKX
# الميزات:
# - تنظيف التكرارات وتطبيع الصيَغ (AAA/USDT).
# - جلب أزواج SPOT/USDT من OKX مرتبة تقريبياً بالسيولة (volUsd/volCcy24h/vol*last).
# - فلترة اختيارية حسب سيولة دنيا بالدولار + قوائم تضمين/استبعاد من البيئة.
# - كاش محلي لتشغيل موثوق عند انقطاع الشبكة.
#
# ملاحظات:
# - يعمل الملف عند الاستيراد ويضبط المتغيّر SYMBOLS النهائي مباشرة.
# - لا يعتمد على ccxt، بل يستدعي REST لـ OKX مباشرة (requests). في حال عدم توفر requests،
#   سيعود إلى القائمة الأساسية بعد تنظيفها أو يستخدم الكاش إن وُجد.
#
# متغيرات البيئة (اختيارية):
#   AUTO_EXPAND_SYMBOLS=1            تفعيل التوسعة (افتراضي 1)
#   TARGET_SYMBOLS_COUNT=100         الهدف النهائي لعدد الأزواج (افتراضي 100)
#   USE_SYMBOLS_CACHE=1              تفعيل الكاش (افتراضي 1)
#   SYMBOLS_CACHE_PATH=...           مسار ملف الكاش (افتراضي symbols_cache.json بجانب الملف)
#   OKX_BASE=https://www.okx.com     أساس واجهة OKX (افتراضي كما هو)
#   OKX_TIMEOUT_SEC=12               مهلة الطلبات بالثواني (افتراضي 12)
#   DEBUG_SYMBOLS=0                  طباعة تشخيصية (افتراضي 0)
#   MIN_24H_USD_VOL=0                حد سيولة دنيا بالدولار (افتراضي 0 = تعطيل)
#   EXCLUDE_STABLES=1                استبعاد الأزواج ذات أصول مستقرة (USDC/USDT, DAI/USDT, ..) (افتراضي 1)
#   EXCLUDE_MEME=0                   استبعاد مجموعة عملات ميم شائعة (افتراضي 0)
#   INCLUDE_SYMBOLS="BTC/USDT,..."   إجبار تضمين أزواج محددة
#   EXCLUDE_SYMBOLS="ABC/USDT,..."   إجبار استبعاد أزواج محددة
#
# ملاحظة: حتى مع الفلاتر، سيُحافظ على أي زوج وضعته صراحة في INCLUDE_SYMBOLS.

import os, time, random, json
from typing import Iterable, List, Tuple

try:
    import requests
except Exception:
    requests = None  # fallback بدون شبكة

# ===== قائمتك الأساسية (كما أرسلت سابقًا) =====
BASE_SYMBOLS = [
  # DeFi
  "AAVE/USDT", "UNI/USDT", "SUSHI/USDT", "COMP/USDT", "MKR/USDT",
  "SNX/USDT", "CRV/USDT", "LDO/USDT", "GRT/USDT", "LINK/USDT",
  # Layer 1
  "ETH/USDT", "SOL/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
  "ALGO/USDT", "ATOM/USDT", "DOT/USDT", "BNB/USDT", "FET/USDT",
  # Gaming/Metaverse
  "MANA/USDT", "AXS/USDT", "SAND/USDT", "CHZ/USDT", "ENJ/USDT",
  "GALA/USDT", "APE/USDT", "ILV/USDT",
  # Layer 2 / Infra
  "OP/USDT", "IMX/USDT", "LUNA/USDT", "ZIL/USDT", "ZRX/USDT", "SKL/USDT",
  # Meme Coins
  "PEPE/USDT", "DOGE/USDT", "SHIB/USDT", "PUMP/USDT", "MEMEFI/USDT",
  # Stable / Oracles / Infra
  "USDC/USDT", "DAI/USDT", "BAND/USDT", "API3/USDT", "AVAX/USDT",
  "LINK/USDT", "RSR/USDT", "UMA/USDT", "KNC/USDT", "BICO/USDT",
  # AI / Web3 / Others
  "RENDER/USDT", "AIXBT/USDT", "VRA/USDT", "GLMR/USDT", "T/USDT",
  "PSTAKE/USDT", "BADGER/USDT", "PHA/USDT", "NC/USDT", "BOME/USDT",
  # OKX meme list
  "DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "PENGU/USDT",
  "BONK/USDT", "TRUMP/USDT", "FLOKI/USDT", "POLYDOGE/USDT",
  "WIF/USDT", "TURBO/USDT", "NOT/USDT", "ORDI/USDT",
  "DEGEN/USDT", "MEME/USDT", "DOGS/USDT", "VINE/USDT",
  "CAT/USDT", "ELON/USDT",
]

# ===== خيارات التوسيع/الفلترة =====
AUTO_EXPAND_SYMBOLS = bool(int(os.getenv("AUTO_EXPAND_SYMBOLS", "1")))
TARGET_SYMBOLS_COUNT = int(os.getenv("TARGET_SYMBOLS_COUNT", "100"))
USE_SYMBOLS_CACHE    = bool(int(os.getenv("USE_SYMBOLS_CACHE", "1")))
DEBUG_SYMBOLS        = bool(int(os.getenv("DEBUG_SYMBOLS", "0")))

OKX_BASE     = os.getenv("OKX_BASE", "https://www.okx.com")
TICKERS_URL  = f"{OKX_BASE}/api/v5/market/tickers?instType=SPOT"
TIMEOUT_SEC  = int(os.getenv("OKX_TIMEOUT_SEC", "12"))
CACHE_PATH   = os.getenv("SYMBOLS_CACHE_PATH", os.path.join(os.path.dirname(__file__), "symbols_cache.json"))

MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "0"))  # 0 = تعطيل
EXCLUDE_STABLES = bool(int(os.getenv("EXCLUDE_STABLES", "1")))
EXCLUDE_MEME    = bool(int(os.getenv("EXCLUDE_MEME", "0")))

def _parse_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [s.strip().upper().replace("-", "/").replace("_", "/") for s in raw.split(",") if s.strip()]

INCLUDE_SYMBOLS = _parse_csv_env("INCLUDE_SYMBOLS")
EXCLUDE_SYMBOLS = set(_parse_csv_env("EXCLUDE_SYMBOLS"))

# مجموعات مساعدة
_STABLE_BASES = {"USDC", "DAI", "TUSD", "USDD", "FDUSD", "USDE", "USDT"}  # استبعاد base=stable/* إن فُعّل
_MEME_BASES = {
    "PEPE", "DOGE", "SHIB", "ELON", "FLOKI", "WIF", "BONK", "PENGU",
    "TURBO", "NOT", "TRUMP", "DEGEN", "MEME", "DOGS", "VINE", "CAT"
}
_LEVERAGED_SUFFIXES = {"3L", "3S", "5L", "5S", "UP", "DOWN"}  # احتياطياً

# ===== أدوات مساعدة =====
def _normalize_symbol(s: str) -> str:
    return str(s).strip().upper().replace("-", "/").replace("_", "/")

def _dedupe_keep_order(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        x = _normalize_symbol(x)
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _alias_symbol(s: str) -> str:
    # تصحيح شائع: Render => RNDR في معظم المنصات
    if s == "RENDER/USDT":
        return "RNDR/USDT"
    return s

def _is_leveraged(sym: str) -> bool:
    base = sym.split("/", 1)[0]
    return any(base.endswith(suf) for suf in _LEVERAGED_SUFFIXES)

def _okx_get_json(url: str, attempts: int = 3):
    if requests is None:
        return None
    for a in range(attempts):
        try:
            r = requests.get(url, timeout=TIMEOUT_SEC)
            if r.status_code == 429:
                time.sleep((2 ** a) + random.random()); continue
            r.raise_for_status()
            j = r.json()
            if str(j.get("code", "0")) not in ("0", "200"):
                time.sleep((2 ** a) + random.random()); continue
            return j
        except Exception:
            time.sleep((2 ** a) + random.random())
    return None

def _fetch_okx_usdt_spot_ranked() -> List[Tuple[str, float]]:
    """
    يرجع قائمة (symbol, vol_usd_approx) لأزواج SPOT/USDT مرتبة تنازليًا حسب السيولة.
    """
    j = _okx_get_json(TICKERS_URL, attempts=3)
    if not j:
        return []
    rows: List[Tuple[str, float]] = []
    for it in j.get("data", []):
        inst = str(it.get("instId", "")).upper()  # مثل BTC-USDT
        if not inst.endswith("-USDT"):
            continue
        # تقدير الحجم بالدولار
        vol_usd = 0.0
        for key in ("volUsd", "volCcy24h", "vol24h"):
            v = it.get(key)
            if v:
                try:
                    # volUsd جاهز، volCcy24h = حجم بالعملة المقابلة غالباً USD/USDT، vol24h = عدد العملات (نضربها بالسعر)
                    if key == "vol24h":
                        last = float(it.get("last", 0.0) or 0.0)
                        vol_usd = float(v) * last
                    else:
                        vol_usd = float(v)
                    break
                except Exception:
                    pass
        sym = inst.replace("-", "/")
        rows.append((sym, float(vol_usd)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows

def _read_cache() -> List[str]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [_normalize_symbol(x) for x in data if isinstance(x, str)]
    except Exception:
        return []

def _write_cache(symbols: List[str]) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(symbols, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _filter_symbol(sym: str) -> bool:
    """
    يعيد True إذا كان الرمز صالحًا للاستخدام بعد الفلاتر العامة.
    """
    base = sym.split("/", 1)[0]
    if _is_leveraged(sym):
        return False
    if EXCLUDE_STABLES and base in _STABLE_BASES:
        return False
    if EXCLUDE_MEME and base in _MEME_BASES:
        return False
    if sym in EXCLUDE_SYMBOLS:
        return False
    return True

def _expand_symbols_okx(existing_symbols: Iterable[str], target: int = 100) -> List[str]:
    """
    يحافظ على الموجود (إن كان مدعومًا على OKX) ثم يُكمّل تلقائيًا حتى يصل للهدف.
    يراعي الفلاتر وحد السيولة MIN_24H_USD_VOL إن فُعّل.
    """
    # طبّع + تصحيح + أزل التكرارات
    base = [_alias_symbol(_normalize_symbol(s)) for s in existing_symbols]
    base = _dedupe_keep_order(base)

    # إجبار تضمين رموز من البيئة (قبل أي فلترة لاحقة)
    for add in INCLUDE_SYMBOLS:
        if add not in base:
            base.append(add)

    ranked = _fetch_okx_usdt_spot_ranked()
    if not ranked:
        # لا إنترنت: جرّب الكاش
        if USE_SYMBOLS_CACHE:
            cached = _read_cache()
            if cached:
                keep = [s for s in base if s in cached and _filter_symbol(s)]
                extras = [s for s in cached if s not in keep and _filter_symbol(s)]
                out = (keep + extras)[:target]
                if DEBUG_SYMBOLS:
                    print(f"[symbols] cache fallback → {len(out)}")
                return out
        # لا إنترنت ولا كاش: ارجع للقائمة الأساسية بعد الفلترة
        out = [s for s in base if _filter_symbol(s)][:target]
        if DEBUG_SYMBOLS:
            print(f"[symbols] no OKX/no cache → using base ({len(out)})")
        return out

    okx_syms = [s for s, v in ranked]
    okx_set = set(okx_syms)

    # حافظ على ما لديك (إن موجود على OKX) وبعد الفلترة
    keep = [s for s in base if (s in okx_set) and _filter_symbol(s)]

    # أضف من الأعلى سيولةً مع مراعاة حد السيولة الدنيا إن طُلِب
    extras: List[str] = []
    for sym, vol in ranked:
        if sym in keep:
            continue
        if not _filter_symbol(sym):
            continue
        if MIN_24H_USD_VOL > 0 and vol < MIN_24H_USD_VOL:
            continue
        extras.append(sym)

    out = (keep + extras)[:target]

    if USE_SYMBOLS_CACHE:
        _write_cache(out)

    if DEBUG_SYMBOLS:
        missing = [s for s in base if s not in okx_set]
        print(f"[symbols] kept={len(keep)}, added={len(out)-len(keep)}, "
              f"missing_from_okx={missing[:8]}, min_vol={MIN_24H_USD_VOL}")
    return out

# ===== تنفيذ التوسيع عند الاستيراد =====
def _prepare_symbols() -> List[str]:
    if not AUTO_EXPAND_SYMBOLS:
        base = [_alias_symbol(_normalize_symbol(s)) for s in BASE_SYMBOLS]
        base = _dedupe_keep_order(base)
        # تضمين/استبعاد
        for add in INCLUDE_SYMBOLS:
            if add not in base:
                base.append(add)
        out = [s for s in base if _filter_symbol(s) and s not in EXCLUDE_SYMBOLS][:TARGET_SYMBOLS_COUNT]
        if DEBUG_SYMBOLS:
            print(f"[symbols] static only: {len(out)} pairs")
        return out
    try:
        return _expand_symbols_okx(BASE_SYMBOLS, target=TARGET_SYMBOLS_COUNT)
    except Exception:
        # أي خطأ غير متوقع: نظّف التكرارات واقصّ للهدف
        fallback = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in BASE_SYMBOLS])
        out = [s for s in fallback if _filter_symbol(s)][:TARGET_SYMBOLS_COUNT]
        return out

SYMBOLS: List[str] = _prepare_symbols()

if DEBUG_SYMBOLS:
    print(f"[symbols] ready: {len(SYMBOLS)} pairs | first 10: {SYMBOLS[:10]}")

__all__ = ["SYMBOLS"]

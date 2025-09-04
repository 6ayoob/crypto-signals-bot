# symbols.py — قائمة 70 عملة + توسعة تلقائية من OKX حتى N (افتراضي 100)
# يتحكم عبر متغيرات بيئة: SKIP_STABLES, SYMBOLS_BLACKLIST, SYMBOLS_WHITELIST, MIN_USD_VOL_24H
# المتطلبات: requests

import os, time, random, json
from typing import Iterable, List

try:
    import requests
except Exception:
    requests = None  # fallback بدون شبكة

# ===== قائمتك الأساسية (كما أرسلتها) =====
SYMBOLS = [
  # DeFi (10)
  "AAVE/USDT", "UNI/USDT", "SUSHI/USDT", "COMP/USDT", "MKR/USDT",
  "SNX/USDT", "CRV/USDT", "LDO/USDT", "GRT/USDT", "LINK/USDT",
  # Layer 1 (10)
  "ETH/USDT", "SOL/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
  "ALGO/USDT", "ATOM/USDT", "DOT/USDT", "BNB/USDT", "FET/USDT",
  # Gaming/Metaverse (8)
  "MANA/USDT", "AXS/USDT", "SAND/USDT", "CHZ/USDT", "ENJ/USDT",
  "GALA/USDT", "APE/USDT", "ILV/USDT",
  # Layer 2 (+ إضافات)
  "OP/USDT", "IMX/USDT", "LUNA/USDT", "ZIL/USDT", "ZRX/USDT", "SKL/USDT",
  # Meme Coins (5)
  "PEPE/USDT", "DOGE/USDT", "SHIB/USDT", "PUMP/USDT", "MEMEFI/USDT",
  # Stable / Oracles / Infra (10)
  "USDC/USDT", "DAI/USDT", "BAND/USDT", "API3/USDT", "AVAX/USDT",
  "LINK/USDT", "RSR/USDT", "UMA/USDT", "KNC/USDT", "BICO/USDT",
  # AI / Web3 / Others (10)
  "RENDER/USDT", "AIXBT/USDT", "VRA/USDT", "GLMR/USDT", "T/USDT",
  "PSTAKE/USDT", "BADGER/USDT", "PHA/USDT", "NC/USDT", "BOME/USDT",
  # رموز ميم من OKX
  "DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "PENGU/USDT",
  "BONK/USDT", "TRUMP/USDT", "FLOKI/USDT", "POLYDOGE/USDT",
  "WIF/USDT", "TURBO/USDT", "NOT/USDT", "ORDI/USDT",
  "DEGEN/USDT", "MEME/USDT", "DOGS/USDT", "VINE/USDT",
  "CAT/USDT", "ELON/USDT",
]

# ===== خيارات التوسيع/الكاش (قائمة على الشبكة) =====
AUTO_EXPAND_SYMBOLS = bool(int(os.getenv("AUTO_EXPAND_SYMBOLS", "1")))   # عطّلها بوضع 0
TARGET_SYMBOLS_COUNT = int(os.getenv("TARGET_SYMBOLS_COUNT", "100"))     # الهدف: 100
USE_SYMBOLS_CACHE    = bool(int(os.getenv("USE_SYMBOLS_CACHE", "1")))    # كاش ملف
DEBUG_SYMBOLS        = bool(int(os.getenv("DEBUG_SYMBOLS", "0")))        # طباعة تشخيصية

OKX_BASE     = os.getenv("OKX_BASE", "https://www.okx.com")
TICKERS_URL  = f"{OKX_BASE}/api/v5/market/tickers?instType=SPOT"  # يحوي last/vol24h...
TIMEOUT_SEC  = int(os.getenv("OKX_TIMEOUT_SEC", "12"))
CACHE_PATH   = os.getenv("SYMBOLS_CACHE_PATH", os.path.join(os.path.dirname(__file__), "symbols_cache.json"))

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
    """تصحيحات بسيطة اختيارية لبعض الأسماء الشائعة الخاطئة."""
    # مثال شائع: Render = RNDR في معظم المنصات.
    if s == "RENDER/USDT":
        return "RNDR/USDT"
    return s

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
            # بعض واجهات OKX ترجع code != 0 عند الخطأ
            if str(j.get("code", "0")) not in ("0", "200"):
                time.sleep((2 ** a) + random.random()); continue
            return j
        except Exception:
            time.sleep((2 ** a) + random.random())
    return None

# سنقرأ بعض المفاتيح هنا (لا تعتمد على دوال مساعدة)
MIN_USD_VOL_24H = float(os.getenv("MIN_USD_VOL_24H", "0"))
SKIP_STABLES    = bool(int(os.getenv("SKIP_STABLES", "1")))
_STABLES = {"USDC/USDT","DAI/USDT","TUSD/USDT","USDD/USDT","USDP/USDT"}

def _fetch_okx_usdt_spot_ranked() -> List[str]:
    """قائمة أزواج SPOT/USDT مرتبة تقريبياً حسب السيولة (vol*last أو volCcy24h)."""
    j = _okx_get_json(TICKERS_URL, attempts=3)
    if not j:
        return []
    rows = []
    for it in j.get("data", []):
        inst = str(it.get("instId", "")).upper()  # مثل BTC-USDT
        if not inst.endswith("-USDT"):
            continue
        # تقدير حجم/سيولة لعمل ترتيب تقريبي
        vol = 0.0
        for key in ("volCcy24h", "volUsd", "vol24h"):
            v = it.get(key)
            if v:
                try:
                    vol = float(v); break
                except:  # noqa
                    pass
        if vol == 0.0:
            try:
                vol = float(it.get("vol24h", 0)) * float(it.get("last", 0))
            except:  # noqa
                vol = 0.0
        if MIN_USD_VOL_24H and vol < MIN_USD_VOL_24H:
            continue
        sym = inst.replace("-", "/")
        rows.append((sym, vol))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows]

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

# نقرأ القوائم السوداء/البيضاء بعد توفر _normalize_symbol
_SYMBOLS_BLACKLIST = { _normalize_symbol(s) for s in os.getenv("SYMBOLS_BLACKLIST", "").split(",") if s.strip() }
_SYMBOLS_WHITELIST = [ _normalize_symbol(s) for s in os.getenv("SYMBOLS_WHITELIST", "").split(",") if s.strip() ]

def _expand_symbols_okx(existing_symbols: Iterable[str], target: int = 100) -> List[str]:
    """يحافظ على الموجود (إن كان مدعومًا على OKX) ثم يُكمّل تلقائيًا حتى يصل للهدف."""
    # طبّع + تصحيح أسماء بسيطة + أزل التكرارات
    base = [_alias_symbol(_normalize_symbol(s)) for s in existing_symbols]
    base = _dedupe_keep_order(base)

    # قوائم بيضاء/سوداء
    if _SYMBOLS_BLACKLIST:
        base = [s for s in base if s not in _SYMBOLS_BLACKLIST]
    # إدراج whitelist في المقدّمة (إن لم تكن موجودة)
    for w in reversed(_SYMBOLS_WHITELIST):  # reverse حتى يبقى ترتيبها في النهاية كما كُتب
        if w not in base:
            base.insert(0, w)

    if SKIP_STABLES:
        base = [s for s in base if s not in _STABLES]

    okx_ranked = _fetch_okx_usdt_spot_ranked()
    if not okx_ranked:
        if USE_SYMBOLS_CACHE:
            cached = _read_cache()
            if cached:
                kept = [s for s in base if s in cached]
                extras = [s for s in cached if s not in kept]
                out = (kept + extras)[:target]
                if DEBUG_SYMBOLS:
                    print(f"[symbols] used cache fallback → {len(out)}")
                return out
        # لا إنترنت ولا كاش: ارجع قائمتك بعد تنظيفها
        out = base[:target]
        if DEBUG_SYMBOLS:
            print(f"[symbols] no OKX/no cache → using base ({len(out)})")
        return out

    # طبّق أساليب التصفية على قائمة OKX
    if _SYMBOLS_BLACKLIST:
        okx_ranked = [s for s in okx_ranked if s not in _SYMBOLS_BLACKLIST]
    if SKIP_STABLES:
        okx_ranked = [s for s in okx_ranked if s not in _STABLES]

    okx_set = set(okx_ranked)
    kept = [s for s in base if s in okx_set]           # احتفظ بما لديك إن كان مدعومًا
    extras = [s for s in okx_ranked if s not in kept]  # ثم أكمل من الأعلى سيولة
    out = (kept + extras)[:target]

    if USE_SYMBOLS_CACHE:
        _write_cache(out)

    if DEBUG_SYMBOLS:
        missing = [s for s in base if s not in okx_set]
        print(f"[symbols] kept={len(kept)}, added={len(out)-len(kept)}, missing_from_okx={missing[:10]}")
    return out

# ===== تنفيذ التوسيع عند الاستيراد =====
if AUTO_EXPAND_SYMBOLS:
    try:
        SYMBOLS = _expand_symbols_okx(SYMBOLS, target=TARGET_SYMBOLS_COUNT)
    except Exception:
        # أي خطأ غير متوقع: نظّف التكرارات واقصّ للهدف
        SYMBOLS = _dedupe_keep_order(SYMBOLS)[:TARGET_SYMBOLS_COUNT]
else:
    # حتى بدون جلب، نظّف التكرارات وقصّ للهدف
    SYMBOLS = _dedupe_keep_order(SYMBOLS)[:TARGET_SYMBOLS_COUNT]

if DEBUG_SYMBOLS:
    print(f"[symbols] ready: {len(SYMBOLS)} pairs | first 10: {SYMBOLS[:10]}")

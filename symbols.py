# symbols.py — أزواج التداول + توسّع تلقائي من OKX (SPOT / SWAP_USDT / BOTH)
# بيئة (اختياري):
#   INST_TYPE=SPOT|SWAP_USDT|BOTH     نوع السوق المراد توسعته (افتراضي SPOT)
#   TARGET_SYMBOLS_COUNT=100          الهدف النهائي للعدد
#   MIN_24H_USD_VOL=0                 حد سيولة دنيا بالدولار (0 = تعطيل)
#   AUTO_EXPAND_SYMBOLS=1             تفعيل التوسعة (1 افتراضي)
#   USE_SYMBOLS_CACHE=1               تفعيل الكاش (1 افتراضي)
#   SYMBOLS_CACHE_PATH=...            مسار الكاش (افتراضي symbols_cache.json)
#   OKX_BASE=https://www.okx.com      أساس واجهة OKX
#   OKX_TIMEOUT_SEC=12                مهلة الطلبات
#   DEBUG_SYMBOLS=0                   لوج تفصيلي
#   EXCLUDE_STABLES=1                 استبعاد عملات مستقرة كأساس
#   EXCLUDE_MEME=0                    استبعاد مجموعة Meme
#   INCLUDE_SYMBOLS="BTC/USDT,..."    تضمين قسري
#   EXCLUDE_SYMBOLS="ABC/USDT,..."    استبعاد قسري

import os, time, random, json
from typing import Iterable, List, Tuple, Dict, Optional

try:
    import requests
except Exception:
    requests = None

# ===== قائمتك الأساسية (منظّفة لمنع التكرار) =====
BASE_SYMBOLS = [
  # DeFi
  "AAVE/USDT","UNI/USDT","SUSHI/USDT","COMP/USDT","MKR/USDT",
  "SNX/USDT","CRV/USDT","LDO/USDT","GRT/USDT","LINK/USDT",
  # Layer 1 / Infra
  "ETH/USDT","SOL/USDT","ADA/USDT","AVAX/USDT","NEAR/USDT",
  "ALGO/USDT","ATOM/USDT","DOT/USDT","BNB/USDT","FET/USDT",
  # Gaming/Metaverse
  "MANA/USDT","AXS/USDT","SAND/USDT","CHZ/USDT","ENJ/USDT",
  "GALA/USDT","APE/USDT","ILV/USDT",
  # Layer 2 / Misc
  "OP/USDT","IMX/USDT","ZIL/USDT","ZRX/USDT","SKL/USDT",
  # Meme
  "PEPE/USDT","DOGE/USDT","SHIB/USDT","PUMP/USDT","MEMEFI/USDT",
  # Oracles/Infra/AI/Web3/Others
  "BAND/USDT","API3/USDT","RSR/USDT","UMA/USDT","KNC/USDT","BICO/USDT",
  "RNDR/USDT","VRA/USDT","GLMR/USDT","T/USDT","PSTAKE/USDT","BADGER/USDT",
  "PHA/USDT","NC/USDT","BOME/USDT",
  # OKX meme list (عينات)
  "PENGU/USDT","BONK/USDT","TRUMP/USDT","FLOKI/USDT","POLYDOGE/USDT",
  "WIF/USDT","TURBO/USDT","NOT/USDT","ORDI/USDT","DEGEN/USDT","MEME/USDT",
  "DOGS/USDT","VINE/USDT","CAT/USDT","ELON/USDT",
]

# ===== بيئة =====
AUTO_EXPAND_SYMBOLS = bool(int(os.getenv("AUTO_EXPAND_SYMBOLS", "1")))
TARGET_SYMBOLS_COUNT = int(os.getenv("TARGET_SYMBOLS_COUNT", "100"))
USE_SYMBOLS_CACHE    = bool(int(os.getenv("USE_SYMBOLS_CACHE", "1")))
DEBUG_SYMBOLS        = bool(int(os.getenv("DEBUG_SYMBOLS", "0")))
INST_TYPE            = os.getenv("INST_TYPE", "SPOT").strip().upper()  # SPOT | SWAP_USDT | BOTH

OKX_BASE    = os.getenv("OKX_BASE", "https://www.okx.com").rstrip("/")
TIMEOUT_SEC = int(os.getenv("OKX_TIMEOUT_SEC", "12"))
CACHE_PATH  = os.getenv("SYMBOLS_CACHE_PATH", os.path.join(os.path.dirname(__file__), "symbols_cache.json"))

MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "0"))
EXCLUDE_STABLES = bool(int(os.getenv("EXCLUDE_STABLES", "1")))
EXCLUDE_MEME    = bool(int(os.getenv("EXCLUDE_MEME", "0")))

def _parse_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw: return []
    return [s.strip().upper().replace("-", "/").replace("_", "/")
            for s in raw.split(",") if s.strip()]

INCLUDE_SYMBOLS = _parse_csv_env("INCLUDE_SYMBOLS")
EXCLUDE_SYMBOLS = set(_parse_csv_env("EXCLUDE_SYMBOLS"))

_STABLE_BASES = {"USDC","DAI","TUSD","USDD","FDUSD","USDE","USDT"}
_MEME_BASES = {"PEPE","DOGE","SHIB","ELON","FLOKI","WIF","BONK","PENGU",
               "TURBO","NOT","TRUMP","DEGEN","MEME","DOGS","VINE","CAT"}
_LEVERAGED_SUFFIXES = {"3L","3S","5L","5S","UP","DOWN"}

# ===== أدوات =====
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
    # تصحيح شائع: RENDER → RNDR
    return "RNDR/USDT" if s == "RENDER/USDT" else s

def _is_leveraged(sym: str) -> bool:
    base = sym.split("/", 1)[0]
    return any(base.endswith(suf) for suf in _LEVERAGED_SUFFIXES)

def _okx_get_json(url: str, attempts: int = 3) -> Optional[Dict]:
    if requests is None:
        return None
    headers = {
        "User-Agent": "mk1_symbols/1.0 (+https://okx.com)",
        "Accept": "application/json",
    }
    for a in range(attempts):
        try:
            r = requests.get(url, timeout=TIMEOUT_SEC, headers=headers)
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

def _okx_tickers(inst_type: str) -> List[Dict]:
    url = f"{OKX_BASE}/api/v5/market/tickers?instType={inst_type}"
    j = _okx_get_json(url, attempts=3)
    return j.get("data", []) if j else []

def _usd_liquidity_approx(it: Dict) -> float:
    # ترتيب السيولة بالدولار: volUsd > volCcy24h*last > vol24h*last
    last = float(it.get("last", 0.0) or 0.0)
    for key in ("volUsd", "volCcy24h", "vol24h"):
        v = it.get(key)
        if not v: continue
        try:
            v = float(v)
            if key == "volUsd":
                return v
            return v * last
        except Exception:
            continue
    return 0.0

def _filter_symbol(sym: str) -> bool:
    base = sym.split("/", 1)[0]
    if _is_leveraged(sym): return False
    if EXCLUDE_STABLES and base in _STABLE_BASES: return False
    if EXCLUDE_MEME and base in _MEME_BASES: return False
    if sym in EXCLUDE_SYMBOLS: return False
    return True

def _rank_spot_usdt() -> List[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    for it in _okx_tickers("SPOT"):
        inst = str(it.get("instId", "")).upper()  # BTC-USDT
        if not inst.endswith("-USDT"): 
            continue
        sym = inst.replace("-", "/")              # BTC/USDT
        rows.append((sym, _usd_liquidity_approx(it)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows

def _rank_swap_usdt() -> List[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    for it in _okx_tickers("SWAP"):
        inst = str(it.get("instId", "")).upper()  # BTC-USDT-SWAP
        # نأخذ فقط العقود الخطية USDT (نتجاهل USD coin-margined)
        if not inst.endswith("-USDT-SWAP"):
            continue
        # نوحد العرض إلى BTC/USDT (للتوافق مع بقية المشروع)
        base = inst.split("-USDT-SWAP", 1)[0]
        sym = f"{base}/USDT"
        rows.append((sym, _usd_liquidity_approx(it)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows

def _read_cache(key: str) -> List[str]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        arr = data.get(key, []) if isinstance(data, dict) else data
        return [_normalize_symbol(x) for x in arr if isinstance(x, str)]
    except Exception:
        return []

def _write_cache(key: str, symbols: List[str]) -> None:
    try:
        data = {}
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                try: data = json.load(f)
                except Exception: data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = symbols
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _expand_for_mode(existing: Iterable[str], ranked: List[Tuple[str, float]], target: int) -> List[str]:
    base = [_alias_symbol(_normalize_symbol(s)) for s in existing]
    base = _dedupe_keep_order(base)
    for add in INCLUDE_SYMBOLS:
        if add not in base:
            base.append(add)

    okx_syms = [s for s, _ in ranked]
    okx_set = set(okx_syms)

    keep = [s for s in base if (s in okx_set) and _filter_symbol(s)]
    extras: List[str] = []
    for sym, vol in ranked:
        if sym in keep: continue
        if not _filter_symbol(sym): continue
        if MIN_24H_USD_VOL > 0 and vol < MIN_24H_USD_VOL: continue
        extras.append(sym)

    out = (keep + extras)[:target]
    return out

def _expand_symbols_okx(existing_symbols: Iterable[str], target: int = 100) -> List[str]:
    # اختيار الرانك حسب INST_TYPE
    ranked_spot = _rank_spot_usdt() if INST_TYPE in ("SPOT","BOTH") else []
    ranked_swap = _rank_swap_usdt() if INST_TYPE in ("SWAP_USDT","BOTH") else []

    if not ranked_spot and not ranked_swap:
        # فشل الشبكة → كاش بحسب المفتاح
        if USE_SYMBOLS_CACHE:
            key = f"cache_{INST_TYPE}"
            cached = _read_cache(key)
            if cached:
                base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in existing_symbols])
                keep = [s for s in base if s in cached and _filter_symbol(s)]
                extras = [s for s in cached if s not in keep and _filter_symbol(s)]
                out = (keep + extras)[:target]
                if DEBUG_SYMBOLS: print(f"[symbols] cache fallback({INST_TYPE}) → {len(out)}")
                return out
        # لا إنترنت ولا كاش
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in existing_symbols])
        out = [s for s in base if _filter_symbol(s)][:target]
        if DEBUG_SYMBOLS: print(f"[symbols] no OKX/no cache → using base ({len(out)})")
        return out

    out_parts: List[str] = []
    if ranked_spot:
        part = _expand_for_mode(existing_symbols, ranked_spot, target)
        out_parts.extend(part)
    if ranked_swap:
        # عند BOTH نكمل للهدف بتجميعة (Spot أولاً ثم Swap)
        need = max(0, target - len(out_parts))
        part = _expand_for_mode(existing_symbols, ranked_swap, need if INST_TYPE=="BOTH" else target)
        out_parts.extend([s for s in part if s not in out_parts])

    out = out_parts[:target]

    if USE_SYMBOLS_CACHE:
        _write_cache(f"cache_{INST_TYPE}", out)

    if DEBUG_SYMBOLS:
        msg = f"[symbols] {INST_TYPE}: total={len(out)} target={target} min_vol={MIN_24H_USD_VOL}"
        print(msg)
    return out

def _prepare_symbols() -> List[str]:
    if not AUTO_EXPAND_SYMBOLS:
        base = [_alias_symbol(_normalize_symbol(s)) for s in BASE_SYMBOLS]
        base = _dedupe_keep_order(base)
        for add in INCLUDE_SYMBOLS:
            if add not in base: base.append(add)
        out = [s for s in base if _filter_symbol(s) and s not in EXCLUDE_SYMBOLS][:TARGET_SYMBOLS_COUNT]
        if DEBUG_SYMBOLS: print(f"[symbols] static only: {len(out)} pairs")
        return out
    try:
        return _expand_symbols_okx(BASE_SYMBOLS, target=TARGET_SYMBOLS_COUNT)
    except Exception:
        fallback = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in BASE_SYMBOLS])
        out = [s for s in fallback if _filter_symbol(s)][:TARGET_SYMBOLS_COUNT]
        return out

SYMBOLS: List[str] = _prepare_symbols()

if DEBUG_SYMBOLS:
    sample = ", ".join(SYMBOLS[:10])
    print(f"[symbols] ready({INST_TYPE}): {len(SYMBOLS)} | first 10: {sample}")

__all__ = ["SYMBOLS"]

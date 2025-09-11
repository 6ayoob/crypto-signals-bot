# -*- coding: utf-8 -*-
"""
symbols.py — توسيع ذكي لقائمة الأزواج من OKX (SPOT / SWAP_USDT / BOTH)
- يعرّف: SYMBOLS (قائمة)، SYMBOLS_META (قاموس معلومات إضافية)
- يدعم: كاش JSON، فلاتر (مين/إكسترا/إستبعاد)، و CLI للطباعة/التصدير.

ENV (اختياري):
  APP_DATA_DIR=/tmp/market-watchdog
  INST_TYPE=SPOT|SWAP_USDT|BOTH       (افتراضي SPOT)
  TARGET_SYMBOLS_COUNT=100
  MIN_24H_USD_VOL=0                   (فلتر سيولة تقديرية)
  AUTO_EXPAND_SYMBOLS=1               (إن 0: يعتمد فقط على INCLUDE_SYMBOLS)
  USE_SYMBOLS_CACHE=1                 (تفعيل الكاش)
  DEBUG_SYMBOLS=0
  OKX_BASE=https://www.okx.com
  OKX_TIMEOUT_SEC=12
  SYMBOLS_CACHE_PATH=<path/to/json>   (افتراضي داخل APP_DATA_DIR)
  INCLUDE_SYMBOLS=BTC/USDT,ETH/USDT   (قائمة مفصولة بفواصل)
  EXCLUDE_SYMBOLS=XYZ/USDT            (قائمة مفصولة بفواصل)
  EXCLUDE_STABLES=1
  EXCLUDE_MEME=0
"""

from __future__ import annotations
import os, time, random, json, argparse
from typing import Iterable, List, Tuple, Dict, Optional
from pathlib import Path

# ===== إعداد المسارات القابلة للكتابة =====
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ===== بيئة =====
AUTO_EXPAND_SYMBOLS = bool(int(os.getenv("AUTO_EXPAND_SYMBOLS", "1")))
TARGET_SYMBOLS_COUNT = int(os.getenv("TARGET_SYMBOLS_COUNT", "100"))
USE_SYMBOLS_CACHE    = bool(int(os.getenv("USE_SYMBOLS_CACHE", "1")))
DEBUG_SYMBOLS        = bool(int(os.getenv("DEBUG_SYMBOLS", "0")))
INST_TYPE            = os.getenv("INST_TYPE", "SPOT").strip().upper()  # SPOT | SWAP_USDT | BOTH

OKX_BASE    = os.getenv("OKX_BASE", "https://www.okx.com").rstrip("/")
TIMEOUT_SEC = int(os.getenv("OKX_TIMEOUT_SEC", "12"))

# كاش داخل APP_DATA_DIR افتراضيًا
CACHE_PATH  = os.getenv("SYMBOLS_CACHE_PATH", str(APP_DATA_DIR / "symbols_cache.json"))

MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "0"))
EXCLUDE_STABLES = bool(int(os.getenv("EXCLUDE_STABLES", "1")))
EXCLUDE_MEME    = bool(int(os.getenv("EXCLUDE_MEME", "0")))

try:
    import requests  # اختياري
except Exception:
    requests = None

# ===== مساعدات ENV =====
def _parse_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw: return []
    return [s.strip().upper().replace("-", "/").replace("_", "/")
            for s in raw.split(",") if s.strip()]

INCLUDE_SYMBOLS = _parse_csv_env("INCLUDE_SYMBOLS")
EXCLUDE_SYMBOLS = set(_parse_csv_env("EXCLUDE_SYMBOLS"))

_STABLE_BASES = {"USDC","DAI","TUSD","USDD","FDUSD","USDE","USDT"}
_MEME_BASES = {"PEPE","DOGE","SHIB","ELON","FLOKI","WIF","BONK","PENGU","TURBO","NOT","TRUMP","DEGEN","MEME","DOGS","VINE","CAT"}
_LEVERAGED_SUFFIXES = {"3L","3S","5L","5S","UP","DOWN"}

# ===== الأدوات الأساسية =====
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
    # توافق بعض التسميات الشائعة
    return "RNDR/USDT" if s == "RENDER/USDT" else s

def _is_leveraged(sym: str) -> bool:
    base = sym.split("/", 1)[0]
    return any(base.endswith(suf) for suf in _LEVERAGED_SUFFIXES)

def _okx_get_json(url: str, attempts: int = 3) -> Optional[Dict]:
    if requests is None:
        return None
    headers = {"User-Agent": "symbols/1.1 (+okx)", "Accept": "application/json"}
    for a in range(attempts):
        try:
            r = requests.get(url, timeout=TIMEOUT_SEC, headers=headers)
            if r.status_code == 429:
                time.sleep((2 ** a) + random.random()); continue
            r.raise_for_status()
            j = r.json()
            # OKX عادةً تُعيد code=0 عند النجاح
            if str(j.get("code","0")) not in ("0","200"):
                time.sleep((2 ** a) + random.random()); continue
            return j
        except Exception:
            time.sleep((2 ** a) + random.random())
    return None

def _usd_liquidity_approx(it: Dict) -> float:
    """
    يحاول تقدير حجم USD:
      - إذا تواجد volUsd: يُستخدم مباشرة
      - وإلّا: volCcy24h أو vol24h × last
    """
    last = 0.0
    try:
        last = float(it.get("last", 0.0) or 0.0)
    except Exception:
        last = 0.0
    for key in ("volUsd", "volCcy24h", "vol24h"):
        v = it.get(key)
        if v is None: continue
        try:
            vv = float(v)
            if key == "volUsd": return vv
            return vv * (last if last > 0 else 1.0)
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

# ===== جلب الرُتب من OKX =====
def _okx_tickers(inst_type: str) -> List[Dict]:
    url = f"{OKX_BASE}/api/v5/market/tickers?instType={inst_type}"
    j = _okx_get_json(url, attempts=3)
    return j.get("data", []) if j and isinstance(j.get("data"), list) else []

def get_ranked(inst: str) -> List[Tuple[str, float, str]]:
    """
    يعيد قائمة [(symbol, usdLiquidity, source)] حيث source ∈ {SPOT, SWAP}
    """
    inst = inst.upper().strip()
    rows: List[Tuple[str, float, str]] = []

    if inst in ("SPOT", "BOTH"):
        for it in _okx_tickers("SPOT"):
            instId = str(it.get("instId","")).upper()
            if not instId.endswith("-USDT"): continue
            sym = instId.replace("-", "/")
            rows.append((sym, _usd_liquidity_approx(it), "SPOT"))

    if inst in ("SWAP_USDT", "BOTH"):
        for it in _okx_tickers("SWAP"):
            instId = str(it.get("instId","")).upper()  # مثال: BTC-USDT-SWAP
            if not instId.endswith("-USDT-SWAP"): continue
            base = instId.split("-USDT-SWAP", 1)[0]
            sym = f"{base}/USDT"
            rows.append((sym, _usd_liquidity_approx(it), "SWAP"))

    # دمج فريد مع تفضيل الأعلى سيولة
    best: Dict[str, Tuple[str, float, str]] = {}
    for sym, vol, src in rows:
        s = _alias_symbol(_normalize_symbol(sym))
        if s not in best or vol > best[s][1]:
            best[s] = (s, vol, src)
    out = list(best.values())
    out.sort(key=lambda x: x[1], reverse=True)
    return out

# ===== كاش =====
def _read_cache(key: str) -> Dict[str, Dict]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(key, {}) if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_cache(key: str, symbols_meta: Dict[str, Dict]) -> None:
    if not USE_SYMBOLS_CACHE:
        return
    try:
        base: Dict[str, Dict] = {}
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                try:
                    base = json.load(f)
                except Exception:
                    base = {}
        if not isinstance(base, dict):
            base = {}
        base[key] = symbols_meta
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ===== المنتج النهائي =====
def list_symbols(inst: str, target: int, min_usd_vol: float) -> Tuple[List[str], Dict[str, Dict]]:
    """
    يعيد (symbols, meta) حيث:
      symbols: قائمة رموز مثل ["BTC/USDT", ...]
      meta: {symbol: {"volUsd": float|None, "source": "SPOT|SWAP|FORCED|STATIC"}}
    """
    inst = inst.upper().strip()
    ranked = get_ranked(inst)

    # لو فشل الجلب: استخدم الكاش إن وجد
    if not ranked and USE_SYMBOLS_CACHE:
        cached = _read_cache(f"meta_{inst}")
        if cached:
            syms = [(k, cached[k].get("volUsd", 0.0)) for k in cached.keys()]
            syms.sort(key=lambda x: x[1], reverse=True)
            symbols = [s for s, _ in syms]
            return symbols[:target], cached

    out_syms: List[str] = []
    meta: Dict[str, Dict] = {}
    seen = set()

    # أضف include أولاً
    for add in INCLUDE_SYMBOLS:
        a = _alias_symbol(_normalize_symbol(add))
        if a not in seen and _filter_symbol(a):
            out_syms.append(a)
            meta[a] = {"volUsd": None, "source": "FORCED"}
            seen.add(a)

    # من الرُتب OKX
    for sym, vol, src in ranked:
        if sym in seen: continue
        if not _filter_symbol(sym): continue
        if min_usd_vol > 0 and vol < min_usd_vol: continue
        out_syms.append(sym)
        meta[sym] = {"volUsd": float(vol), "source": src}
        seen.add(sym)
        if len(out_syms) >= target: break

    # استبعاد قسري بعد الإضافة
    out_syms = [s for s in out_syms if s not in set(EXCLUDE_SYMBOLS)]

    # كاش ميتا
    _write_cache(f"meta_{inst}", meta)

    return out_syms[:target], meta

def _prepare_symbols() -> Tuple[List[str], Dict[str, Dict]]:
    """
    المنفذ النهائي الذي يجهّز SYMBOLS/SYMBOLS_META
    """
    if not AUTO_EXPAND_SYMBOLS:
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        return base[:TARGET_SYMBOLS_COUNT], {s: {"volUsd": None, "source": "STATIC"} for s in base}
    try:
        return list_symbols(INST_TYPE, TARGET_SYMBOLS_COUNT, MIN_24H_USD_VOL)
    except Exception:
        # fallback: include فقط
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        return base[:TARGET_SYMBOLS_COUNT], {s: {"volUsd": None, "source": "STATIC"} for s in base}

# === نقاط التصدير ===
SYMBOLS, SYMBOLS_META = _prepare_symbols()
__all__ = ["SYMBOLS", "SYMBOLS_META", "list_symbols", "get_ranked"]

# ===== Debug =====
if DEBUG_SYMBOLS:
    sample = ", ".join(SYMBOLS[:10])
    print(f"[symbols] ready({INST_TYPE}): {len(SYMBOLS)} | first 10: {sample}")

# ===== CLI =====
def main():
    ap = argparse.ArgumentParser(description="OKX symbols helper (SPOT/SWAP/BOTH)")
    ap.add_argument("--print", dest="to_print", type=int, default=0, help="طباعة أول N رمز مع السيولة")
    ap.add_argument("--inst", default=INST_TYPE, choices=["SPOT","SWAP_USDT","BOTH"])
    ap.add_argument("--target", type=int, default=TARGET_SYMBOLS_COUNT)
    ap.add_argument("--minvol", type=float, default=MIN_24H_USD_VOL)
    ap.add_argument("--export", default="", help="اكتب meta إلى ملف JSON")
    args = ap.parse_args()

    syms, meta = list_symbols(args.inst, args.target, args.minvol)

    if args.to_print:
        print(f"[list] {args.inst} total={len(syms)} (minvol={args.minvol})")
        for i, s in enumerate(syms[:args.to_print], 1):
            m = meta.get(s, {})
            vol = m.get("volUsd", "-")
            src = m.get("source", "-")
            print(f"{i:>3}. {s:<15} volUsd≈{vol}  src={src}")

    if args.export:
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            print(f"[export] wrote meta → {args.export}")
        except Exception as e:
            print(f"[export][err] {e}")

if __name__ == "__main__":
    main()

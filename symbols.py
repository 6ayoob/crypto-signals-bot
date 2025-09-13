# -*- coding: utf-8 -*-
"""
 symbols.py — توليد ذكي لقائمة الأزواج من OKX (SPOT / SWAP_USDT / BOTH)
 - يعرّف: SYMBOLS (قائمة)، SYMBOLS_META (قاموس معلومات إضافية)
 - يدعم: كاش JSON، فلاتر، و CLI للطباعة/التصدير.
 - v1.4.2: Fallback إلى urllib إذا لم تتوفر requests.

 مهم (توافق خلفي):
 - list_symbols(inst, target, min_usd_vol) تُرجع **قائمة** فقط افتراضيًا.
 - إن أردت الميتا: list_symbols(..., return_meta=True) → (list, meta)

 ENV (اختياري):
   APP_DATA_DIR=/tmp/market-watchdog
   INST_TYPE=SPOT|SWAP_USDT|BOTH
   TARGET_SYMBOLS_COUNT=100
   MIN_24H_USD_VOL=0
   AUTO_EXPAND_SYMBOLS=1
   USE_SYMBOLS_CACHE=1
   DEBUG_SYMBOLS=0
   OKX_BASE=https://www.okx.com
   OKX_TIMEOUT_SEC=12
   SYMBOLS_CACHE_PATH=<path/to/json>
   INCLUDE_SYMBOLS=BTC/USDT,ETH/USDT
   EXCLUDE_SYMBOLS=XYZ/USDT
   EXCLUDE_STABLES=1
   EXCLUDE_MEME=0
   EXCLUDE_LEVERAGED_SUFFIXES=3L,3S,5L,5S,UP,DOWN
"""

from __future__ import annotations
import os, time, random, json, argparse
from typing import Iterable, List, Tuple, Dict, Optional, Union
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

# تخصيص لواحق الأزواج ذات الرافعة عبر ENV
_DEF_LEV = {"3L","3S","5L","5S","UP","DOWN"}
try:
    env_raw = os.getenv("EXCLUDE_LEVERAGED_SUFFIXES", ",".join(sorted(_DEF_LEV)))
    _ENV_LEV = {x.strip().upper() for x in env_raw.split(",") if x.strip()}
    LEVERAGED_SUFFIXES = _ENV_LEV or _DEF_LEV
except Exception:
    LEVERAGED_SUFFIXES = _DEF_LEV

# ===== HTTP: requests (اختياري) + urllib fallback =====
try:
    import requests  # اختياري
except Exception:  # pragma: no cover
    requests = None

import urllib.request

# ===== مساعدات ENV =====

def _normalize_symbol(s: str) -> str:
    return str(s).strip().upper().replace("-", "/").replace("_", "/")

def _parse_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [_normalize_symbol(s) for s in raw.split(",") if s.strip()]

INCLUDE_SYMBOLS = _parse_csv_env("INCLUDE_SYMBOLS")
EXCLUDE_SYMBOLS = set(_parse_csv_env("EXCLUDE_SYMBOLS"))

_STABLE_BASES = {"USDC","DAI","TUSD","USDD","FDUSD","USDE","USDT"}
_MEME_BASES = {
    "PEPE","DOGE","SHIB","ELON","FLOKI","WIF","BONK","PENGU","TURBO","NOT","TRUMP","DEGEN","MEME","DOGS","VINE","CAT"
}

# Aliases قابلة للتوسعة
_ALIASES: Dict[str, str] = {
    "RENDER/USDT": "RNDR/USDT",
}

# ===== الأدوات الأساسية =====

def _dedupe_keep_order(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        x = _normalize_symbol(x)
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _alias_symbol(s: str) -> str:
    return _ALIASES.get(s, s)

def _is_leveraged(sym: str) -> bool:
    base = sym.split("/", 1)[0]
    return any(base.endswith(suf) for suf in LEVERAGED_SUFFIXES)

# ===== HTTP helper =====

def _okx_get_json(url: str, attempts: int = 3) -> Optional[Dict]:
    headers = {"User-Agent": "symbols/1.4.2 (+okx)", "Accept": "application/json"}

    def _urllib_fetch(u: str):
        req = urllib.request.Request(u, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return json.loads(r.read().decode("utf-8"))

    for a in range(attempts):
        try:
            if requests is not None:
                r = requests.get(url, timeout=TIMEOUT_SEC, headers=headers)
                if r.status_code in (429, 524, 520, 502, 503):
                    time.sleep((2 ** a) + random.random()); continue
                r.raise_for_status()
                j = r.json()
            else:
                j = _urllib_fetch(url)
            if j and str(j.get("code","0")) in ("0","200"):
                return j
        except Exception:
            pass
        time.sleep((2 ** a) + random.random())
    return None

# ===== تقدير السيولة بالدولار =====

def _usd_liquidity_approx(it: Dict) -> float:
    try:
        last = float(it.get("last", 0.0) or 0.0)
    except Exception:
        last = 0.0
    for key in ("volUsd", "volCcy24h", "vol24h"):
        v = it.get(key)
        if v is None:
            continue
        try:
            vv = float(v)
            if key == "volUsd":
                return vv
            return vv * (last if last > 0 else 1.0)
        except Exception:
            continue
    return 0.0

# ===== فلاتر =====

def _filter_symbol(sym: str) -> bool:
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

# ===== جلب الرُتب من OKX =====

def _okx_tickers(inst_type: str) -> List[Dict]:
    url = f"{OKX_BASE}/api/v5/market/tickers?instType={inst_type}"
    j = _okx_get_json(url, attempts=3)
    return j.get("data", []) if j and isinstance(j.get("data"), list) else []

def get_ranked(inst: str) -> List[Tuple[str, float, str]]:
    """يعيد [(symbol, usdLiquidity, source)] حيث source ∈ {SPOT, SWAP}.
    دائمًا نُرجع الرمز كـ "BASE/USDT" بغض النظر عن المصدر.
    """
    inst = inst.upper().strip()
    rows: List[Tuple[str, float, str]] = []

    if inst in ("SPOT", "BOTH"):
        for it in _okx_tickers("SPOT"):
            instId = str(it.get("instId", "")).upper()
            if not instId.endswith("-USDT"):
                continue
            sym = instId.replace("-", "/")
            rows.append((sym, _usd_liquidity_approx(it), "SPOT"))

    if inst in ("SWAP_USDT", "BOTH"):
        for it in _okx_tickers("SWAP"):
            instId = str(it.get("instId", "")).upper()  # مثل: BTC-USDT-SWAP
            if not instId.endswith("-USDT-SWAP"):
                continue
            base = instId.split("-USDT-SWAP", 1)[0]
            sym = f"{base}/USDT"
            rows.append((sym, _usd_liquidity_approx(it), "SWAP"))

    # دمج وتفضيل الأعلى سيولة
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
        tmp = Path(str(CACHE_PATH) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
        Path(tmp).replace(CACHE_PATH)
    except Exception:
        pass

# ===== المنتج النهائي =====

def list_symbols(inst: str, target: int, min_usd_vol: float, *, return_meta: bool = False
                 ) -> Union[List[str], Tuple[List[str], Dict[str, Dict]]]:
    inst = inst.upper().strip()
    ranked = get_ranked(inst)

    # لو فشل الجلب: استخدم الكاش إن وجد
    if not ranked and USE_SYMBOLS_CACHE:
        cached = _read_cache(f"meta_{inst}")
        if cached:
            syms = [(k, cached[k].get("volUsd", 0.0)) for k in cached.keys()]
            syms.sort(key=lambda x: x[1], reverse=True)
            symbols = [s for s, _ in syms][:target]
            if return_meta:
                return symbols, cached
            return symbols

    out_syms: List[str] = []
    meta: Dict[str, Dict] = {}
    seen = set()

    # include أولًا
    for add in INCLUDE_SYMBOLS:
        a = _alias_symbol(_normalize_symbol(add))
        if a not in seen and _filter_symbol(a):
            out_syms.append(a)
            meta[a] = {"volUsd": None, "source": "FORCED"}
            seen.add(a)

    # من الرُتب OKX
    for sym, vol, src in ranked:
        if sym in seen:
            continue
        if not _filter_symbol(sym):
            continue
        if min_usd_vol > 0 and vol < min_usd_vol:
            continue
        out_syms.append(sym)
        meta[sym] = {"volUsd": float(vol), "source": src}
        seen.add(sym)
        if len(out_syms) >= target:
            break

    # استبعاد قسري بعد الإضافة
    out_syms = [s for s in out_syms if s not in set(EXCLUDE_SYMBOLS)]

    # كاش ميتا
    _write_cache(f"meta_{inst}", meta)

    out_syms = out_syms[:target]
    if return_meta:
        return out_syms, meta
    return out_syms

# ===== تحويل أسماء إلى صيغة ccxt (اختياري) =====

def to_ccxt_names(symbols: List[str] | Tuple[List[str], Dict[str, Dict]],
                  meta: Optional[Dict[str, Dict]] = None,
                  prefer_swap: bool = True) -> List[str]:
    if isinstance(symbols, tuple) and meta is None:
        symbols, meta = symbols  # type: ignore
    meta = meta or {}
    out: List[str] = []
    for s in symbols:
        src = (meta.get(s, {}) or {}).get("source", "SPOT")
        if prefer_swap and src == "SWAP":
            base = s.split("/", 1)[0]
            out.append(f"{base}/USDT:USDT")
        else:
            out.append(s)
    return out

# ===== منفذ نهائي لتهيئة الرموز =====

def _prepare_symbols() -> Tuple[List[str], Dict[str, Dict]]:
    if not AUTO_EXPAND_SYMBOLS:
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        return base[:TARGET_SYMBOLS_COUNT], {s: {"volUsd": None, "source": "STATIC"} for s in base}
    try:
        return list_symbols(INST_TYPE, TARGET_SYMBOLS_COUNT, MIN_24H_USD_VOL, return_meta=True)
    except Exception:
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        return base[:TARGET_SYMBOLS_COUNT], {s: {"volUsd": None, "source": "STATIC"} for s in base}

# === نقاط التصدير ===
SYMBOLS, SYMBOLS_META = _prepare_symbols()
__all__ = ["SYMBOLS", "SYMBOLS_META", "list_symbols", "get_ranked", "to_ccxt_names"]

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
    ap.add_argument("--with-meta", action="store_true", help="أعد الميتا مع القوائم في CLI")
    ap.add_argument("--ccxt", action="store_true", help="حوّل الأسماء لشكل ccxt (SWAP → :USDT)")
    ap.add_argument("--export-ccxt", default="", help="اكتب قائمة الرموز بصيغة ccxt إلى ملف نصي")
    args = ap.parse_args()

    if args.with_meta:
        syms, meta = list_symbols(args.inst, args.target, args.minvol, return_meta=True)
    else:
        syms = list_symbols(args.inst, args.target, args.minvol)
        meta = {}

    ccxt_syms: Optional[List[str]] = None
    if args.ccxt:
        ccxt_syms = to_ccxt_names((syms, meta) if meta else syms, meta or None, prefer_swap=True)

    if args.to_print:
        print(f"[list] {args.inst} total={len(syms)} (minvol={args.minvol})")
        view_list = ccxt_syms if ccxt_syms is not None else syms
        for i, s in enumerate(view_list[:args.to_print], 1):
            m = meta.get(s, meta.get(s.replace(":USDT","/USDT"), {})) if meta else {}
            vol = m.get("volUsd", "-")
            src = m.get("source", "-")
            print(f"{i:>3}. {s:<18} volUsd≈{vol}  src={src}")

    if args.export:
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(meta or {}, f, ensure_ascii=False, indent=2)
            print(f"[export] wrote meta → {args.export}")
        except Exception as e:
            print(f"[export][err] {e}")

    if args.export_ccxt:
        try:
            out_list = ccxt_syms if ccxt_syms is not None else to_ccxt_names((syms, meta) if meta else syms, meta or None)
            with open(args.export_ccxt, "w", encoding="utf-8") as f:
                for s in out_list:
                    f.write(s + "\n")
            print(f"[export-ccxt] wrote {len(out_list)} symbols → {args.export_ccxt}")
        except Exception as e:
            print(f"[export-ccxt][err] {e}")

if __name__ == "__main__":
    main()

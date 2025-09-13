# -*- coding: utf-8 -*-
"""
symbols.py — توسيع ذكي + آمن لقائمة الأزواج من OKX (SPOT / SWAP_USDT / BOTH)
- يعرّف: SYMBOLS (قائمة)، SYMBOLS_META (قاموس معلومات إضافية)
- يدعم: كاش JSON (كتابة ذرّية/آمنة)، فلاتر، و CLI للطباعة/التصدير.
- يمنع تمامًا سيناريو "0 symbols" عبر fallback إلى الكاش السابق أو baseline.

مهم (توافق خلفي):
- list_symbols(inst, target, min_usd_vol) تُرجِع **قائمة** افتراضيًا.
- إن أردت الميتا أيضًا: list_symbols(..., return_meta=True) → (list, meta)

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
  SYMBOLS_CACHE_PATH=<path/to/json>   (افتراضي داخل APP_DATA_DIR)
  INCLUDE_SYMBOLS=BTC/USDT,ETH/USDT
  EXCLUDE_SYMBOLS=XYZ/USDT
  EXCLUDE_STABLES=1
  EXCLUDE_MEME=0
  BASELINE_SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,TON/USDT,TRX/USDT,DOT/USDT
"""

from __future__ import annotations
import os, time, random, json, argparse, tempfile, shutil
from typing import Iterable, List, Tuple, Dict, Optional, Union
from pathlib import Path

# ===== إعداد المسارات القابلة للكتابة =====
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ===== بيئة =====
def _as_bool(v: str | None, default: bool = True) -> bool:
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

AUTO_EXPAND_SYMBOLS = _as_bool(os.getenv("AUTO_EXPAND_SYMBOLS", "1"))
TARGET_SYMBOLS_COUNT = int(os.getenv("TARGET_SYMBOLS_COUNT", "100"))
USE_SYMBOLS_CACHE    = _as_bool(os.getenv("USE_SYMBOLS_CACHE", "1"))
DEBUG_SYMBOLS        = _as_bool(os.getenv("DEBUG_SYMBOLS", "0"))
INST_TYPE            = os.getenv("INST_TYPE", "SPOT").strip().upper()  # SPOT | SWAP_USDT | BOTH

OKX_BASE    = os.getenv("OKX_BASE", "https://www.okx.com").rstrip("/")
TIMEOUT_SEC = int(os.getenv("OKX_TIMEOUT_SEC", "12"))

# كاش داخل APP_DATA_DIR افتراضيًا
CACHE_PATH  = os.getenv("SYMBOLS_CACHE_PATH", str(APP_DATA_DIR / "symbols_cache.json"))

MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "0"))
EXCLUDE_STABLES = _as_bool(os.getenv("EXCLUDE_STABLES", "1"))
EXCLUDE_MEME    = _as_bool(os.getenv("EXCLUDE_MEME", "0"))

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
BASELINE_SYMBOLS_ENV = _parse_csv_env("BASELINE_SYMBOLS")

_STABLE_BASES = {"USDC","DAI","TUSD","USDD","FDUSD","USDE","USDT"}
_MEME_BASES = {"PEPE","DOGE","SHIB","ELON","FLOKI","WIF","BONK","PENGU","TURBO","NOT","TRUMP","DEGEN","MEME","DOGS","VINE","CAT"}
_LEVERAGED_SUFFIXES = {"3L","3S","5L","5S","UP","DOWN"}

# ===== Debug =====
def _dbg(msg: str):
    if DEBUG_SYMBOLS:
        print(f"[symbols-debug] {msg}", flush=True)

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

# ===== OKX HTTP helpers =====

def _okx_get_json(url: str, attempts: int = 3) -> Optional[Dict]:
    if requests is None:
        _dbg("requests module not available; skipping HTTP fetch → will use cache/baseline")
        return None
    headers = {"User-Agent": "symbols/1.3 (+okx)", "Accept": "application/json"}
    for a in range(attempts):
        try:
            r = requests.get(url, timeout=TIMEOUT_SEC, headers=headers)
            if r.status_code == 429:
                # backoff تصاعدي بسيط
                time.sleep((2 ** a) + random.random()); continue
            r.raise_for_status()
            j = r.json()
            if str(j.get("code", "0")) not in ("0", "200"):
                time.sleep((2 ** a) + random.random()); continue
            return j
        except Exception as e:
            _dbg(f"http error: {e}")
            time.sleep((2 ** a) + random.random())
    return None

# تقدير سيولة بالدولار

def _usd_liquidity_approx(it: Dict) -> float:
    """
    يحاول تقدير حجم USD:
      - إذا تواجد volUsd: يُستخدم مباشرة
      - وإلّا: volCcy24h أو vol24h × last
    """
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

# فلاتر

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
    data = j.get("data", []) if j and isinstance(j.get("data"), list) else []
    _dbg(f"tickers {inst_type}: {len(data)}")
    return data


def get_ranked(inst: str) -> List[Tuple[str, float, str]]:
    """
    يعيد قائمة [(symbol, usdLiquidity, source)] حيث source ∈ {SPOT, SWAP}
    """
    inst = inst.upper().strip()
    rows: List[Tuple[str, float, str]] = []

    if inst in ("SPOT", "BOTH"):
        for it in _okx_tickers("SPOT"):
            instId = str(it.get("instId", "")).upper()  # مثال: BTC-USDT
            if not instId.endswith("-USDT"): continue
            sym = instId.replace("-", "/")
            rows.append((sym, _usd_liquidity_approx(it), "SPOT"))

    if inst in ("SWAP_USDT", "BOTH"):
        for it in _okx_tickers("SWAP"):
            instId = str(it.get("instId", "")).upper()  # مثال: BTC-USDT-SWAP
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
    _dbg(f"ranked merged: {len(out)}")
    return out

# ===== كاش (قراءة/كتابة ذرّية) =====

def _cache_keys(inst: str) -> Tuple[str, str]:
    inst = inst.upper().strip()
    return f"meta_{inst}", f"list_{inst}"


def _read_cache(inst: str) -> Tuple[Dict[str, Dict], List[str]]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, []
        k_meta, k_list = _cache_keys(inst)
        meta = data.get(k_meta, {})
        lst  = data.get(k_list, [])
        if not isinstance(meta, dict): meta = {}
        if not isinstance(lst, list): lst = []
        _dbg(f"cache read {inst}: meta={len(meta)} list={len(lst)}")
        return meta, [_normalize_symbol(s) for s in lst]
    except Exception:
        return {}, []


def _write_cache_atomic(inst: str, symbols_meta: Dict[str, Dict], symbols_list: List[str]) -> None:
    if not USE_SYMBOLS_CACHE:
        return
    if not symbols_list:
        # لا تكتب أبداً قائمة فارغة
        _dbg("skip cache write: empty list")
        return
    # حمّل الموجود أولاً
    base: Dict[str, Dict] = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                j = json.load(f)
                if isinstance(j, dict):
                    base = j
        except Exception:
            base = {}
    k_meta, k_list = _cache_keys(inst)
    base[k_meta] = symbols_meta or {}
    base[k_list] = [_normalize_symbol(s) for s in symbols_list]

    tmpdir = tempfile.mkdtemp(prefix="symcache_")
    tmpfile = os.path.join(tmpdir, "cache.json")
    try:
        with open(tmpfile, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
        shutil.move(tmpfile, CACHE_PATH)  # استبدال ذرّي
        _dbg(f"cache write {inst}: meta={len(symbols_meta)} list={len(symbols_list)}")
    finally:
        try: shutil.rmtree(tmpdir)
        except: pass

# ===== Baseline =====

_DEFAULT_BASELINE = [
    "BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT",
    "ADA/USDT","DOGE/USDT","TON/USDT","TRX/USDT","DOT/USDT"
]

def _baseline_list() -> List[str]:
    # قابل للتخصيص عبر ENV
    src = BASELINE_SYMBOLS_ENV or _DEFAULT_BASELINE
    return _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in src])

# ===== المنتج النهائي =====

def list_symbols(inst: str, target: int, min_usd_vol: float, *, return_meta: bool = False
                 ) -> Union[List[str], Tuple[List[str], Dict[str, Dict]]]:
    """
    افتراضيًا تُرجِع قائمة فقط (توافقًا مع الإصدارات القديمة).
    عيّن return_meta=True لتحصل على (list, meta).
    *لن* تعيد هذه الدالة قائمة فارغة؛ دائمًا هناك fallback آمن.
    """
    inst = inst.upper().strip()
    ranked = get_ranked(inst)

    out_syms: List[str] = []
    meta: Dict[str, Dict] = {}
    seen = set()

    # أضف include أولاً (لا يخضع لحد السيولة)
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

    # تقليم للهدف
    if target > 0:
        out_syms = out_syms[:target]

    # إذا النتيجة صفر → جرّب الكاش ثم baseline
    if not out_syms:
        cache_meta, cache_list = _read_cache(inst)
        if cache_list:
            # أعطِ أولوية لـ INCLUDE ثم أكمل من الكاش
            merged = _dedupe_keep_order(list(INCLUDE_SYMBOLS) + cache_list)
            merged = [s for s in merged if _filter_symbol(s)]
            if target > 0:
                merged = merged[:target]
            out_syms = merged
            # meta من الكاش إن وجد وإلا STATIC
            if cache_meta:
                meta = {s: cache_meta.get(s, {"volUsd": None, "source": "CACHED"}) for s in out_syms}
            else:
                meta = {s: {"volUsd": None, "source": "CACHED"} for s in out_syms}
            _dbg(f"fallback: using CACHE list → {len(out_syms)}")
        else:
            base = _baseline_list()
            merged = _dedupe_keep_order(list(INCLUDE_SYMBOLS) + base)
            merged = [s for s in merged if _filter_symbol(s)]
            if target > 0:
                merged = merged[:target]
            out_syms = merged or base[:max(1, target)]  # تأكد من عدم الصفر
            meta = {s: {"volUsd": None, "source": "BASELINE"} for s in out_syms}
            _dbg(f"fallback: using BASELINE list → {len(out_syms)}")

    # كاش ميتا + لست إن كانت النتيجة غير صفرية
    if out_syms:
        _write_cache_atomic(inst, meta, out_syms)

    if return_meta:
        return out_syms, meta
    return out_syms


def _prepare_symbols() -> Tuple[List[str], Dict[str, Dict]]:
    """
    المنفذ النهائي الذي يجهّز SYMBOLS/SYMBOLS_META
    """
    if not AUTO_EXPAND_SYMBOLS:
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        base = [s for s in base if _filter_symbol(s)]
        base = base[:TARGET_SYMBOLS_COUNT]
        return base, {s: {"volUsd": None, "source": "STATIC"} for s in base}

    try:
        return list_symbols(INST_TYPE, TARGET_SYMBOLS_COUNT, MIN_24H_USD_VOL, return_meta=True)  # (list, meta)
    except Exception as e:
        _dbg(f"prepare error: {e}")
        # fallback: include أو baseline
        base = _dedupe_keep_order([_alias_symbol(_normalize_symbol(s)) for s in INCLUDE_SYMBOLS])
        base = [s for s in base if _filter_symbol(s)]
        if not base:
            base = _baseline_list()
        base = base[:TARGET_SYMBOLS_COUNT]
        return base, {s: {"volUsd": None, "source": "STATIC"} for s in base}

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
    ap.add_argument("--export", default="", help="اكتب meta+list إلى ملف JSON")
    ap.add_argument("--with-meta", action="store_true", help="أعد الميتا مع القوائم في CLI")
    args = ap.parse_args()

    if args.with_meta:
        syms, meta = list_symbols(args.inst, args.target, args.minvol, return_meta=True)
    else:
        syms = list_symbols(args.inst, args.target, args.minvol)
        meta = {}

    if args.to_print:
        print(f"[list] {args.inst} total={len(syms)} (minvol={args.minvol})")
        for i, s in enumerate(syms[:args.to_print], 1):
            m = meta.get(s, {})
            vol = m.get("volUsd", "-")
            src = m.get("source", "-")
            print(f"{i:>3}. {s:<15} volUsd≈{vol}  src={src}")

    if args.export:
        # نكتب هيكلًا بسيطًا يحتوي على القائمة والميتا
        payload = {"list": syms, "meta": meta}
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[export] wrote list+meta → {args.export}")
        except Exception as e:
            print(f"[export][err] {e}")

if __name__ == "__main__":
    main()

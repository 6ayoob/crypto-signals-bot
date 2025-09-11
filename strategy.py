# -*- coding: utf-8 -*-
from __future__ import annotations
"""
strategy.py — Router (BRK/PULL/RANGE/SWEEP/VBR) + MTF + S/R + VWAP/AVWAP
Balanced+ v2.5 — Per-Symbol Profiles (JSON/CSV) — Ready for Render

الهدف:
- نسخة استراتيجية متكاملة تُستورد مباشرة: from strategy import check_signal
- دعم بروفايلات لكل رمز عبر ملف إعدادات خارجي (JSON/CSV) اختياري.
- عدم الكتابة في /mnt/data. جميع الملفات المؤقتة/الحالة تُكتب افتراضيًا في /tmp/market-watchdog
  (يمكن تغييره عبر APP_DATA_DIR)، والـ STATE_FILE عبر STRATEGY_STATE_FILE.

ENV (اختياري):
  APP_DATA_DIR=/tmp/market-watchdog
  STRATEGY_STATE_FILE=/tmp/market-watchdog/strategy_state.json
  SYMBOLS_FILE=symbols_config.json  (أو symbols.csv)
  RISK_MODE=conservative|balanced|aggressive
  BRK_HOUR_START=11  BRK_HOUR_END=23
  MAX_POS_FUNDING_MAJ=0.00025  MAX_POS_FUNDING_ALT=0.00025
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os, json, math, time, csv
import pandas as pd
import numpy as np

# ========= مسارات قابلة للكتابة (متوافقة مع Render) =========
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ملف حالة الاستراتيجية (آخر إشارة) — افتراضي داخل APP_DATA_DIR
STATE_FILE = os.getenv("STRATEGY_STATE_FILE", str(APP_DATA_DIR / "strategy_state.json"))

# ========= أساسيات =========
VOL_MA = 20
ATR_PERIOD = 14
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200

RISK_MODE = os.getenv("RISK_MODE", "balanced").lower()
RISK_PROFILES = {
    "conservative": {"SCORE_MIN": 78, "ATR_BAND": (0.0018, 0.015), "RVOL_MIN": 1.05, "TP_R": (1.0, 1.8, 3.0), "HOLDOUT_BARS": 3, "MTF_STRICT": True},
    "balanced":     {"SCORE_MIN": 72, "ATR_BAND": (0.0015, 0.020), "RVOL_MIN": 1.00, "TP_R": (1.0, 2.0, 3.5), "HOLDOUT_BARS": 2, "MTF_STRICT": True},
    "aggressive":   {"SCORE_MIN": 68, "ATR_BAND": (0.0012, 0.030), "RVOL_MIN": 0.95, "TP_R": (1.2, 2.4, 4.0), "HOLDOUT_BARS": 1, "MTF_STRICT": False},
}
_cfg = RISK_PROFILES.get(RISK_MODE, RISK_PROFILES["balanced"])

USE_VWAP, USE_ANCHORED_VWAP = True, True
VWAP_TOL_BELOW, VWAP_MAX_DIST_PCT = 0.002, 0.008
USE_FUNDING_GUARD, USE_OI_TREND, USE_BREADTH_ADJUST = True, True, True
USE_PARABOLIC_GUARD, MAX_SEQ_BULL = True, 3
MAX_BRK_DIST_ATR, BREAKOUT_BUFFER = 0.90, 0.0015
RSI_EXHAUSTION, DIST_EMA50_EXHAUST_ATR = 78.0, 2.8

# أهداف / دخول
TRAIL_AFTER_TP2, TRAIL_AFTER_TP2_ATR = True, 1.0
USE_MAX_BARS_TO_TP1, MAX_BARS_TO_TP1_BASE = True, 8
ENTRY_ZONE_WIDTH_R, ENTRY_MIN_PCT, ENTRY_MAX_R = 0.25, 0.005, 0.60
ENABLE_MULTI_TARGETS = True
TARGETS_MODE_BY_SETUP = {"BRK": "r", "PULL": "r", "RANGE": "pct", "SWEEP": "pct", "VBR":"pct"}
TARGETS_R5   = (1.0, 1.8, 3.0, 4.5, 6.0)
ATR_MULT_RANGE = (1.5, 2.5, 3.5, 4.5, 6.0)
ATR_MULT_VBR   = (0.0, 0.6, 1.2, 1.8, 2.4)

# S/R & Fib
USE_SR, SR_WINDOW = True, 40
RES_BLOCK_NEAR, SUP_BLOCK_NEAR = 0.004, 0.003
USE_FIB, SWING_LOOKBACK, FIB_TOL = True, 60, 0.004

# حالة و Relax
LOG_REJECTS = os.getenv("STRATEGY_LOG_REJECTS", "1").strip().lower() in ("1","true","yes","on")
AUTO_RELAX_AFTER_HRS_1 = int(os.getenv("AUTO_RELAX_AFTER_HRS_1", "24"))
AUTO_RELAX_AFTER_HRS_2 = int(os.getenv("AUTO_RELAX_AFTER_HRS_2", "48"))

# رسائل
MOTIVATION = {
    "entry": "🔥 دخول {symbol}! خطة أهداف على R — فلنلتزم 👊",
    "tp1":   "🎯 T1 تحقق على {symbol}! انقل SL للتعادل — استمر ✨",
    "tp2":   "🚀 T2 على {symbol}! فعّلنا التريلينغ — حماية المكسب 🛡️",
    "tp3":   "🏁 T3 على {symbol}! صفقة ممتازة 🌟",
    "tpX":   "🏁 هدف تحقق على {symbol}! استمرار ممتاز 🌟",
    "sl":    "🛑 SL على {symbol}. حماية رأس المال أولًا — فرص أقوى قادمة 🔄",
    "time":  "⌛ خروج زمني على {symbol} — الحركة لم تتفعّل سريعًا، خرجنا بخفّة 🔎",
}

# تتبعات لمنع التكرار/الهولد أوت
_LAST_ENTRY_BAR_TS: Dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: Dict[str, int] = {}

# ========== أدوات عامة ==========
def _now() -> int: return int(time.time())

def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_signal_ts": 0}

def _save_state(s):
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass

def mark_signal_now():
    s = _load_state(); s["last_signal_ts"] = _now(); _save_state(s)

def hours_since_last_signal() -> float:
    ts = _load_state().get("last_signal_ts", 0)
    if not ts: return 1e9
    return (_now() - int(ts)) / 3600.0

def relax_level() -> int:
    h = hours_since_last_signal()
    if h >= AUTO_RELAX_AFTER_HRS_2: return 2
    if h >= AUTO_RELAX_AFTER_HRS_1: return 1
    return 0

def apply_relax(base_cfg: dict) -> dict:
    """إرخاء تدريجي عند الجفاف: Score/RVOL/ATR band + MIN_T1."""
    lvl = relax_level()
    out = dict(base_cfg)
    # Score
    if lvl >= 1: out["SCORE_MIN"] = max(0, out["SCORE_MIN"] - 4)
    if lvl >= 2: out["SCORE_MIN"] = max(0, out["SCORE_MIN"] - 4)
    # RVOL
    if lvl >= 1: out["RVOL_MIN"] = max(0.90, out["RVOL_MIN"] - 0.05)
    if lvl >= 2: out["RVOL_MIN"] = max(0.85, out["RVOL_MIN"] - 0.05)
    # ATR band widen
    lo, hi = out["ATR_BAND"]
    if lvl >= 1: lo, hi = lo * 0.9, hi * 1.1
    if lvl >= 2: lo, hi = lo * 0.85, hi * 1.15
    out["ATR_BAND"] = (max(1e-5, lo), max(hi, lo + 5e-5))
    # T1 distance
    out["MIN_T1_ABOVE_ENTRY"] = 0.010 if lvl == 0 else (0.008 if lvl == 1 else 0.006)
    # Holdout
    out["HOLDOUT_BARS_EFF"] = max(1, base_cfg.get("HOLDOUT_BARS", 2) - lvl)
    out["RELAX_LEVEL"] = lvl
    return out

def _log_reject(symbol: str, msg: str):
    if LOG_REJECTS:
        print(f"[strategy][reject] {symbol}: {msg}")

# ========= تحميل إعدادات السِمبلز =========
# ترتيب البحث: ENV -> cwd json/csv -> APP_DATA_DIR json/csv
SYMBOLS_FILE_ENV = os.getenv("SYMBOLS_FILE", "symbols_config.json")
SYMBOLS_FILE_CANDIDATES = [
    SYMBOLS_FILE_ENV,
    "symbols_config.json",
    "symbols.csv",
    str(APP_DATA_DIR / "symbols_config.json"),
    str(APP_DATA_DIR / "symbols.csv"),
]
_SYMBOL_PROFILES: Dict[str, dict] = {}

def _load_symbols_file():
    for path in SYMBOLS_FILE_CANDIDATES:
        try:
            if os.path.isfile(path):
                if path.endswith(".json"):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            for k, v in data.items():
                                _SYMBOL_PROFILES[k.upper()] = dict(v or {})
                            return
                elif path.endswith(".csv"):
                    with open(path, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            sym = (row.get("symbol") or "").upper()
                            if not sym: continue
                            prof = {}
                            for key in ("class","atr_lo","atr_hi","rvol_min","min_quote_vol","ema50_req_R","ema200_req_R","vbr_min_dev_atr","oi_window","oi_down_thr","max_pos_funding","brk_hour_start","brk_hour_end","guard_refs"):
                                val = row.get(key, "")
                                if val == "": continue
                                if key == "class":
                                    prof[key] = val.strip().lower()
                                elif key == "guard_refs":
                                    prof[key] = [s.strip().upper() for s in val.split(";") if s.strip()]
                                else:
                                    try:
                                        prof[key] = float(val) if "." in val or "e" in val.lower() else int(val)
                                    except Exception:
                                        try:
                                            prof[key] = float(val)
                                        except Exception:
                                            prof[key] = val
                            _SYMBOL_PROFILES[sym] = prof
                    return
        except Exception as e:
            print("[symbols][warn]", e)

_load_symbols_file()

def get_symbol_profile(symbol: str) -> dict:
    """يبني بروفايل للرمز مع تطبيق Overrides من ملفك إن وُجِد."""
    s = (symbol or "").upper()
    prof = _SYMBOL_PROFILES.get(s, {}).copy()

    cls = (prof.get("class") or ("major" if s in ("BTCUSDT","BTCUSD","ETHUSDT","ETHUSD") else "alt")).lower()
    is_major = (cls == "major")

    # Defaults per class
    if is_major:
        base = {
            "atr_lo": 0.0020, "atr_hi": 0.0240, "rvol_min": 1.00, "min_quote_vol": 20000,
            "ema50_req_R": 0.25, "ema200_req_R": 0.35, "vbr_min_dev_atr": 0.6,
            "oi_window": 10, "oi_down_thr": 0.0,
            "max_pos_funding": float(os.getenv("MAX_POS_FUNDING_MAJ", "0.00025")),
            "brk_hour_start": int(os.getenv("BRK_HOUR_START","11")), "brk_hour_end": int(os.getenv("BRK_HOUR_END","23")),
            "guard_refs": prof.get("guard_refs") or ["BTCUSDT","ETHUSDT"],
            "class": "major"
        }
    else:
        base = {
            "atr_lo": 0.0025, "atr_hi": 0.0300, "rvol_min": 1.05, "min_quote_vol": 100000,
            "ema50_req_R": 0.30, "ema200_req_R": 0.45, "vbr_min_dev_atr": 0.7,
            "oi_window": 14, "oi_down_thr": -0.03,
            "max_pos_funding": float(os.getenv("MAX_POS_FUNDING_ALT", "0.00025")),
            "brk_hour_start": int(os.getenv("BRK_HOUR_START","11")), "brk_hour_end": int(os.getenv("BRK_HOUR_END","23")),
            "guard_refs": prof.get("guard_refs") or ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"],
            "class": "alt"
        }

    base.update(prof)
    return base

# ========= حسابات المؤشرات =========
def _trim(df: pd.DataFrame, n: int = 240) -> pd.DataFrame:
    return df.tail(n).copy()

def ema(series, period): return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    d = series.diff()
    gain = d.where(d > 0, 0.0); loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean().replace(0, 1e-9)
    rs = ag / al; return 100 - (100 / (1 + rs))

def macd_cols(df, fast=12, slow=26, signal=9):
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]; return df

def atr_series(df, period=14):
    c = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]).abs(), (df["high"]-c).abs(), (df["low"]-c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def vwap_series(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    numer = (tp * df["volume"]).cumsum()
    denom = (df["volume"]).cumsum().replace(0, np.nan)
    return numer / denom

def zscore(series: pd.Series, win: int = 20) -> pd.Series:
    r = series.rolling(win)
    m = r.mean(); s = r.std(ddof=0).replace(0, np.nan)
    return (series - m) / s

def is_nrN(series_high: pd.Series, series_low: pd.Series, N: int = 7) -> pd.Series:
    rng = (series_high - series_low).abs()
    return rng == rng.rolling(N).min()

def add_indicators(df):
    df["ema9"]   = ema(df["close"], EMA_FAST)
    df["ema21"]  = ema(df["close"], EMA_SLOW)
    df["ema50"]  = ema(df["close"], EMA_TREND)
    df["ema200"] = ema(df["close"], EMA_LONG)
    df["rsi"]    = rsi(df["close"], 14)
    df["vol_ma20"] = df["volume"].rolling(VOL_MA, min_periods=1).mean()
    df = macd_cols(df)
    df["atr"] = atr_series(df, ATR_PERIOD)
    df["vwap"] = vwap_series(df)
    df["nr7"] = is_nrN(df["high"], df["low"], 7)
    df["nr4"] = is_nrN(df["high"], df["low"], 4)
    df["vol_z20"] = zscore(df["volume"], 20)
    return df

# ========= S/R & FIB =========
def get_sr_on_closed(df, window=40) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < window + 3: return None, None
    hi = float(df.iloc[-(window+1):-1]["high"].max())
    lo = float(df.iloc[-(window+1):-1]["low"].min())
    if not math.isfinite(hi) or not math.isfinite(lo): return None, None
    return float(lo), float(hi)

def recent_swing(df, lookback=60) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
    if len(df) < lookback + 5: return None, None, None, None
    seg = df.iloc[-(lookback+1):-1]
    hhv = seg["high"].max(); llv = seg["low"].min()
    if pd.isna(hhv) or pd.isna(llv) or hhv <= llv: return None, None, None, None
    hi_idx = seg["high"].idxmax(); lo_idx = seg["low"].idxmin()
    return float(hhv), float(llv), int(hi_idx), int(lo_idx)

def near_any_fib(price: float, hhv: float, llv: float, tol: float) -> Tuple[bool, str]:
    rng = hhv - llv
    if rng <= 0: return False, ""
    fib382 = hhv - rng * 0.382; fib618 = hhv - rng * 0.618
    for lvl, name in ((fib382, "Fib 0.382"), (fib618, "Fib 0.618")):
        if abs(price - lvl) / max(lvl, 1e-9) <= tol: return True, name
    return False, ""

def _fib_ok(price: float, df: pd.DataFrame) -> bool:
    try:
        sw = recent_swing(df, SWING_LOOKBACK)
        if not sw or sw[0] is None or sw[1] is None:
            return False
        return near_any_fib(price, sw[0], sw[1], FIB_TOL)[0]
    except Exception:
        return False

# ========= Anchored VWAP =========
def avwap_from_index(df: pd.DataFrame, idx: int) -> Optional[float]:
    if idx is None or idx < 0 or idx >= len(df)-1: return None
    sub = df.iloc[idx:].copy()
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3.0
    numer = (tp * sub["volume"]).cumsum()
    denom = (sub["volume"]).cumsum().replace(0, np.nan)
    v = numer / denom
    return float(v.iloc[-2]) if len(v) >= 2 and math.isfinite(v.iloc[-2]) else None

# ========= نظام السوق =========
def detect_regime(df) -> str:
    c = df["close"]; e50 = df["ema50"]
    up = (c.iloc[-1] > e50.iloc[-1]) and (e50.diff(10).iloc[-1] > 0)
    if up: return "trend"
    seg = df.iloc[-80:]
    width = (seg["high"].max() - seg["low"].min()) / max(seg["close"].iloc[-1], 1e-9)
    atrp = float(seg["atr"].iloc[-2]) / max(seg["close"].iloc[-2], 1e-9)
    return "range" if width <= 6 * atrp else "mixed"

# ========= برايس أكشن =========
def candle_quality(row, rvol_hint: float | None = None) -> bool:
    o = float(row["open"]); c = float(row["close"]); h = float(row["high"]); l = float(row["low"])
    tr = max(h - l, 1e-9); body = abs(c - o); upper_wick = h - max(c, o)
    body_pct = body / tr; upwick_pct = upper_wick / tr
    min_body = 0.55 if (rvol_hint is None or rvol_hint < 1.3) else 0.45
    return (c > o) and (body_pct >= min_body) and (upwick_pct <= 0.35)

def is_bull_engulf(prev, cur) -> bool:
    return (float(cur["close"]) > float(cur["open"]) and
            float(prev["close"]) < float(prev["open"]) and
            (float(cur["close"]) - float(cur["open"])) > (abs(float(prev["close"]) - float(prev["open"])) * 0.9) and
            float(cur["close"]) >= float(prev["open"]))

def is_hammer(cur) -> bool:
    h = float(cur["high"]); l = float(cur["low"]); o = float(cur["open"]); c = float(cur["close"])
    tr = max(h - l, 1e-9); body = abs(c - o); lower_wick = min(o, c) - l
    return (c > o) and (lower_wick / tr >= 0.5) and (body / tr <= 0.35) and ((h - max(o, c)) / tr <= 0.15)

def is_inside_break(pprev, prev, cur) -> bool:
    cond_inside = (float(prev["high"]) <= float(pprev["high"]) and float(prev["low"]) >= float(pprev["low"]))
    return cond_inside and (float(cur["high"]) > float(prev["high"])) and (float(cur["close"]) > float(prev["high"]))

def swept_liquidity(prev, cur) -> bool:
    return (float(cur["low"]) < float(prev["low"]) and (float(cur["close"]) > float(prev["close"])) )

def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)

# ========= MTF + ميزات خارجية =========
def _df_from_ohlcv(ohlcv: List[list]) -> Optional[pd.DataFrame]:
    try:
        df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().reset_index(drop=True)
        df = _trim(df, 240)
        return add_indicators(df)
    except Exception:
        return None

def pass_mtf_filter_any(ohlcv_htf) -> Tuple[bool, bool]:
    frames: List[pd.DataFrame] = []
    if isinstance(ohlcv_htf, list):
        d = _df_from_ohlcv(ohlcv_htf)
        if d is not None: frames.append(d)
    elif isinstance(ohlcv_htf, dict):
        for k in ("H1","H4","D1"):
            data = ohlcv_htf.get(k)
            if data:
                d = _df_from_ohlcv(data)
                if d is not None: frames.append(d)
    if not frames:
        return False, False
    ok_count = 0
    for dfh in frames:
        if len(dfh) < 60: continue
        closed = dfh.iloc[-2]
        conds = [
            float(closed["close"]) > float(closed["ema50"]),
            float(closed["macd_hist"]) > 0,
            float(closed["rsi"]) > 50,
            float(dfh["ema50"].diff(5).iloc[-2]) > 0,
        ]
        ok_count += int(all(conds))
    return True, (ok_count >= 1)

def extract_features(ohlcv_htf) -> Dict[str, object]:
    out: Dict[str, object] = {}
    if isinstance(ohlcv_htf, dict):
        feats = ohlcv_htf.get("features") or {}
        if isinstance(feats, dict):
            out.update(feats)
    return out

# ========= ATR Band تكيفي =========
def adapt_atr_band(atr_pct_series: pd.Series, base_band: Tuple[float, float]) -> Tuple[float, float]:
    if atr_pct_series is None or len(atr_pct_series) < 50:
        return base_band
    recent = atr_pct_series.tail(200).clip(lower=0)
    med = float(recent.median())
    std = float(recent.std(ddof=0)) if recent.std(ddof=0) > 0 else 0.0
    lo, hi = base_band
    center = (lo + hi) / 2.0
    shift = 0.5 * (med - center)
    widen = 0.5 * std
    new_lo = max(1e-5, lo + shift - widen)
    new_hi = hi + shift + widen
    if new_lo >= new_hi:
        new_lo, new_hi = lo, hi
    ctr = (new_lo + new_hi) / 2.0
    half = (new_hi - new_lo) / 2.0
    half *= 1.10
    return (max(1e-5, ctr - half), ctr + half)

# ========= بناء الأهداف/الوقف =========
def _build_targets_r(entry: float, sl: float, tp_r: Tuple[float, ...]) -> List[float]:
    R = max(entry - sl, 1e-9)
    return [entry + r*R for r in tp_r]

def _build_targets_pct_from_atr(price: float, atr: float, multipliers: Tuple[float, ...]) -> Tuple[List[float], Tuple[float,...]]:
    pcts = [max(atr / max(price, 1e-9) * m, 0.002) for m in multipliers]  # حد أدنى 0.2%
    return [price * (1 + p) for p in pcts], tuple(pcts)

def _clamp_t1_below_res(entry: float, t1: float, res: Optional[float], buf_pct: float = 0.0015) -> Tuple[float, bool]:
    if res is None: return t1, False
    if res * (1 - buf_pct) < t1: return float(res * (1 - buf_pct)), True
    return t1, False

def _protect_sl_with_swing(df, entry_price: float, atr: float) -> float:
    base_sl = entry_price - max(atr * 0.9, entry_price * 0.002)
    try:
        swing_low = float(df.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
        if swing_low < entry_price: return min(base_sl, swing_low)
    except Exception:
        pass
    return base_sl

# ========= سكور =========
def score_signal(
    struct_ok: bool,
    rvol: float,
    atr_pct: float,
    ema_align: bool,
    mtf_pass: bool,
    srdist_R: float,
    mtf_has_frames: bool,
    rvol_min: float,
    atr_band: Tuple[float, float],
    oi_trend: Optional[float] = None,
    breadth_pct: Optional[float] = None,
    avwap_confluence: Optional[bool] = None,
) -> Tuple[int, Dict[str, float]]:
    w = {"struct": 27, "rvol": 13, "atr": 13, "ema": 13, "mtf": 13, "srdist": 8, "oi": 6, "breadth": 4, "avwap": 3}
    sc = 0.0; bd: Dict[str, float] = {}

    bd["struct"] = w["struct"] if struct_ok else 0; sc += bd["struct"]

    rvol_score = min(max((rvol - rvol_min) / max(0.5, rvol_min), 0), 1) * w["rvol"]
    bd["rvol"] = rvol_score; sc += bd["rvol"]

    lo, hi = atr_band
    center = (lo + hi)/2
    if lo <= atr_pct <= hi:
        atr_score = (1 - abs(atr_pct - center)/max(center - lo, 1e-9)) * w["atr"]
        bd["atr"] = max(0, min(w["atr"], atr_score)); sc += bd["atr"]
    else:
        bd["atr"] = 0

    bd["ema"] = w["ema"] if ema_align else 0; sc += bd["ema"]

    if mtf_has_frames:
        bd["mtf"] = w["mtf"] if mtf_pass else 0
        sc += bd["mtf"]
        if not mtf_pass:
            sc -= 15; bd["mtf_penalty"] = -15
    else:
        bd["mtf"] = 0; bd["mtf_penalty"] = -15; sc -= 15

    srd = max(srdist_R, 0.0)
    bd["srdist"] = min(srd / 1.5, 1.0) * w["srdist"]; sc += bd["srdist"]

    if oi_trend is not None:
        bd["oi"] = max(min(oi_trend, 0.2), -0.2) / 0.2 * w["oi"]
        sc += bd["oi"]

    if breadth_pct is not None:
        bd["breadth"] = ((breadth_pct - 0.5) * 2.0) * w["breadth"]
        sc += bd["breadth"]

    if avwap_confluence:
        bd["avwap"] = w["avwap"]; sc += bd["avwap"]
    else:
        bd["avwap"] = 0

    return int(round(sc)), bd

# ========= Quality Control =========
def bar_is_outlier(row, atr: float) -> bool:
    rng = float(row["high"]) - float(row["low"])
    if atr <= 0: return False
    return (rng > 6.0 * atr) or (float(row["volume"]) <= 0)

# ========= حارس سوق ديناميكي بالاعتماد على guard_refs =========
def market_guard_ok(symbol_profile: dict, feats: dict) -> bool:
    refs = symbol_profile.get("guard_refs") or []
    if not refs:
        return True
    # نبحث أولاً في market_state (أدق)، ثم majors_state (توافقاً مع نسخ قديمة)
    ms = feats.get("market_state")
    if isinstance(ms, dict) and ms:
        good = 0
        for r in refs:
            st = ms.get(r)
            if not isinstance(st, dict): continue
            try:
                c, e = float(st.get("close", 0)), float(st.get("ema200", 0))
                rsi = float(st.get("rsi_h1", 50))
                if c > e and rsi >= 48: good += 1
            except Exception:
                pass
        # نطلب على الأقل 50% من المراجع تكون إيجابية
        return good >= max(1, int(len(refs)*0.5))
    # fallback: majors_state (list of dicts)
    maj = feats.get("majors_state", [])
    if isinstance(maj, list) and maj:
        above = 0
        for x in maj:
            try:
                c_ = float(x.get("close", 0)); e_ = float(x.get("ema200", 0))
                if c_ > e_: above += 1
            except Exception:
                pass
        breadth_pct = above / max(1, len(maj))
        return breadth_pct >= 0.5
    return True  # إذا لا تتوفر بيانات، لا نمنع

# ========= المولّد الرئيسي =========
def check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[object] = None) -> Optional[Dict]:
    if not ohlcv or len(ohlcv) < 80:
        _log_reject(symbol, "insufficient_bars")
        return None

    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 60:
        _log_reject(symbol, "after_cleaning_len<60")
        return None

    df = _trim(df, 240)
    df = add_indicators(df)
    if len(df) < 60:
        _log_reject(symbol, "after_indicators_len<60")
        return None

    prev2  = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # QC
    atr = float(df["atr"].iloc[-2]); atr_pct = atr / max(price, 1e-9)
    if bar_is_outlier(closed, atr):
        _log_reject(symbol, "bar_outlier"); return None

    # بروفايل الرمز + Auto-Relax
    prof = get_symbol_profile(symbol)
    base_cfg = dict(_cfg)
    base_cfg["ATR_BAND"] = (prof["atr_lo"], prof["atr_hi"])
    base_cfg["RVOL_MIN"] = max(base_cfg.get("RVOL_MIN", 1.0), float(prof["rvol_min"]))
    thr = apply_relax(base_cfg)
    MIN_T1_ABOVE_ENTRY = thr.get("MIN_T1_ABOVE_ENTRY", 0.010)
    holdout_eff = thr.get("HOLDOUT_BARS_EFF", base_cfg.get("HOLDOUT_BARS", 2))

    # منع التكرار + Holdout
    _LAST_ENTRY_BAR_TS.setdefault("##", 0)  # bootstrap
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts:
        _log_reject(symbol, "duplicate_bar"); return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < holdout_eff:
        _log_reject(symbol, f"holdout<{holdout_eff}"); return None

    # سيولة
    if price * float(closed["volume"]) < float(prof["min_quote_vol"]):
        _log_reject(symbol, f"low_quote_vol<{prof['min_quote_vol']}")
        return None

    # نطاق ATR ديناميكي
    base_lo, base_hi = thr["ATR_BAND"]
    lo_dyn, hi_dyn = adapt_atr_band((df["atr"] / df["close"]).dropna(), (base_lo, base_hi))
    if not (lo_dyn <= atr_pct <= hi_dyn):
        _log_reject(symbol, f"atr_pct_outside[{atr_pct:.4f}] not in [{lo_dyn:.4f},{hi_dyn:.4f}]")
        return None

    # RVOL & Spike
    vma = float(closed.get("vol_ma20") or 0.0)
    rvol = (float(closed["volume"]) / (vma + 1e-9)) if vma > 0 else 0.0
    vol_spike = float(df["vol_z20"].iloc[-2]) >= 1.2
    if rvol < thr["RVOL_MIN"] and not vol_spike:
        _log_reject(symbol, f"rvol<{thr['RVOL_MIN']:.2f} and no spike")
        return None

    # MTF + ميزات خارجية
    regime = detect_regime(df)
    mtf_has_frames, mtf_pass = pass_mtf_filter_any(ohlcv_htf)
    feats = extract_features(ohlcv_htf)

    # Funding guard (per symbol)
    if USE_FUNDING_GUARD:
        fr = feats.get("funding_rate")
        try:
            if fr is not None and float(fr) > float(prof["max_pos_funding"]):
                _log_reject(symbol, f"funding_rate_high={float(fr):.5f}>{float(prof['max_pos_funding']):.5f}")
                return None
        except Exception:
            pass

    # OI trend (per symbol window/threshold)
    oi_sc = None
    if USE_OI_TREND:
        oi_hist = feats.get("oi_hist")
        if isinstance(oi_hist, (list, tuple)) and len(oi_hist) >= int(prof["oi_window"]):
            try:
                s = pd.Series(oi_hist[-int(prof["oi_window"]):], dtype="float64")
                oi_sc = float((s.iloc[-1] - s.iloc[0]) / max(s.iloc[0], 1e-9))
                if oi_sc < float(prof["oi_down_thr"]):
                    _log_reject(symbol, f"oi_downtrend={oi_sc:.2%} < {float(prof['oi_down_thr']):.2%}")
                    return None
            except Exception:
                pass

    # Breadth adjust (اختياري)
    breadth_pct = None
    majors_state = feats.get("majors_state", [])
    try:
        if isinstance(majors_state, list) and majors_state:
            above = 0
            for x in majors_state:
                c_ = float(x.get("close", 0))
                e_ = float(x.get("ema200", 0))
                if c_ > 0 and e_ > 0 and c_ > e_: above += 1
            breadth_pct = above / max(1, len(majors_state))
            if breadth_pct >= 0.65:
                thr["SCORE_MIN"] = max(60, thr["SCORE_MIN"] - 4)
            elif breadth_pct <= 0.35:
                thr["SCORE_MIN"] = min(86, thr["SCORE_MIN"] + 4)
    except Exception:
        pass

    # حارس سوق بالـ guard_refs
    if not market_guard_ok(prof, feats):
        _log_reject(symbol, "market_guard_block"); return None

    # اتجاه/جودة + VWAP/AVWAP
    vwap_now = float(df["vwap"].iloc[-2]) if USE_VWAP else price
    above_vwap = (not USE_VWAP) or (price >= vwap_now * (1 - VWAP_TOL_BELOW))

    avwap_val = None; avwap_ok = True
    if USE_ANCHORED_VWAP:
        hhv, llv, hi_idx, lo_idx = recent_swing(df, SWING_LOOKBACK)
        if lo_idx is not None:
            avwap_val = avwap_from_index(df, lo_idx)
            if avwap_val is not None:
                avwap_ok = price >= avwap_val * (1 - 0.002)

    ema_align = ((float(closed["ema9"]) > float(closed["ema21"]) > float(closed["ema50"])) or (price > float(closed["ema50"])))
    ema_align = ema_align and above_vwap and avwap_ok

    if not (price > float(closed["open"])):
        _log_reject(symbol, "close<=open"); return None
    if not ema_align:
        _log_reject(symbol, "ema/vwap/avwap_align_false"); return None
    if not candle_quality(closed, rvol_hint=rvol):
        _log_reject(symbol, "candle_quality_fail"); return None

    # S/R + برايس أكشن
    sup = res = None
    if USE_SR: sup, res = get_sr_on_closed(df, SR_WINDOW)
    rev_hammer  = is_hammer(closed)
    rev_engulf  = is_bull_engulf(prev, closed)
    rev_insideb = is_inside_break(prev2, prev, closed)
    had_sweep   = swept_liquidity(prev, closed)
    near_res = near_level(price, res, RES_BLOCK_NEAR)
    near_sup = near_level(price, sup, SUP_BLOCK_NEAR)

    try:
        hhv_prev = float(df.iloc[-(SR_WINDOW+1):-1]["high"].max())
    except Exception:
        hhv_prev = float(prev["high"])
    breakout_ok = price > hhv_prev * (1.0 + BREAKOUT_BUFFER)

    prev_l = float(prev["low"]); prev2_l = float(prev2["low"])
    retest_band_hi = hhv_prev * (1.0 + 0.0008)
    retest_band_lo = hhv_prev * (1.0 - 0.0025)
    retest_ok = ((retest_band_lo <= prev_l <= retest_band_hi) or (retest_band_lo <= prev2_l <= retest_band_hi))

    nr_recent = bool(df["nr7"].iloc[-2] or df["nr7"].iloc[-3] or df["nr4"].iloc[-2])
    seg = df.iloc[-120:]
    range_width = (seg["high"].max() - seg["low"].min())/max(seg["close"].iloc[-1],1e-9)
    range_atr = float(seg["atr"].iloc[-2])/max(price,1e-9)
    range_env = (range_width <= 6*range_atr)

    # Parabolic guard
    if USE_PARABOLIC_GUARD and atr_pct > 0.020:
        seq_bull = int((df["close"] > df["open"]).tail(6).sum())
        if seq_bull > MAX_SEQ_BULL:
            _log_reject(symbol, "parabolic_runup"); return None

    # EMA distance guards لصفقات الاتجاه
    ema50_req, ema200_req = float(prof["ema50_req_R"]), float(prof["ema200_req_R"])
    ema50 = float(closed["ema50"]); ema200 = float(closed["ema200"])
    dist50_atr = (price - ema50) / max(atr, 1e-9)
    dist200_atr = (price - ema200) / max(atr, 1e-9)
    trend_guard = (dist50_atr >= ema50_req) and (dist200_atr >= ema200_req)
    mixed_guard = (dist50_atr >= (0.15 if prof["class"]=="major" else 0.20))

    # ساعات BRK من البروفايل
    try:
        ts_sec = cur_ts/1000.0 if cur_ts > 1e12 else cur_ts
        hr_riyadh = (datetime.utcfromtimestamp(ts_sec).hour + 3) % 24
    except Exception:
        hr_riyadh = 12
    brk_in_session = (int(prof["brk_hour_start"]) <= hr_riyadh <= int(prof["brk_hour_end"]))

    # اختيار الست-أب
    setup = None; struct_ok = False; reasons: List[str] = []
    brk_far = (price - hhv_prev) / max(atr, 1e-9) > MAX_BRK_DIST_ATR

    if (breakout_ok and retest_ok and not brk_far and (rvol >= thr["RVOL_MIN"] or vol_spike) and brk_in_session):
        if ((regime == "trend" and trend_guard) or (regime != "trend" and mixed_guard)):
            if (rev_insideb or rev_engulf or candle_quality(closed, rvol)):
                setup = "BRK"; struct_ok = True; reasons += ["Breakout+Retest","SessionOK"]

    if (setup is None) and ((regime == "trend") or (regime != "trend" and mixed_guard)):
        pull_near = abs(price - float(closed["ema21"])) / max(price,1e-9) <= 0.005 or (USE_FIB and _fib_ok(price, df))
        if pull_near and above_vwap and avwap_ok and (rev_hammer or rev_engulf or rev_insideb):
            if ((regime == "trend" and trend_guard) or (regime != "trend" and mixed_guard)):
                setup = "PULL"; struct_ok = True; reasons += ["Pullback Reclaim"]

    if (setup is None) and range_env and near_sup and (rev_hammer or candle_quality(closed, rvol)) and nr_recent:
        setup = "RANGE"; struct_ok = True; reasons += ["Range Rotation (NR)"]

    vbr_min_dev = float(prof["vbr_min_dev_atr"])
    if (setup is None and (atr_pct <= 0.015) and nr_recent):
        dev_atr = (vwap_now - price) / max(atr, 1e-9)  # موجب إذا تحت VWAP
        if dev_atr >= vbr_min_dev and (rev_hammer or rev_engulf or candle_quality(closed, rvol)):
            setup = "VBR"; struct_ok = True; reasons += ["VWAP Band Reversion"]

    if (setup is None) and had_sweep and (rev_engulf or candle_quality(closed, rvol) or price > float(closed["ema21"])):
        setup = "SWEEP"; struct_ok = True; reasons += ["Liquidity Sweep"]

    if setup is None:
        _log_reject(symbol, "no_setup_match"); return None

    # Exhaustion guard (لا يطبق على VBR/RANGE)
    dist_ema50_atr = (price - float(closed["ema50"])) / max(atr, 1e-9)
    rsi_now = float(closed["rsi"])
    if (setup in ("BRK","PULL") and rsi_now >= RSI_EXHAUSTION and dist_ema50_atr >= DIST_EMA50_EXHAUST_ATR):
        _log_reject(symbol, f"exhaustion_guard rsi={rsi_now:.1f}, distATR={dist_ema50_atr:.2f}"); return None

    # SL وأهداف
    sl = _protect_sl_with_swing(df, price, atr)
    targets_display_vals = None
    disp_mode = "r"

    if ENABLE_MULTI_TARGETS:
        disp_mode = TARGETS_MODE_BY_SETUP.get(setup, "r")
        if disp_mode == "pct":
            if setup == "VBR":
                t_list, pct_vals = _build_targets_pct_from_atr(price, atr, ATR_MULT_VBR)
                if t_list and USE_VWAP:
                    t_list[0] = max(t_list[0], vwap_now)  # T1≈VWAP
                targets_display_vals = pct_vals
            else:
                t_list, pct_vals = _build_targets_pct_from_atr(price, atr, ATR_MULT_RANGE)
                targets_display_vals = pct_vals
        else:
            t_list = _build_targets_r(price, sl, TARGETS_R5)
            targets_display_vals = TARGETS_R5
    else:
        t_list = _build_targets_r(price, sl, _cfg["TP_R"])
        disp_mode = "r"
        targets_display_vals = _cfg["TP_R"]

    t_list = sorted(t_list)

    # قصّ T1 لو قرب المقاومة
    t1, clamped = _clamp_t1_below_res(price, t_list[0], res, buf_pct=0.0015)
    t_list[0] = t1
    if clamped: reasons.append("T1@ResClamp")

    # رفض لو T1 قريب جدًا
    if (t_list[0] - price)/max(price,1e-9) < MIN_T1_ABOVE_ENTRY:
        _log_reject(symbol, f"t1_entry_gap<{MIN_T1_ABOVE_ENTRY:.3%}"); return None
    if not (sl < price < t_list[0] <= t_list[-1]):
        _log_reject(symbol, "bounds_invalid(sl<price<t1<=tN)"); return None

    # مسافة المقاومة بـ R
    R_val = max(price - sl, 1e-9)
    srdist_R = ((res - price)/R_val) if (res is not None and res > price) else 10.0

    # سكور شامل
    score, bd = score_signal(
        struct_ok, rvol, atr_pct, ema_align, mtf_pass, srdist_R, mtf_has_frames,
        thr["RVOL_MIN"], (lo_dyn, hi_dyn),
        oi_trend=oi_sc, breadth_pct=breadth_pct, avwap_confluence=avwap_ok if USE_ANCHORED_VWAP else None
    )
    if score < thr["SCORE_MIN"]:
        _log_reject(symbol, f"score<{thr['SCORE_MIN']} (got {score})"); return None

    # منطقة دخول ثنائية
    entries = None
    width_r = max(ENTRY_ZONE_WIDTH_R * R_val, price * ENTRY_MIN_PCT)
    width_r = min(width_r, ENTRY_MAX_R * R_val)
    entry_low  = max(sl + 1e-6, price - width_r)
    entry_high = price
    if entry_low < entry_high:
        entries = [round(entry_low, 6), round(entry_high, 6)]

    # حفظ آخر بار
    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # أسباب/Confluence للعرض
    reasons_full: List[str] = []
    if price > float(closed["ema50"]): reasons_full.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons_full.append("EMA9>EMA21")
    if is_hammer(closed): reasons_full.append("Hammer")
    if is_bull_engulf(prev, closed): reasons_full.append("Bull Engulf")
    if is_inside_break(prev2, prev, closed): reasons_full.append("InsideBreak")
    if near_res: reasons_full.append("NearRes")
    if near_sup: reasons_full.append("NearSup")
    if USE_VWAP and above_vwap: reasons_full.append("VWAP OK")
    if USE_ANCHORED_VWAP and avwap_val is not None and avwap_ok: reasons_full.append("AVWAP OK")
    if setup == "VBR": reasons_full.append("VBR")
    reasons_full.append(f"RVOL≥{round(thr['RVOL_MIN'],2)}")
    confluence = (reasons + reasons_full)[:6]

    # رسائل
    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1":   MOTIVATION["tp1"].format(symbol=symbol),
        "tp2":   MOTIVATION["tp2"].format(symbol=symbol),
        "tp3":   MOTIVATION["tp3"].format(symbol=symbol),
        "tp4":   MOTIVATION["tpX"].format(symbol=symbol),
        "tp5":   MOTIVATION["tpX"].format(symbol=symbol),
        "sl":    MOTIVATION["sl"].format(symbol=symbol),
        "time":  "⌛ خروج زمني على {symbol} — الحركة لم تتفعّل سريعًا، خرجنا بخفّة 🔎".format(symbol=symbol),
    }

    # عرض للمشترك
    targets_display = {"mode": disp_mode, "values": list(targets_display_vals)}

    # زمن الوصول لـ T1 (ديناميكي حسب ATR%)
    max_bars_to_tp1 = MAX_BARS_TO_TP1_BASE
    if atr_pct <= 0.008: max_bars_to_tp1 = 10
    elif atr_pct >= 0.020: max_bars_to_tp1 = 6
    if setup in ("BRK","SWEEP"): max_bars_to_tp1 = max(6, max_bars_to_tp1 - 2)

    # Partial ديناميكي حسب السكور
    def _partials_for(score: int, n: int) -> List[float]:
        if score >= 84: base = [0.30, 0.25, 0.20, 0.15, 0.10]
        elif score >= 76: base = [0.35, 0.25, 0.20, 0.12, 0.08]
        else: base = [0.40, 0.25, 0.18, 0.10, 0.07]
        return base[:n]
    partials = _partials_for(score, len(t_list))

    # قاعدة وقف متوافقة (breakeven بعد T1)
    stop_rule = {
        "type": "breakeven_after",
        "at_idx": 0,
        "meta": {
            "intended": "htf_close_below",
            "tf": os.getenv("STOP_RULE_TF", "H4").upper(),
            "htf_level": round(_protect_sl_with_swing(df, price, atr), 6)
        }
    }

    tp1 = t_list[0]; tp2 = t_list[1] if len(t_list) > 1 else t_list[0]
    tp3 = t_list[2] if len(t_list) > 2 else None
    tp_final = t_list[-1]
    entry_out = round(sum(entries)/len(entries), 6) if entries else round(price, 6)

    mark_signal_now()

    return {
        "symbol": symbol,
        "side": "buy",
        "entry": entry_out,
        "entries": entries,
        "sl":    round(sl, 6),
        "targets": [round(x,6) for x in t_list],
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp3":   round(tp3, 6) if tp3 is not None else None,
        "tp_final": round(tp_final, 6),

        "atr":   round(atr, 6),
        "r":     round(entry_out - sl, 6),
        "score": int(score),
        "regime": regime,
        "reasons": reasons_full,
        "confluence": confluence,

        "features": {
            "rsi": float(closed["rsi"]),
            "rvol": rvol,
            "vol_spike_z20": float(df["vol_z20"].iloc[-2]),
            "atr_pct": atr_pct,
            "ema9": float(closed["ema9"]),
            "ema21": float(closed["ema21"]),
            "ema50": float(closed["ema50"]),
            "ema200": float(closed["ema200"]),
            "vwap": float(vwap_now) if USE_VWAP else None,
            "avwap": float(avwap_val) if (USE_ANCHORED_VWAP and avwap_val is not None) else None,
            "sup": float(sup) if sup is not None else None,
            "res": float(res) if res is not None else None,
            "setup": setup,
            "targets_display": targets_display,
            "score_breakdown": bd,
            "atr_band_dyn": {"lo": lo_dyn, "hi": hi_dyn},
            "relax_level": thr["RELAX_LEVEL"],
            "thresholds": {
                "SCORE_MIN": thr["SCORE_MIN"],
                "RVOL_MIN": thr["RVOL_MIN"],
                "ATR_BAND": thr["ATR_BAND"],
                "MIN_T1_ABOVE_ENTRY": MIN_T1_ABOVE_ENTRY,
                "HOLDOUT_BARS_EFF": holdout_eff,
                "EMA50_req_R": prof["ema50_req_R"],
                "EMA200_req_R": prof["ema200_req_R"],
                "VBR_MIN_DEV_ATR": prof["vbr_min_dev_atr"],
                "BRK_HOURS_RIYADH": (int(prof["brk_hour_start"]), int(prof["brk_hour_end"])),
                "MIN_QUOTE_VOL_EFF": prof["min_quote_vol"],
                "MAX_POS_FUNDING_EFF": prof["max_pos_funding"],
                "GUARD_REFS": prof.get("guard_refs"),
            },
        },

        "partials": partials,
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": TRAIL_AFTER_TP2_ATR if TRAIL_AFTER_TP2 else None,
        "max_bars_to_tp1": max_bars_to_tp1 if USE_MAX_BARS_TO_TP1 else None,

        "cooldown_after_sl_min": 15,
        "cooldown_after_tp_min": 5,

        "profile": RISK_MODE,
        "strategy_code": setup,
        "messages": messages,

        "stop_rule": stop_rule,
        "timestamp": datetime.utcnow().isoformat()
    }

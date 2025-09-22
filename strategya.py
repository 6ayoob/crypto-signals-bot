# -*- coding: utf-8 -*-
from __future__ import annotations
"""
strategy.py — Router (BRK/PULL/RANGE/SWEEP/VBR) + MTF + S/R + VWAP/AVWAP
Balanced+ v3.4 — نسخة مُراجَعة سطر-بسطر مع إصلاحات الخروج السريع وR والتنفيذ

هذا الملف مُقسم إلى 6 أجزاء. يحتوي هذا المستند على **الجزء 1/6 و 2/6**.
— تمت المراجعة على كودك الأصلي ودمج التحسينات التالية بدون كسر التوافق:
  • وقف SWEEP مُحمي (ATR + Swing + Buffer)
  • نافذة Soft‑Stop أولية (MAE‑based)
  • تقريب T1 في SWEEP/RANGE إلى ≥ 1.0R مع قصّ تحت المقاومة
  • حارس سبريد/انزلاق اختياري عبر features
  • تصحيح مخرجات R (r_unit/tp1_R/tp2_R) لاستخدامها في التنبيهات
  • عقوبة قرب المقاومة في السكور
  • بقاء بقية المنطق كما هو (QV/MTF/Relax/Breadth/Regime/Targets)
"""

# =========================
# Part 1/6 — Imports • Paths • Config • State • Relax/DSC • Symbols • Indicators • S/R helpers
# =========================

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os, json, math, time, csv
import pandas as pd
import numpy as np

# ========= مسارات =========
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = os.getenv("STRATEGY_STATE_FILE", str(APP_DATA_DIR / "strategy_state.json"))

# ========= إعدادات عامة =========
VOL_MA = 20
ATR_PERIOD = 14
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200

RISK_MODE = os.getenv("RISK_MODE", "balanced").lower()
RISK_PROFILES = {
    "conservative": {"SCORE_MIN": 78, "ATR_BAND": (0.0018, 0.015), "RVOL_MIN": 1.05, "TP_R": (1.0, 1.8, 3.0), "HOLDOUT_BARS": 3, "MTF_STRICT": True},
    "balanced":     {"SCORE_MIN": 72, "ATR_BAND": (0.0015, 0.020), "RVOL_MIN": 1.00, "TP_R": (1.0, 2.0, 3.5), "HOLDOUT_BARS": 2, "MTF_STRICT": True},
    "aggressive":   {"SCORE_MIN": 68, "ATR_BAND": (0.0012, 0.030), "RVOL_MIN": 0.95, "TP_R": (1.2, 2.4, 4.0), "HOLDOUT_BARS": 1, "MTF_STRICT": False},
}

# بروفايل خفيف "sniper"
RISK_PROFILES["sniper"] = {
    "SCORE_MIN": 69,
    "ATR_BAND": (0.0014, 0.0230),
    "RVOL_MIN": 0.95,
    "TP_R": (1.0, 2.0, 3.5),
    "HOLDOUT_BARS": 2,
    "MTF_STRICT": True,
}

_cfg = RISK_PROFILES.get(RISK_MODE, RISK_PROFILES["balanced"])

# الانتقائية الافتراضية
SELECTIVITY_MODE = os.getenv("SELECTIVITY_MODE", "soft").lower()  # soft|balanced|strict|auto
TARGET_SIGNALS_PER_DAY = float(os.getenv("TARGET_SIGNALS_PER_DAY", "3"))

USE_VWAP, USE_ANCHORED_VWAP = True, True
VWAP_TOL_BELOW, VWAP_MAX_DIST_PCT = 0.003, 0.010

USE_FUNDING_GUARD, USE_OI_TREND, USE_BREADTH_ADJUST = True, True, True
USE_PARABOLIC_GUARD, MAX_SEQ_BULL = True, 4
MAX_BRK_DIST_ATR, BREAKOUT_BUFFER = 0.90, 0.0015
RSI_EXHAUSTION, DIST_EMA50_EXHAUST_ATR = 80.0, 2.8

TRAIL_AFTER_TP2 = True
USE_MAX_BARS_TO_TP1, MAX_BARS_TO_TP1_BASE = True, 8
ENTRY_ZONE_WIDTH_R, ENTRY_MIN_PCT, ENTRY_MAX_R = 0.25, 0.005, 0.60
ENABLE_MULTI_TARGETS = True
TARGETS_MODE_BY_SETUP = {"BRK": "r", "PULL": "r", "RANGE": "pct", "SWEEP": "pct", "VBR":"pct"}
TARGETS_R5   = (1.0, 1.8, 3.0, 4.5, 6.0)
ATR_MULT_RANGE = (1.5, 2.5, 3.5, 4.5, 6.0)
ATR_MULT_VBR   = (0.0, 0.6, 1.2, 1.8, 2.4)

USE_SR, SR_WINDOW = True, 40
RES_BLOCK_NEAR, SUP_BLOCK_NEAR = 0.004, 0.003
USE_FIB, SWING_LOOKBACK, FIB_TOL = True, 60, 0.004

LOG_REJECTS = os.getenv("STRATEGY_LOG_REJECTS", "1").strip().lower() in ("1","true","yes","on")

# Relax أسرع: يبدأ 5h ويبلغ 10h
AUTO_RELAX_AFTER_HRS_1 = int(os.getenv("AUTO_RELAX_AFTER_HRS_1", "5"))
AUTO_RELAX_AFTER_HRS_2 = int(os.getenv("AUTO_RELAX_AFTER_HRS_2", "10"))
BREADTH_MIN_RATIO = float(os.getenv("BREADTH_MIN_RATIO", "0.60"))

MOTIVATION = {
    "entry": "🔥 دخول {symbol}! خطة أهداف على R — فلنلتزم 👊",
    "tp1":   "🎯 T1 تحقق على {symbol}! انقل SL للتعادل — استمر ✨",
    "tp2":   "🚀 T2 على {symbol}! فعّلنا التريلينغ — حماية المكسب 🛡️",
    "tp3":   "🏁 T3 على {symbol}! صفقة ممتازة 🌟",
    "tpX":   "🏁 هدف تحقق على {symbol}! استمرار ممتاز 🌟",
    "sl":    "🛑 SL على {symbol}. حماية رأس المال أولًا — فرص أقوى قادمة 🔄",
    "time":  "⌛ خروج زمني على {symbol} — الحركة لم تتفعّل سريعًا، خرجنا بخفّة 🔎",
}

# === باتشات الخروج السريع (Config) ===
SWEEP_STOP_K = float(os.getenv("SWEEP_STOP_K", "1.4"))           # 1.2..1.6
SWEEP_BUFFER_PCT = float(os.getenv("SWEEP_BUFFER_PCT", "0.0012")) # 12 bps
SOFT_STOP_SECONDS = int(os.getenv("SOFT_STOP_SECONDS", "120"))    # 60..180
SOFT_MAX_MAE_R    = float(os.getenv("SOFT_MAX_MAE_R", "1.25"))    # market exit if MAE>limit during soft window
SPREAD_MAX_BPS_MAJOR = int(os.getenv("SPREAD_MAX_BPS_MAJOR", "25"))   # 0.25%
SPREAD_MAX_BPS_ALT   = int(os.getenv("SPREAD_MAX_BPS_ALT", "35"))     # 0.35%
SLIPPAGE_MAX_BPS     = int(os.getenv("SLIPPAGE_MAX_BPS", "20"))
TRAIL_AFTER_TP1 = os.getenv("TRAIL_AFTER_TP1", "0").lower() in ("1","true","yes","on")
TRAIL_ATR_MULT_TP1 = float(os.getenv("TRAIL_ATR_MULT_TP1", "1.4"))

_LAST_ENTRY_BAR_TS: Dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: Dict[str, int] = {}

# ========= أدوات الحالة (State) =========

def _now() -> int:
    return int(time.time())


def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
            s.setdefault("relax_wins", 0)
            s.setdefault("last_signal_ts", 0)
            s.setdefault("signals_day_date", "")
            s.setdefault("signals_today", 0)
            s.setdefault("breadth_ema", None)
            s.setdefault("relax_last_update_ts", 0)
            s.setdefault("reject_counters", {})
            return s
    except Exception:
        return {
            "last_signal_ts": 0, "relax_wins": 0,
            "signals_day_date": "", "signals_today": 0,
            "breadth_ema": None, "relax_last_update_ts": 0,
            "reject_counters": {}
        }


def _save_state(s: dict):
    try:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def _reset_daily_counters(s: dict):
    try:
        day = datetime.utcnow().strftime("%Y-%m-%d")
        if s.get("signals_day_date") != day:
            s["signals_day_date"] = day
            s["signals_today"] = 0
            s["reject_counters"] = {}
    except Exception:
        pass


def mark_signal_now():
    s = _load_state(); _reset_daily_counters(s)
    s["last_signal_ts"] = _now()
    s["signals_today"] = int(s.get("signals_today", 0)) + 1
    _save_state(s)


def hours_since_last_signal() -> float:
    ts = _load_state().get("last_signal_ts", 0)
    if not ts: return 1e9
    return (_now() - int(ts)) / 3600.0


def relax_level() -> int:
    h = hours_since_last_signal()
    if h >= AUTO_RELAX_AFTER_HRS_2: return 2
    if h >= AUTO_RELAX_AFTER_HRS_1: return 1
    return 0


# ===== Relax المستمر + تنعيم Breadth =====

def _relax_factor_continuous(h: float, h_soft: float = 6.0, h_max: float = 12.0) -> float:
    """f∈[0..1]: 0 قبل h_soft، ثم يرتفع خطيًا حتى 1 عند h_max."""
    if h <= h_soft: return 0.0
    if h >= h_max: return 1.0
    return (h - h_soft) / max(h_max - h_soft, 1e-9)


def _breadth_smoothed(b_now: Optional[float]) -> Optional[float]:
    if b_now is None: return None
    s = _load_state()
    b_prev = s.get("breadth_ema", b_now)
    b_ema = 0.7 * (b_prev if b_prev is not None else b_now) + 0.3 * b_now
    s["breadth_ema"] = b_ema
    _save_state(s)
    return float(b_ema)


# ========= محوّل الانتقائية (DSC) =========

def _get_selectivity_mode(breadth_pct: Optional[float]) -> str:
    if SELECTIVITY_MODE in ("soft","balanced","strict"):
        return SELECTIVITY_MODE
    s = _load_state(); _reset_daily_counters(s)
    sigs = int(s.get("signals_today", 0))
    breadth_pct = 0.5 if breadth_pct is None else float(breadth_pct)
    if sigs < TARGET_SIGNALS_PER_DAY and breadth_pct >= 0.65:
        return "soft"
    if breadth_pct <= 0.40 or sigs >= TARGET_SIGNALS_PER_DAY * 1.5:
        return "strict"
    return "balanced"


def _apply_selectivity_mode(thr: dict, mode: str) -> dict:
    out = dict(thr)
    if mode == "soft":
        out["SCORE_MIN"] = max(60, out["SCORE_MIN"] - 4)
        out["RVOL_MIN"]  = max(0.80, out["RVOL_MIN"] - 0.03)
        lo, hi = out["ATR_BAND"]; out["ATR_BAND"] = (lo*0.95, hi*1.08)
    elif mode == "strict":
        out["SCORE_MIN"] = min(90, out["SCORE_MIN"] + 4)
        out["RVOL_MIN"]  = min(1.20, out["RVOL_MIN"] + 0.03)
        lo, hi = out["ATR_BAND"]; out["ATR_BAND"] = (lo*1.05, hi*0.95)
    out["SELECTIVITY_MODE"] = mode
    return out


def apply_relax(base_cfg: dict, breadth_hint: Optional[float] = None) -> dict:
    """يطبق: Relax تدريجي (continuous) + اختيار وضع الانتقائية تلقائيًا (DSC)."""
    out = dict(base_cfg)
    h = hours_since_last_signal()
    f = _relax_factor_continuous(h, AUTO_RELAX_AFTER_HRS_1, AUTO_RELAX_AFTER_HRS_2)  # f∈[0..1]

    out["SCORE_MIN"] = max(0, base_cfg["SCORE_MIN"] - int(round(8 * f)))        # حتى -8
    out["RVOL_MIN"]  = max(0.85, base_cfg["RVOL_MIN"] - 0.10 * f)               # حتى -0.10
    lo, hi = base_cfg["ATR_BAND"]
    out["ATR_BAND"] = (max(1e-5, lo*(1 - 0.15*f)), hi*(1 + 0.20*f))             # توسعة ناعمة
    out["MIN_T1_ABOVE_ENTRY"] = 0.010 - 0.004 * f                                # 0.010→0.006
    out["HOLDOUT_BARS_EFF"] = max(1, base_cfg.get("HOLDOUT_BARS", 2) - int(round(1*f)))

    out["RELAX_LEVEL"] = 1 if f > 0 else 0
    out["RELAX_F"] = f

    mode = _get_selectivity_mode(_breadth_smoothed(breadth_hint))
    out = _apply_selectivity_mode(out, mode)
    return out


# ========= إعادة ضبط التخفيف بعد صفقتين ناجحتين ==========

def register_trade_result(pnl_net: float, r_value: float | None = None):
    try:
        s = _load_state()
        now = _now()
        last_ts = int(s.get("relax_last_update_ts", 0))
        if last_ts and (now - last_ts) > 7*24*3600:
            s["relax_wins"] = 0
        min_win = 0.0
        if r_value is not None and r_value > 0:
            min_win = 0.3 * float(r_value)
        wins = int(s.get("relax_wins", 0))
        if float(pnl_net) >= float(min_win):
            wins += 1
        s["relax_wins"] = wins
        s["relax_last_update_ts"] = now
        if wins >= 2:
            s["relax_wins"] = 0
            s["last_signal_ts"] = now  # يرجع الوضع الطبيعي فورًا
        _save_state(s)
    except Exception:
        pass


# ========= تحميل إعدادات السِمبلز =========

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
                                        try: prof[key] = float(val)
                                        except Exception: prof[key] = val
                            _SYMBOL_PROFILES[sym] = prof
                    return
        except Exception as e:
            print("[symbols][warn]", e)


_load_symbols_file()


def get_symbol_profile(symbol: str) -> dict:
    s = (symbol or "").upper()
    s_flat = "".join(ch for ch in s if ch.isalnum())  # BTC/USDT → BTCUSDT
    prof = (_SYMBOL_PROFILES.get(s) or _SYMBOL_PROFILES.get(s_flat) or {}).copy()
    cls = (prof.get("class") or ("major" if s_flat in ("BTCUSDT","BTCUSD","ETHUSDT","ETHUSD") else "alt")).lower()
    is_major = (cls == "major")
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
    env_qv = os.getenv("MIN_BAR_QUOTE_VOL_USD")
    if env_qv:
        try: base["min_quote_vol"] = float(env_qv)
        except Exception: pass
    return base


# ========= مؤشرات فنية =========

def _trim(df: pd.DataFrame, n: int = 240) -> pd.DataFrame:
    return df.tail(n).copy()


def ema(series: pd.Series, period: int):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period=14):
    d = series.diff()
    gain = d.where(d > 0, 0.0); loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean().replace(0, 1e-9)
    rs = ag / al
    return 100 - (100 / (1 + rs))


def macd_cols(df: pd.DataFrame, fast=12, slow=26, signal=9):
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def atr_series(df: pd.DataFrame, period=ATR_PERIOD):
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


def add_indicators(df: pd.DataFrame):
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

def get_sr_on_closed(df: pd.DataFrame, window=SR_WINDOW) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < window + 3: return None, None
    hi = float(df.iloc[-(window+1):-1]["high"].max())
    lo = float(df.iloc[-(window+1):-1]["low"].min())
    if not math.isfinite(hi) or not math.isfinite(lo): return None, None
    return float(lo), float(hi)


def recent_swing(df: pd.DataFrame, lookback=SWING_LOOKBACK) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
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


def avwap_from_index(df: pd.DataFrame, idx: int) -> Optional[float]:
    if idx is None or idx < 0 or idx >= len(df)-1: return None
    sub = df.iloc[idx:].copy()
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3.0
    numer = (tp * sub["volume"]).cumsum()
    denom = (sub["volume"]).cumsum().replace(0, np.nan)
    v = numer / denom
    return float(v.iloc[-2]) if len(v) >= 2 and math.isfinite(v.iloc[-2]) else None


# ========= Helpers =========

def _pivot_highs(df: pd.DataFrame, left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    hs = []
    for i in range(left, len(df)-right):
        if df["high"].iloc[i] == df["high"].iloc[i-left:i+right+1].max():
            hs.append((i, float(df["high"].iloc[i])))
    return hs


def _pivot_lows(df: pd.DataFrame, left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    ls = []
    for i in range(left, len(df)-right):
        if df["low"].iloc[i] == df["low"].iloc[i-left:i+right+1].min():
            ls.append((i, float(df["low"].iloc[i])))
    return ls


def nearest_resistance_above(df: pd.DataFrame, price: float, lookback: int = 60) -> Optional[float]:
    piv = _pivot_highs(df.tail(lookback+5))
    above = [v for (_, v) in piv if v > price]
    return min(above) if above else None


# =========================
# Part 2/6 — QV Gate • Regime • QC • Price Action • ATR Dynamic • MTF • Market Guard
# =========================

# ========= حارس السيولة / QV v2 =========

def _compute_quote_vol_series(df: pd.DataFrame, contract_size: float = 1.0) -> pd.Series:
    return df["close"] * df["volume"] * float(contract_size)


def _dynamic_qv_threshold(symbol_min_qv: float, qv_hist: pd.Series, pct_of_median: float = 0.10) -> float:
    try:
        x = qv_hist.dropna().tail(240)
        med = float(x.median()) if len(x) else 0.0
    except Exception:
        med = 0.0
    dyn = max(symbol_min_qv, pct_of_median * med) if med > 0 else symbol_min_qv
    return float(dyn)


def _vwap_tol_pct(atr_pct: float, base_low: float = 0.0015, cap: float = 0.0040, is_major: bool = False) -> float:
    cap_eff = 0.0050 if is_major else cap
    return min(cap_eff, max(base_low, 0.60 * atr_pct))


def _qv_gate(
    qv_series: pd.Series,
    sym_min_qv: float,
    win: int = 10,
    low_vol_env: bool = False,
    is_major: bool = False,
    hr_riyadh: int | None = None
) -> tuple[bool, str]:
    if len(qv_series) < win:
        return False, "qv_window_short"

    # نافذة ديناميكية حسب الساعة
    if hr_riyadh is not None:
        if 1 <= hr_riyadh <= 8:
            win = max(win, 12)
        elif 9 <= hr_riyadh <= 12:
            win = max(win, 11)

    window = qv_series.tail(win)

    dyn_thr = _dynamic_qv_threshold(sym_min_qv, qv_series, pct_of_median=0.12)
    lvl = relax_level()
    if lvl == 1: dyn_thr *= 0.90
    elif lvl >= 2: dyn_thr *= 0.80
    if low_vol_env: dyn_thr *= 0.95

    if (not is_major) and hr_riyadh is not None and 1 <= hr_riyadh <= 8:
        dyn_thr *= 0.90

    qv_sum = float(window.sum())
    qv_min = float(window.min())

    minbar_req = max(700.0, 0.015 * dyn_thr)
    below = int((window < minbar_req).sum())
    soft_floor = 0.60 * minbar_req
    too_low = int((window < soft_floor).sum())

    if qv_sum >= 1.05 * dyn_thr and below <= 2 and too_low == 0:
        return True, f"sum={qv_sum:.0f}≥{1.10*dyn_thr:.0f} minbar_soft({below}<=2)"

    ok = (qv_sum >= dyn_thr) and (qv_min >= minbar_req)
    return ok, f"sum={qv_sum:.0f} thr={dyn_thr:.0f} minbar={qv_min:.0f}≥{minbar_req:.0f}"


# ========= نظام السوق (Regime) =========

def detect_regime(df: pd.DataFrame) -> str:
    c = df["close"]; e50 = df["ema50"]
    up = (c.iloc[-1] > e50.iloc[-1]) and (e50.diff(10).iloc[-1] > 0)
    if up:
        return "trend"
    seg = df.iloc[-80:]
    width = (seg["high"].max() - seg["low"].min()) / max(seg["close"].iloc[-1], 1e-9)
    atrp = float(seg["atr"].iloc[-2]) / max(seg["close"].iloc[-2], 1e-9)
    return "range" if width <= 6 * atrp else "mixed"


# ========= Quality Control =========

def bar_is_outlier(row: pd.Series, atr: float) -> bool:
    rng = float(row["high"]) - float(row["low"])
    if atr <= 0:
        return False
    return (rng > 6.0 * atr) or (float(row["volume"]) <= 0)


# ========= برايس أكشن خفيف =========

def candle_quality(row: pd.Series, rvol_hint: float | None = None) -> bool:
    o = float(row["open"]); c = float(row["close"]); h = float(row["high"]); l = float(row["low"])
    tr = max(h - l, 1e-9); body = abs(c - o); upper_wick = h - max(c, o)
    body_pct = body / tr; upwick_pct = upper_wick / tr
    min_body = 0.55 if (rvol_hint is None or rvol_hint < 1.3) else 0.45
    return (c > o) and (body_pct >= min_body) and (upwick_pct <= 0.35)


def is_bull_engulf(prev: pd.Series, cur: pd.Series) -> bool:
    return (float(cur["close"]) > float(cur["open"]) and
            float(prev["close"]) < float(prev["open"]) and
            (float(cur["close"]) - float(cur["open"])) > (abs(float(prev["close"]) - float(prev["open"])) * 0.9) and
            float(cur["close"]) >= float(prev["open"]))


def is_hammer(cur: pd.Series) -> bool:
    h = float(cur["high"]); l = float(cur["low"]); o = float(cur["open"]); c = float(cur["close"])
    tr = max(h - l, 1e-9); body = abs(c - o); lower_wick = min(o, c) - l
    return (c > o) and (lower_wick / tr >= 0.5) and (body / tr <= 0.35) and ((h - max(o, c)) / tr <= 0.15)


def is_inside_break(pprev: pd.Series, prev: pd.Series, cur: pd.Series) -> bool:
    cond_inside = (float(prev["high"]) <= float(pprev["high"]) and float(prev["low"]) >= float(pprev["low"]))
    return cond_inside and (float(cur["high"]) > float(prev["high"]) and float(cur["close"]) > float(prev["high"]))


def swept_liquidity(prev: pd.Series, cur: pd.Series) -> bool:
    return (float(cur["low"]) < float(prev["low"]) and (float(cur["close"]) > float(prev["close"]) ))


def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)


# ========= أدوات ATR ديناميكي =========

def _ema_smooth(s: pd.Series, span: int = 5) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def quantile_atr_band(atr_pct: pd.Series) -> tuple[float, float]:
    x = (atr_pct.dropna().clip(lower=0)).tail(200)
    if len(x) < 40:
        m = float(x.median()) if len(x) else 0.01
        return max(1e-5, m*0.6), m*1.6
    q25, q75 = float(x.quantile(0.25)), float(x.quantile(0.75))
    iqr = max(q75 - q25, 1e-6)
    lo = q25 - 0.25*iqr
    hi = q75 + 0.35*iqr
    if lo >= hi:
        lo, hi = q25*0.9, q75*1.1
    return max(1e-5, lo), max(hi, lo + 5e-5)


def adapt_atr_band(atr_pct_series: pd.Series, base_band: tuple[float, float]) -> tuple[float, float]:
    if atr_pct_series is None or len(atr_pct_series) < 40:
        return base_band
    sm = _ema_smooth(atr_pct_series.tail(240), span=5)
    q_lo, q_hi = quantile_atr_band(sm)
    lvl = relax_level()
    expand = 0.05 if lvl == 1 else (0.10 if lvl >= 2 else 0.0)
    lo = q_lo * (1 - expand)
    hi = q_hi * (1 + expand)
    return (max(1e-5, lo), max(hi, lo + 5e-5))


# ========= بوابة MTF خفيفة (H1/H4/D1) =========

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


def pass_mtf_filter_any(ohlcv_htf) -> tuple[bool, bool, bool, dict]:
    frames: Dict[str, pd.DataFrame] = {}
    def _mk(data):
        d = _df_from_ohlcv(data)
        return d if (d is not None and len(d) >= 60) else None

    if isinstance(ohlcv_htf, dict):
        for k in ("H1","H4","D1"):
            if ohlcv_htf.get(k):
                dd = _mk(ohlcv_htf[k])
                if dd is not None: frames[k] = dd
    elif isinstance(ohlcv_htf, list):
        dd = _mk(ohlcv_htf)
        if dd is not None: frames["H1"] = dd

    has = len(frames) > 0
    def _ok(dfh: pd.DataFrame) -> bool:
        c = dfh.iloc[-2]
        macd_ok = (float(c["macd_hist"]) > 0) or (float(dfh["macd_hist"].diff(3).iloc[-2]) > 0)
        return (float(c["close"]) > float(c["ema50"])) and (float(dfh["ema50"].diff(10).iloc[-2]) > 0) and macd_ok

    h1_ok = _ok(frames["H1"]) if "H1" in frames else False
    h4_ok = _ok(frames["H4"]) if "H4" in frames else False
    d1_ok = _ok(frames["D1"]) if "D1" in frames else True

    pass_h1h4 = (h1_ok and h4_ok) if ("H1" in frames and "H4" in frames) else (h1_ok or h4_ok)
    return has, pass_h1h4, d1_ok, {"h1": h1_ok, "h4": h4_ok, "d1": d1_ok}


# ========= حارس سوق ذكي (2 من 3) =========

def extract_features(ohlcv_htf) -> Dict[str, object]:
    out: Dict[str, object] = {}
    if isinstance(ohlcv_htf, dict):
        feats = ohlcv_htf.get("features") or {}
        if isinstance(feats, dict):
            out.update(feats)
    return out


def market_guard_ok(symbol_profile: dict, feats: dict) -> bool:
    refs = symbol_profile.get("guard_refs") or []
    breadth_ok = True

    # تصويت breadth المُنعّم
    try:
        s = _load_state()
        b_ema = s.get("breadth_ema")
        if b_ema is not None and float(b_ema) >= 0.65:
            breadth_ok = True
    except Exception:
        pass

    ms = feats.get("market_state")
    if isinstance(ms, dict) and refs:
        good = 0
        for r in refs:
            st = ms.get(r) or {}
            try:
                c, e = float(st.get("close", 0)), float(st.get("ema200", 0))
                rsi_h1 = float(st.get("rsi_h1", 50))
                if c > e and rsi_h1 >= 48: good += 1
            except Exception:
                pass
        breadth_ok = breadth_ok and (good >= max(1, int(len(refs)*0.5)))
    else:
        maj = feats.get("majors_state", [])
        if isinstance(maj, list) and maj:
            above = 0
            for x in maj:
                try:
                    c_, e_ = float(x.get("close", 0)), float(x.get("ema200", 0))
                    if c_ > e_: above += 1
                except Exception:
                    pass
            breadth_ok = breadth_ok and ((above / max(1, len(maj))) >= BREADTH_MIN_RATIO)

    # تمويل
    try:
        fr = feats.get("funding_rate")
        max_fr = float(symbol_profile.get("max_pos_funding", 0.00025))
        funding_ok = (fr is None) or (float(fr) <= max_fr)
    except Exception:
        funding_ok = True

    # اتجاه OI
    oi_ok = True
    try:
        oi_hist = feats.get("oi_hist")
        if isinstance(oi_hist, (list, tuple)) and len(oi_hist) >= int(symbol_profile.get("oi_window", 10)):
            s = pd.Series(oi_hist[-int(symbol_profile.get("oi_window", 10)):], dtype="float64")
            oi_sc = float((s.iloc[-1] - s.iloc[0]) / max(s.iloc[0], 1e-9))
            oi_ok = oi_sc >= float(symbol_profile.get("oi_down_thr", 0.0))
    except Exception:
        pass

    votes = sum([bool(breadth_ok), bool(funding_ok), bool(oi_ok)])
    return votes >= 2

# (يتبع في Part 3/6 — score_signal + check_signal (النصف الأول) …)
# === strategy.py (Balanced+ v3.4, محسّن) ===
# --- الجزء 3/6 ---

# ========= برايس أكشن =========
def candle_quality(row: pd.Series, rvol_hint: float | None = None) -> bool:
    o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
    tr = max(h - l, 1e-9); body = abs(c - o); upper_wick = h - max(c, o)
    body_pct = body / tr; upwick_pct = upper_wick / tr
    min_body = 0.55 if (rvol_hint is None or rvol_hint < 1.3) else 0.45
    return (c > o) and (body_pct >= min_body) and (upwick_pct <= 0.35)

def is_bull_engulf(prev: pd.Series, cur: pd.Series) -> bool:
    return (
        float(cur["close"]) > float(cur["open"])
        and float(prev["close"]) < float(prev["open"])
        and (float(cur["close"]) - float(cur["open"])) > (abs(float(prev["close"]) - float(prev["open"])) * 0.9)
        and float(cur["close"]) >= float(prev["open"])
    )

def is_hammer(cur: pd.Series) -> bool:
    h, l, o, c = float(cur["high"]), float(cur["low"]), float(cur["open"]), float(cur["close"])
    tr = max(h - l, 1e-9); body = abs(c - o); lower_wick = min(o, c) - l
    return (c > o) and (lower_wick / tr >= 0.5) and (body / tr <= 0.35) and ((h - max(o, c)) / tr <= 0.15)

def is_inside_break(pprev: pd.Series, prev: pd.Series, cur: pd.Series) -> bool:
    cond_inside = (float(prev["high"]) <= float(pprev["high"]) and float(prev["low"]) >= float(pprev["low"]))
    return cond_inside and (float(cur["high"]) > float(prev["high"])) and (float(cur["close"]) > float(prev["high"]))

def swept_liquidity(prev: pd.Series, cur: pd.Series) -> bool:
    return (float(cur["low"]) < float(prev["low"]) and (float(cur["close"]) > float(prev["close"])))

def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)

# ========= أدوات ATR ديناميكي =========
def _ema_smooth(s: pd.Series, span: int = 5) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def quantile_atr_band(atr_pct: pd.Series) -> tuple[float, float]:
    x = (atr_pct.dropna().clip(lower=0)).tail(200)
    if len(x) < 40:
        m = float(x.median()) if len(x) else 0.01
        return max(1e-5, m*0.6), m*1.6
    q25, q75 = float(x.quantile(0.25)), float(x.quantile(0.75))
    iqr = max(q75 - q25, 1e-6)
    lo = q25 - 0.25*iqr
    hi = q75 + 0.35*iqr
    if lo >= hi:
        lo, hi = q25*0.9, q75*1.1
    return max(1e-5, lo), max(hi, lo + 5e-5)

def adapt_atr_band(atr_pct_series: pd.Series, base_band: tuple[float, float]) -> tuple[float, float]:
    if atr_pct_series is None or len(atr_pct_series) < 40:
        return base_band
    sm = _ema_smooth(atr_pct_series.tail(240), span=5)
    q_lo, q_hi = quantile_atr_band(sm)
    lvl = relax_level()
    expand = 0.05 if lvl == 1 else (0.10 if lvl >= 2 else 0.0)
    lo = q_lo * (1 - expand)
    hi = q_hi * (1 + expand)
    return (max(1e-5, lo), max(hi, lo + 5e-5))

# --- Enhancements ---
SWEEP_STOP_K = float(os.getenv("SWEEP_STOP_K", "1.4"))
SWEEP_BUFFER_PCT = float(os.getenv("SWEEP_BUFFER_PCT", "0.0012"))
SOFT_STOP_SECONDS = int(os.getenv("SOFT_STOP_SECONDS", "120"))
SOFT_MAX_MAE_R = float(os.getenv("SOFT_MAX_MAE_R", "1.25"))
SPREAD_MAX_BPS_MAJOR = int(os.getenv("SPREAD_MAX_BPS_MAJOR", "25"))
SPREAD_MAX_BPS_ALT = int(os.getenv("SPREAD_MAX_BPS_ALT", "35"))
SLIPPAGE_MAX_BPS = int(os.getenv("SLIPPAGE_MAX_BPS", "20"))

# --- الجزء 4/6 ---

# ========= Score مع عقوبات ديناميكية =========
def score_signal(
    struct_ok: bool,
    rvol: float,
    atr_pct: float,
    ema_align: bool,
    mtf_pass: bool,
    srdist_R: float,
    mtf_has_frames: bool,
    rvol_min: float,
    atr_band: tuple[float, float],
    oi_trend: Optional[float] = None,
    breadth_pct: Optional[float] = None,
    avwap_confluence: Optional[bool] = None,
    d1_ok: bool = True,
    mtf_detail: Optional[dict] = None
) -> tuple[int, dict]:
    weights = {"struct":27, "rvol":13, "atr":13, "ema":13, "mtf":13, "srdist":8, "oi":6, "breadth":4, "avwap":3}
    score = 0.0; bd: Dict[str,float] = {}

    bd["struct"] = weights["struct"] if struct_ok else 0; score += bd["struct"]
    rvol_score = min(max((rvol - rvol_min) / max(0.5, rvol_min), 0), 1) * weights["rvol"]
    bd["rvol"] = rvol_score; score += bd["rvol"]

    lo, hi = atr_band; center = (lo+hi)/2
    if lo <= atr_pct <= hi:
        atr_score = (1 - abs(atr_pct - center)/max(center - lo,1e-9)) * weights["atr"]
        bd["atr"] = max(0, min(weights["atr"], atr_score)); score += bd["atr"]
    else:
        bd["atr"] = 0

    bd["ema"] = weights["ema"] if ema_align else 0; score += bd["ema"]
    bd["mtf"] = weights["mtf"] if (mtf_has_frames and mtf_pass) else 0; score += bd["mtf"]

    # عقوبة للـ MTF
    if not mtf_has_frames: score -= 6
    elif mtf_has_frames and not mtf_pass: score -= 8
    if mtf_has_frames and mtf_pass and not d1_ok: score -= 8

    bd["srdist"] = min(max(srdist_R,0)/1.5,1.0)*weights["srdist"]; score += bd["srdist"]
    if oi_trend is not None:
        bd["oi"] = max(min(oi_trend,0.2),-0.2)/0.2*weights["oi"]; score += bd["oi"]
    if breadth_pct is not None:
        bd["breadth"] = ((breadth_pct-0.5)*2.0)*weights["breadth"]; score += bd["breadth"]
    if avwap_confluence: bd["avwap"] = weights["avwap"]; score += bd["avwap"]

    # عقوبة قرب المقاومة
    if srdist_R < 0.8: score -= 4
    elif srdist_R < 1.0: score -= 2

    return int(round(score)), bd
# === strategy.py (Balanced+ v3.4, محسّن) ===
# --- الجزء 5/6 ---

# ========= وقف محمي لـ SWEEP =========
def _compute_protected_sl(df_: pd.DataFrame, entry_price: float, atr_: float, setup_: str) -> float:
    atr_leg = atr_ * (SWEEP_STOP_K if setup_ == "SWEEP" else 0.9)
    try:
        swing_low = float(df_.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
    except Exception:
        swing_low = entry_price
    struct_leg = swing_low - max(entry_price * SWEEP_BUFFER_PCT, 1e-6)
    base_sl = entry_price - max(atr_leg, entry_price * 0.002)
    sl_val = min(base_sl, struct_leg) if setup_ == "SWEEP" else base_sl
    return float(sl_val)

# ========= ضبط T1 تحت المقاومة =========
def _clamp_t1_below_res(price: float, t1: float, res: Optional[float], buf_pct: float = 0.0015) -> tuple[float, bool]:
    if res is None: return t1, False
    if t1 >= res * (1 - buf_pct):
        return res * (1 - buf_pct), True
    return t1, False

# ========= Payload =========
def build_payload(symbol: str, setup: str, price: float, sl: float, t_list: list[float], score: int, regime: str) -> dict:
    R_unit = max(price - sl, 1e-9)
    tp1_R  = (t_list[0] - price) / R_unit if len(t_list) > 0 else None
    tp2_R  = (t_list[1] - price) / R_unit if len(t_list) > 1 else None

    return {
        "symbol": symbol,
        "setup": setup,
        "price": price,
        "sl": round(sl, 6),
        "targets": [round(x, 6) for x in t_list],
        "score": score,
        "regime": regime,
        "soft_stop": {"enabled": True, "seconds": SOFT_STOP_SECONDS, "max_mae_R": SOFT_MAX_MAE_R},
        "r_unit": round(R_unit, 8),
        "tp1_R": round(tp1_R, 3) if tp1_R else None,
        "tp2_R": round(tp2_R, 3) if tp2_R else None,
        "trail_after_tp1": TRAIL_AFTER_TP1,
        "trail_atr_mult_tp1": TRAIL_ATR_MULT_TP1 if TRAIL_AFTER_TP1 else None,
    }

# --- الجزء 6/6 ---

# ========= سجل الرفض =========
def _log_reject(symbol: str, msg: str):
    if LOG_REJECTS:
        print(f"[strategy][reject] {symbol}: {msg}")

# ========= Strategy Wrapper =========
def strategy_entry(symbol: str, ohlcv: list[list], ohlcv_htf: Optional[object] = None) -> Optional[dict]:
    try:
        sig = check_signal(symbol, ohlcv, ohlcv_htf)
        if not sig:
            return None
        mark_signal_now()
        return sig
    except Exception as e:
        print(f"[strategy][error] {symbol}: {e}")
        return None

# ========= نهاية الملف =========

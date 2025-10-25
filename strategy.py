# -*- coding: utf-8 -*-
from __future__ import annotations
"""
strategy.py — Router (BRK/PULL/RANGE/SWEEP/VBR) + MTF + S/R + VWAP/AVWAP
Balanced+ v3.4 — نسخة مُراجَعة سطر-بسطر مع إصلاحات الخروج السريع وR والتنفيذ
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os, json, math, time, csv
import pandas as pd
import numpy as np

# ---- Optional OKX fetch hook (safe if missing) ----
try:
    from okx_api import fetch_ohlcv as _okx_fetch_ohlcv  # متوفر في بعض المشاريع
except Exception:
    _okx_fetch_ohlcv = None  # غير متوفر → سنعمل بالمدخلات فقط

# ✅ غلاف عام ليستعمله bot.py عبر: from strategy import fetch_ohlcv
def fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200):
    if _okx_fetch_ohlcv is None:
        # سيقع bot.py في except ويسجّل التحذير بدلاً من التعطّل
        raise RuntimeError("okx_api.fetch_ohlcv is not available in this deployment")
    return _okx_fetch_ohlcv(symbol, timeframe, limit)

# ✅ نسخة موحَّدة من _ensure_data (واحدة فقط)
def _ensure_data(symbol: str, ohlcv: Optional[list], ohlcv_htf: Optional[object]):
    """
    يُستخدم فقط إذا أرسلت None إلى check_signal/strategy_entry.
    لا يعمل أي استدعاء خارجي إن لم تتوفر okx_api.
    """
    if (ohlcv is None or len(ohlcv) < 80) and _okx_fetch_ohlcv:
        ohlcv = _okx_fetch_ohlcv(symbol, LTF_TF, 200)
    if (ohlcv_htf is None) and _okx_fetch_ohlcv:
        ohlcv_htf = {
            "H1": _okx_fetch_ohlcv(symbol, "1h", 200),
            "H4": _okx_fetch_ohlcv(symbol, "4h", 200),
            "D1": _okx_fetch_ohlcv(symbol, "1d", 200),
        }
    return ohlcv, ohlcv_htf

# ========= مسارات =========
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp/market-watchdog")).resolve()
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = os.getenv("STRATEGY_STATE_FILE", str(APP_DATA_DIR / "strategy_state.json"))

# ========= إعدادات عامة =========
SILENCE_SOFTEN_HOURS = int(os.getenv("SILENCE_SOFTEN_HOURS", "9"))

VOL_MA = 20
ATR_PERIOD = 14
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200

RISK_MODE = os.getenv("RISK_MODE", "balanced").lower()
RISK_PROFILES = {
    "conservative": {"SCORE_MIN": 78, "ATR_BAND": (0.0018, 0.015), "RVOL_MIN": 1.05, "TP_R": (1.0, 1.8, 3.0), "HOLDOUT_BARS": 3, "MTF_STRICT": True},
    "balanced":     {"SCORE_MIN": 72, "ATR_BAND": (0.0015, 0.020), "RVOL_MIN": 1.00, "TP_R": (1.0, 2.0, 3.5), "HOLDOUT_BARS": 2, "MTF_STRICT": True},
    "aggressive":   {"SCORE_MIN": 68, "ATR_BAND": (0.0012, 0.030), "RVOL_MIN": 0.95, "TP_R": (1.2, 2.4, 4.0), "HOLDOUT_BARS": 1, "MTF_STRICT": False},
}
RISK_PROFILES["sniper"] = {"SCORE_MIN": 69, "ATR_BAND": (0.0014, 0.0230), "RVOL_MIN": 0.95, "TP_R": (1.0, 2.0, 3.5), "HOLDOUT_BARS": 2, "MTF_STRICT": True}
_cfg = RISK_PROFILES.get(RISK_MODE, RISK_PROFILES["balanced"])

SELECTIVITY_MODE = os.getenv("SELECTIVITY_MODE", "soft").lower()  # soft|balanced|strict|auto
TARGET_SIGNALS_PER_DAY = float(os.getenv("TARGET_SIGNALS_PER_DAY", "3"))

# --- Soften thresholds (env-tunable) ---
RVOL_MIN_FLOOR   = float(os.getenv("RVOL_MIN_FLOOR", "0.70"))   # لا نطلب RVOL أعلى من 0.70
QV_THR_SCALE     = float(os.getenv("QV_THR_SCALE", "0.50"))     # خفّض عتبة السيولة للنصف
ATR_WIDEN_LO     = float(os.getenv("ATR_WIDEN_LO", "0.90"))     # وسّع ATR لأسفل 10%
ATR_WIDEN_HI     = float(os.getenv("ATR_WIDEN_HI", "1.12"))     # … ولأعلى 12%
MIN_T1_GAP_FLOOR = float(os.getenv("MIN_T1_GAP_FLOOR", "0.003"))# 0.3% أرضية للفجوة

USE_FUNDING_GUARD, USE_OI_TREND, USE_BREADTH_ADJUST = True, True, True
USE_PARABOLIC_GUARD, MAX_SEQ_BULL = True, 4
MAX_BRK_DIST_ATR, BREAKOUT_BUFFER = 0.90, 0.0015
RSI_EXHAUSTION, DIST_EMA50_EXHAUST_ATR = 80.0, 2.8

# (أُزيل التعريف القديم الثابت لـ TRAIL_AFTER_TP2 من هنا)

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

_LAST_ENTRY_BAR_TS: Dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: Dict[str, int] = {}

# ---- ENV helpers & flags (مرنة مع أسماء مختلفة في اللوحة) ----
def _as_bool(name, default="1"):
    try:
        return os.getenv(name, default).strip().lower() in ("1","true","yes","on")
    except Exception:
        return default.strip().lower() in ("1","true","yes","on")

# لوحّدنا اسم الإطار الزمني بين LTF_TF و TIMEFRAME
LTF_TF = os.getenv("LTF_TF") or os.getenv("TIMEFRAME", "15m")

# تفعيل VWAP/AVWAP من الـENV بدل التثبيت الصلب
USE_VWAP = _as_bool("USE_VWAP", "1")                # 1=تشغيل (افتراضي)
USE_ANCHORED_VWAP = _as_bool("USE_ANCHORED_VWAP", "1")

# السماح باسمين المختلفين للانزلاق/السبريد (BP/BPS)
SLIPPAGE_MAX_BPS = int(os.getenv("SLIPPAGE_MAX_BPS") or os.getenv("SLIPPAGE_MAX_BP", "20"))
SPREAD_MAX_BPS_MAJOR = int(os.getenv("SPREAD_MAX_BPS_MAJOR") or os.getenv("SPREAD_MAX_BP", "25"))
SPREAD_MAX_BPS_ALT   = int(os.getenv("SPREAD_MAX_BPS_ALT")   or os.getenv("SPREAD_MAX_BP", "35"))

# باقي الثوابت كما هي:
VWAP_TOL_BELOW, VWAP_MAX_DIST_PCT = 0.003, 0.010

# ==== Trailing settings (موحّدة) ====
# تفعيل/تعطيل التريلينغ بعد T2 من متغير بيئة (افتراضي: يعمل)
TRAIL_AFTER_TP2 = _as_bool("TRAIL_AFTER_TP2", "1")
# يمكن ضبط مضاعف ATR لاحقًا عبر البيئة (TRAIL_ATR_MULT_TP2)، وإلا نستخدم الذكي حسب السكور.

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
    if sigs < TARGET_SIGNALS_PER_DAY and breadth_pct >= 0.65: return "soft"
    if breadth_pct <= 0.40 or sigs >= TARGET_SIGNALS_PER_DAY * 1.5: return "strict"
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
    out = dict(base_cfg)
    h = hours_since_last_signal()
    f = _relax_factor_continuous(h, AUTO_RELAX_AFTER_HRS_1, AUTO_RELAX_AFTER_HRS_2)
    out["SCORE_MIN"] = max(0, base_cfg["SCORE_MIN"] - int(round(8 * f)))
    out["RVOL_MIN"]  = max(0.85, base_cfg["RVOL_MIN"] - 0.10 * f)
    lo, hi = base_cfg["ATR_BAND"]
    out["ATR_BAND"] = (max(1e-5, lo*(1 - 0.15*f)), hi*(1 + 0.20*f))
    out["MIN_T1_ABOVE_ENTRY"] = 0.010 - 0.004 * f
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
            s["last_signal_ts"] = now
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
        base = {"atr_lo": 0.0020, "atr_hi": 0.0240, "rvol_min": 1.00, "min_quote_vol": 20000,
                "ema50_req_R": 0.25, "ema200_req_R": 0.35, "vbr_min_dev_atr": 0.6,
                "oi_window": 10, "oi_down_thr": 0.0,
                "max_pos_funding": float(os.getenv("MAX_POS_FUNDING_MAJ", "0.00025")),
                "brk_hour_start": int(os.getenv("BRK_HOUR_START","11")), "brk_hour_end": int(os.getenv("BRK_HOUR_END","23")),
                "guard_refs": prof.get("guard_refs") or ["BTCUSDT","ETHUSDT"], "class": "major"}
    else:
        base = {"atr_lo": 0.0025, "atr_hi": 0.0300, "rvol_min": 1.05, "min_quote_vol": 100000,
                "ema50_req_R": 0.30, "ema200_req_R": 0.45, "vbr_min_dev_atr": 0.7,
                "oi_window": 14, "oi_down_thr": -0.03,
                "max_pos_funding": float(os.getenv("MAX_POS_FUNDING_ALT", "0.00025")),
                "brk_hour_start": int(os.getenv("BRK_HOUR_START","11")), "brk_hour_end": int(os.getenv("BRK_HOUR_END","23")),
                "guard_refs": prof.get("guard_refs") or ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"], "class": "alt"}
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

def _clamp_t1_below_res(entry_price: float, t1: float, res_level: float | None, buf_pct: float = 0.0015):
    """
    يعيد T1 مقصوصًا تحت المقاومة (إن وُجدت) بهامش أمان buf_pct.
    يرجع (t1_new, was_clamped: bool)
    """
    if res_level is None or res_level <= 0:
        return float(t1), False
    max_t1 = res_level * (1.0 - buf_pct)
    if max_t1 <= entry_price:   # المقاومة قريبة جدًا
        return float(t1), False  # اتركها كما هي ليمر شرط الرفض لاحقًا
    if t1 > max_t1:
        return float(max_t1), True
    return float(t1), False

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
    cap_eff = 0.0060 if is_major else 0.0050
    return min(cap_eff, max(base_low, 0.80 * atr_pct))

def _qv_gate(qv_series: pd.Series, sym_min_qv: float, win: int = 10, low_vol_env: bool = False, is_major: bool = False, hr_riyadh: int | None = None) -> tuple[bool, str]:
    if len(qv_series) < win:
        return False, "qv_window_short"
    if hr_riyadh is not None:
        if 1 <= hr_riyadh <= 8:   win = max(win, 12)
        elif 9 <= hr_riyadh <= 12: win = max(win, 11)

    window = qv_series.tail(win)
    dyn_thr = _dynamic_qv_threshold(sym_min_qv, qv_series, pct_of_median=0.12)

    lvl = relax_level()
    if lvl == 1: dyn_thr *= 0.92
    elif lvl >= 2: dyn_thr *= 0.84

    hours_silence = hours_since_last_signal()
    if (not is_major) and hr_riyadh is not None and 1 <= hr_riyadh <= 8: dyn_thr *= 0.90
    if hours_silence >= SILENCE_SOFTEN_HOURS: dyn_thr *= 0.92
    if hours_silence >= SILENCE_SOFTEN_HOURS + 6: dyn_thr *= 0.88

    qv_sum = float(window.sum())
    qv_min = float(window.min())

    minbar_req = max(600.0, 0.012 * dyn_thr)
    if lvl >= 1: minbar_req *= 0.92
    if lvl >= 2: minbar_req *= 0.86
    if low_vol_env: minbar_req *= 0.96

    below = int((window < minbar_req).sum())
    soft_floor = 0.60 * minbar_req
    too_low = int((window < soft_floor).sum())

    if qv_sum >= 1.04 * dyn_thr and below <= 2 and too_low == 0:
        return True, f"sum={qv_sum:.0f}≥{1.04*dyn_thr:.0f} minbar_ok"

    ok = (qv_sum >= dyn_thr) and (qv_min >= minbar_req)
    if ok:
        return True, f"sum={qv_sum:.0f}≥{dyn_thr:.0f} minbar={qv_min:.0f}≥{minbar_req:.0f}"
    parts = []
    if qv_sum < dyn_thr:
        parts.append(f"sum={qv_sum:.0f}<{dyn_thr:.0f}")
    else:
        parts.append(f"sum={qv_sum:.0f}≥{dyn_thr:.0f}")
    if qv_min < minbar_req:
        parts.append(f"minbar={qv_min:.0f}<{minbar_req:.0f}")
    else:
        parts.append(f"minbar={qv_min:.0f}≥{minbar_req:.0f}")
    return False, " ".join(parts)

# ========= نظام السوق (Regime) =========
def detect_regime(df: pd.DataFrame) -> str:
    c = df["close"]; e50 = df["ema50"]
    up = (c.iloc[-1] > e50.iloc[-1]) and (e50.diff(10).iloc[-1] > 0)
    if up: return "trend"
    seg = df.iloc[-80:]
    width = (seg["high"].max() - seg["low"].min()) / max(seg["close"].iloc[-1], 1e-9)
    atrp = float(seg["atr"].iloc[-2]) / max(seg["close"].iloc[-2], 1e-9)
    return "range" if width <= 6 * atrp else "mixed"

# ========= Quality Control =========
def bar_is_outlier(row: pd.Series, atr: float) -> bool:
    rng = float(row["high"]) - float(row["low"])
    if atr <= 0: return False
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
    ATR_EXTRA_EXPAND = float(os.getenv("ATR_EXTRA_EXPAND", "0.02"))  # افتراضي +2%
    expand = (0.05 if lvl == 1 else (0.10 if lvl >= 2 else 0.0)) + ATR_EXTRA_EXPAND
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

# ========= سجل الرفض =========
def _log_reject(symbol: str, msg: str):
    if LOG_REJECTS:
        print(f"[strategy][reject] {symbol}: {msg}")
    # سجّل السبب يوميًا في STATE_FILE
    try:
        s = _load_state()
        _reset_daily_counters(s)
        rc = s.get("reject_counters", {})
        rc[msg] = int(rc.get(msg, 0)) + 1
        s["reject_counters"] = rc
        s["last_reject_ts"] = _now()
        _save_state(s)
    except Exception:
        pass

# ========= المولّد الرئيسي للإشارة (Merged+) =========
def check_signal(
    symbol: str,
    ohlcv: list[list],
    ohlcv_htf: Optional[object] = None
) -> Optional[dict]:
    import math  # للتأكد موجود
    # اجلب/أكمل البيانات إن احتجنا (بدون الاعتماد الإجباري على okx_api)
    ohlcv, ohlcv_htf = _ensure_data(symbol, ohlcv, ohlcv_htf)

    # تحقق بيانات
    if not ohlcv or len(ohlcv) < 80:
        _log_reject(symbol, "insufficient_bars")
        return None

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
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

    prev2 = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price = float(closed["close"])

    # QC & Parabolic guard
    atr = float(df["atr"].iloc[-2])
    atr_pct = atr / max(price, 1e-9)
    if bar_is_outlier(closed, atr):
        _log_reject(symbol, "bar_outlier")
        return None
    try:
        macd_slope_3 = float(df["macd_hist"].diff(3).iloc[-2])
    except Exception:
        macd_slope_3 = 0.0
    if USE_PARABOLIC_GUARD and atr_pct > 0.020 and macd_slope_3 < 0:
        _log_reject(symbol, "parabolic_macd_cooling")
        return None

    # بروفايل + نظام + MTF + ميزات
    prof = get_symbol_profile(symbol)
    regime = detect_regime(df)
    mtf_has_frames, mtf_pass, d1_ok, mtf_detail = pass_mtf_filter_any(ohlcv_htf)
    feats = extract_features(ohlcv_htf)

    # Breadth hint من majors_state
    breadth_pct = None
    majors_state = feats.get("majors_state", [])
    try:
        if isinstance(majors_state, list) and majors_state:
            above = 0
            for x in majors_state:
                c_ = float(x.get("close", 0))
                e_ = float(x.get("ema200", 0))
                if c_ > 0 and e_ > 0 and c_ > e_:
                    above += 1
            breadth_pct = above / max(1, len(majors_state))
    except Exception:
        breadth_pct = None

    # قواعد Relax + DSC
    base_cfg = dict(_cfg)
    base_cfg["ATR_BAND"] = (prof["atr_lo"], prof["atr_hi"])
    base_cfg["RVOL_MIN"] = max(base_cfg.get("RVOL_MIN", 1.0), float(prof["rvol_min"]))
    thr = apply_relax(base_cfg, breadth_hint=breadth_pct)

    MIN_T1_ABOVE_ENTRY = thr.get("MIN_T1_ABOVE_ENTRY", 0.010)
    holdout_eff = thr.get("HOLDOUT_BARS_EFF", base_cfg.get("HOLDOUT_BARS", 2))

    # ضبط RVOL_MIN وفق breadth/relax + أرضية لكل فئة
    if breadth_pct is not None:
        if breadth_pct >= 0.70:
            thr["RVOL_MIN"] = max(0.75, float(thr.get("RVOL_MIN", 1.0)) - 0.08)
        elif breadth_pct <= 0.35:
            thr["RVOL_MIN"] = min(1.25, float(thr["RVOL_MIN"]) + 0.03)
    if thr.get("RELAX_LEVEL", 0) >= 1:
        thr["RVOL_MIN"] = max(0.72 if prof.get("class") != "major" else 0.85, float(thr["RVOL_MIN"]) - 0.03)

    # منع التكرار + Holdout (أخف للميجرز)
    base_sym = symbol.split("#")[0]
    if _LAST_ENTRY_BAR_TS.get(base_sym) == cur_ts:
        _log_reject(symbol, "duplicate_symbol_bar")
        return None
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts:
        _log_reject(symbol, "duplicate_bar")
        return None
    cur_idx = len(df) - 2
    is_major = (prof.get("class") == "major")
    if is_major:
        holdout_eff = max(1, int(holdout_eff) - 1)
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < holdout_eff:
        _log_reject(symbol, f"holdout<{holdout_eff}")
        return None

    # سيولة — QV Gate
    qv_series = _compute_quote_vol_series(df, contract_size=1.0)
    low_vol_env = (atr_pct <= 0.006)
    try:
        ts_sec = (cur_ts / 1000.0) if cur_ts > 1e12 else float(cur_ts)
        hr_riyadh = (datetime.utcfromtimestamp(ts_sec).hour + 3) % 24
    except Exception:
        hr_riyadh = 12
    ok_qv, qv_dbg = _qv_gate(
        qv_series,
        float(prof["min_quote_vol"]),
        win=10,
        low_vol_env=low_vol_env,
        is_major=is_major,
        hr_riyadh=hr_riyadh,
    )
    if not ok_qv:
        _log_reject(symbol, f"low_quote_vol ({qv_dbg})")
        return None

    # نطاق ATR ديناميكي (مع تليين)
    base_lo, base_hi = thr["ATR_BAND"]
    atr_pct_series = (df["atr"] / df["close"]).dropna()
    lo_dyn, hi_dyn = adapt_atr_band(atr_pct_series, (base_lo, base_hi))

    if mtf_has_frames and not d1_ok:
        lo_dyn *= 0.95
        hi_dyn *= 1.07

    # ==== FIX (step 4): توسيع/تحقق نطاق ATR بأمان ====
    eps_abs = 0.00018
    ATR_EPS_REL_ADD = float(os.getenv("ATR_EPS_REL_ADD", "0.02"))
    eps_rel = 0.05 + ATR_EPS_REL_ADD

    lo_eff = float(lo_dyn)
    hi_eff = float(hi_dyn)

    lo_eff = max(lo_eff - eps_abs, lo_eff * (1 - eps_rel))
    hi_eff = min(hi_eff + eps_abs, hi_eff * (1 + eps_rel))

    if is_major:
        hi_eff *= 1.08
    if regime == "trend":
        hi_eff *= (1.08 if is_major else 1.05)
    elif regime == "range":
        lo_eff *= 0.94
    if hours_since_last_signal() >= SILENCE_SOFTEN_HOURS:
        lo_eff *= 0.98
        hi_eff *= 1.02

    try:
        if not (math.isfinite(lo_eff) and math.isfinite(hi_eff) and lo_eff > 0 and hi_eff > 0 and hi_eff > lo_eff):
            _log_reject(symbol, "atr_band_invalid")
            return None
    except Exception:
        _log_reject(symbol, "atr_band_invalid")
        return None

    if not (lo_eff <= atr_pct <= hi_eff):
        _log_reject(symbol, f"atr_pct_outside[{atr_pct:.4f}] not in [{lo_eff:.4f},{hi_eff:.4f}]")
        return None
    # ==== END FIX ====

    # RVول & Spike
    v_med60 = float(df["volume"].iloc[-61:-1].median()) if len(df) >= 61 else float(closed.get("vol_ma20") or 1e-9)
    base_vol = v_med60 if v_med60 > 0 else (float(closed.get("vol_ma20") or 1e-9))
    rvol = float(closed["volume"]) / max(base_vol, 1e-9)
    z20 = float(df["vol_z20"].iloc[-2])
    spike_z = 1.2 - min(0.3, (atr_pct / 0.02) * 0.2)
    vol_ema5 = df["volume"].ewm(span=5, adjust=False).mean().iloc[-2]
    vol_ema20 = df["volume"].ewm(span=20, adjust=False).mean().iloc[-2]
    accel_vol = (vol_ema5 > vol_ema20 * 1.05)
    spike_ok = (z20 >= (spike_z - 0.15))
    if hours_since_last_signal() >= SILENCE_SOFTEN_HOURS:
        thr["RVOL_MIN"] = max(0.80 if is_major else 0.70, float(thr["RVOL_MIN"]) - 0.05)
        spike_z -= 0.10
        spike_ok = (z20 >= (spike_z - 0.15))
    if rvol < thr["RVOL_MIN"] and not spike_ok:
        if not (accel_vol and z20 >= (spike_z - 0.35)):
            _log_reject(symbol, f"rvol<{thr['RVOL_MIN']:.2f} and no spike/accel (rv={rvol:.2f}, z={z20:.2f})")
            return None

    # حارس السوق
    if not market_guard_ok(prof, feats):
        _log_reject(symbol, "market_guard_block")
        return None

    # اتجاه/VWAP/AVWAP
    vwap_now = float(closed["vwap"]) if "vwap" in closed else float(df["vwap"].iloc[-2])
    vw_tol = _vwap_tol_pct(atr_pct, is_major=is_major)
    ema50_slope_pos = float(df["ema50"].diff(10).iloc[-2]) > 0
    macd_pos = float(df["macd_hist"].iloc[-2]) > 0
    price_above_ema50 = price > float(closed["ema50"])
    two_of_three = sum([ema50_slope_pos, macd_pos, price_above_ema50]) >= 2

    # AVWAPs
    avwap_swing_low = avwap_swing_high = avwap_day = None
    hhv, llv, hi_idx, lo_idx = recent_swing(df, SWING_LOOKBACK)
    if lo_idx is not None:
        avwap_swing_low = avwap_from_index(df, lo_idx)
    if hi_idx is not None:
        avwap_swing_high = avwap_from_index(df, hi_idx)
    try:
        ts = pd.to_datetime(
            df["timestamp"],
            unit="ms" if df["timestamp"].iloc[-2] > 1e12 else "s",
            utc=True,
        )
        last_day = ts.dt.date.iloc[-2]
        day_start_idx = ts[ts.dt.date == last_day].index[0]
        avwap_day = avwap_from_index(df, int(day_start_idx))
    except Exception:
        avwap_day = None

    # لا تُحسب كونفلونس إذا AVWAP مفقود
    def _above(x: Optional[float], tol: float = vw_tol) -> bool:
        return (x is not None) and (price >= x * (1 - tol))

    av_list = [avwap_swing_low, avwap_swing_high, avwap_day]
    av_ok_count = sum(1 for v in av_list if _above(v))
    avwap_confluence_ok = (av_ok_count >= 1)

    above_vwap = (price >= vwap_now * (1 - vw_tol))
    ema_align = two_of_three and above_vwap and (avwap_confluence_ok or not USE_ANCHORED_VWAP)
    if regime == "range" and not ema_align:
        near_vwap_soft = (price >= vwap_now * (1 - vw_tol * 1.35))
        two_of_three_soft = sum([price > float(closed["ema21"]), macd_pos, near_vwap_soft]) >= 2
        if (two_of_three_soft and (av_ok_count>=1 or bool(df["nr7"].iloc[-2] or df["nr4"].iloc[-2]))):
            ema_align = True

    # شرط الإغلاق فوق الافتتاح (مع استثناء pin-hammer الأحمر)
    if not (price > float(closed["open"])):
        allow_red_pin = False
        try:
            h, l, o, c = float(closed["high"]), float(closed["low"]), float(closed["open"]), float(closed["close"])
            tr = max(h - l, 1e-9)
            body = abs(c - o)
            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)
            if (body / tr <= 0.25) and (lower_wick / tr >= 0.45) and ((c - l) / tr >= 0.55) and (upper_wick / tr <= 0.20):
                allow_red_pin = True
        except Exception:
            allow_red_pin = False
        if not allow_red_pin:
            _log_reject(symbol, "close<=open")
            return None

    # تليين فشل EMA/VWAP/AVWAP في وضع soft بدل الرفض الفوري
    ema_align_final = ema_align
    soft_ema_penalty = 0
    if not ema_align:
        mode = thr.get("SELECTIVITY_MODE", "balanced")
        ema_align_soft_ok = (two_of_three or above_vwap) and (av_ok_count >= 1 or regime != "trend")
        if mode == "soft" and ema_align_soft_ok:
            ema_align_final = False
            soft_ema_penalty = 5
        else:
            _log_reject(symbol, "ema/vwap/avwap_align_false")
            return None

    # S/R + برايس أكشن
    sup = res = None
    if USE_SR:
        sup, res = get_sr_on_closed(df, SR_WINDOW)
    pivot_res = nearest_resistance_above(df, price, lookback=SR_WINDOW)
    res_eff = min(x for x in [res, pivot_res] if x is not None) if (res is not None or pivot_res is not None) else None

    rev_hammer = is_hammer(closed)
    rev_engulf = is_bull_engulf(prev, closed)
    rev_insideb = is_inside_break(df.iloc[-5] if len(df) >= 5 else prev2, prev, closed)
    had_sweep = swept_liquidity(prev, closed)
    near_res = near_level(price, res_eff, RES_BLOCK_NEAR)
    near_sup = near_level(price, sup, SUP_BLOCK_NEAR)

    try:
        hhv_prev = float(df.iloc[-(SR_WINDOW + 1):-1]["high"].max())
    except Exception:
        hhv_prev = float(prev["high"])
    breakout_ok = price > hhv_prev * (1.0 + BREAKOUT_BUFFER)

    prev_l = float(prev["low"])
    prev2_l = float(prev2["low"])
    retest_band_hi = hhv_prev * (1.0 + 0.0008)
    retest_band_lo = hhv_prev * (1.0 - 0.0025)
    retest_ok = ((retest_band_lo <= prev_l <= retest_band_hi) or (retest_band_lo <= prev2_l <= retest_band_hi))

    nr_recent = bool(df["nr7"].iloc[-2] or df["nr7"].iloc[-3] or df["nr4"].iloc[-2])
    seg = df.iloc[-120:]
    range_width = (seg["high"].max() - seg["low"].min()) / max(seg["close"].iloc[-1], 1e-9)
    range_atr = float(seg["atr"].iloc[-2]) / max(price, 1e-9)
    range_env = (range_width <= 6 * range_atr)

    if USE_PARABOLIC_GUARD and atr_pct > 0.020:
        seq_bull = int((df["close"] > df["open"]).tail(6).sum())
        if seq_bull > MAX_SEQ_BULL:
            _log_reject(symbol, "parabolic_runup")
            return None

    # مسافة EMA كحارس
    ema50 = float(closed["ema50"])
    ema200 = float(closed["ema200"])
    dist50_atr = (price - ema50) / max(atr, 1e-9)
    dist200_atr = (price - ema200) / max(atr, 1e-9)
    ema50_req = max(0.0, float(prof["ema50_req_R"]) - 0.05)
    ema200_req = max(0.0, float(prof["ema200_req_R"]) - 0.10)
    trend_guard = (dist50_atr >= ema50_req) and (dist200_atr >= ema200_req)
    mixed_guard = (dist50_atr >= (0.15 if is_major else 0.20))

    # ساعات BRK (+/- 1 ساعة تسامح)
    brk_in_session = (int(prof["brk_hour_start"]) <= hr_riyadh <= int(prof["brk_hour_end"]))
    if not brk_in_session and (
        abs(hr_riyadh - int(prof["brk_hour_start"])) <= 1
        or abs(hr_riyadh - int(prof["brk_hour_end"])) <= 1
    ):
        brk_in_session = True

    # اختيار الست-أب
    setup = None
    struct_ok = False
    reasons: list[str] = []
    brk_far = (price - hhv_prev) / max(atr, 1e-9) > MAX_BRK_DIST_ATR

    if breakout_ok and retest_ok and not brk_far and (rvol >= thr["RVOL_MIN"] or spike_ok) and brk_in_session:
        if ((regime == "trend" and trend_guard) or (regime != "trend" and mixed_guard)):
            if (rev_insideb or rev_engulf or candle_quality(closed, rvol)):
                setup = "BRK"
                struct_ok = True
                reasons += ["Breakout+Retest", "SessionOK"]

    if (setup is None) and ((regime == "trend") or (regime != "trend" and mixed_guard)):
        fib_ok = False
        if USE_FIB:
            sw = recent_swing(df, SWING_LOOKBACK)
            if sw and sw[0] is not None and sw[1] is not None:
                fib_ok = near_any_fib(price, sw[0], sw[1], FIB_TOL)[0]
        pull_near = (abs(price - float(closed["ema21"])) / max(price, 1e-9) <= 0.005) or fib_ok
        if pull_near and (rev_hammer or rev_engulf or rev_insideb):
            if ((regime == "trend" and trend_guard) or (regime != "trend" and mixed_guard)):
                if (price >= vwap_now * (1 - vw_tol)) and avwap_confluence_ok:
                    setup = "PULL"
                    struct_ok = True
                    reasons += ["Pullback Reclaim"]

    if (setup is None) and range_env and near_sup and (rev_hammer or candle_quality(closed, rvol)) and nr_recent:
        setup = "RANGE"
        struct_ok = True
        reasons += ["Range Rotation (NR)"]

    vbr_min_dev = float(prof["vbr_min_dev_atr"])
    if (setup is None and (atr_pct <= 0.015) and nr_recent):
        dev_atr = (vwap_now - price) / max(atr, 1e-9)  # موجب إذا تحت VWAP
        if dev_atr >= vbr_min_dev and (rev_hammer or rev_engulf or candle_quality(closed, rvol)):
            setup = "VBR"
            struct_ok = True
            reasons += ["VWAP Band Reversion"]

    if (setup is None) and had_sweep and (rev_engulf or candle_quality(closed, rvol) or price > float(closed["ema21"])):
        setup = "SWEEP"
        struct_ok = True
        reasons += ["Liquidity Sweep"]

    if setup is None:
        _log_reject(symbol, "no_setup_match")
        return None

    # Exhaustion guard
    dist_ema50_atr = (price - float(closed["ema50"])) / max(atr, 1e-9)
    rsi_now = float(closed["rsi"])
    if (setup in ("BRK", "PULL") and rsi_now >= RSI_EXHAUSTION and dist_ema50_atr >= DIST_EMA50_EXHAUST_ATR):
        _log_reject(symbol, f"exhaustion_guard rsi={rsi_now:.1f}, distATR={dist_ema50_atr:.2f}")
        return None

    # SL وأهداف
    def _protect_sl_with_swing(df_: pd.DataFrame, entry_price: float, atr_: float) -> float:
        base_sl = entry_price - max(atr_ * 0.9, entry_price * 0.002)
        try:
            swing_low = float(df_.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
            if swing_low < entry_price:
                return min(base_sl, swing_low)
        except Exception:
            pass
        return base_sl

    sl = _protect_sl_with_swing(df, price, atr)
    if (price - sl) < max(price * 1e-6, 1e-9):
        _log_reject(symbol, "R_too_small")
        return None

    def _build_targets_r(entry: float, sl_: float, tp_r: tuple[float, ...]) -> list[float]:
        R_ = max(entry - sl_, 1e-9)
        return [entry + r * R_ for r in tp_r]

    def _build_targets_pct_from_atr(price_: float, atr_: float, multipliers: tuple[float, ...]) -> tuple[list[float], tuple[float, ...]]:
        pcts = [max(atr_ / max(price_, 1e-9) * m, 0.002) for m in multipliers]
        return [price_ * (1 + p) for p in pcts], tuple(pcts)

    disp_mode = TARGETS_MODE_BY_SETUP.get(setup, "r")
    if ENABLE_MULTI_TARGETS:
        if disp_mode == "pct":
            if setup == "VBR":
                t_list, pct_vals = _build_targets_pct_from_atr(price, atr, ATR_MULT_VBR)
                if t_list and USE_VWAP:
                    t_list[0] = max(t_list[0], vwap_now)  # T1≈VWAP
            else:
                t_list, pct_vals = _build_targets_pct_from_atr(price, atr, ATR_MULT_RANGE)
        else:
            t_list = _build_targets_r(price, sl, TARGETS_R5)
    else:
        t_list = _build_targets_r(price, sl, _cfg["TP_R"])

    t_list = sorted(t_list)

    # حد أدنى T1 + قصّ تحت المقاومة
    if atr_pct <= 0.008:
        min_t1_pct = 0.0075
    elif atr_pct <= 0.020:
        min_t1_pct = 0.0095
    else:
        min_t1_pct = 0.0115
    min_t1_pct = max(min_t1_pct, MIN_T1_GAP_FLOOR)
    if hours_since_last_signal() >= SILENCE_SOFTEN_HOURS:
        min_t1_pct *= 0.95

    t1, _ = _clamp_t1_below_res(price, t_list[0], (res_eff if ('res_eff' in locals()) else None), buf_pct=0.0015)
    t_list[0] = t1
    if (t_list[0] - price) / max(price, 1e-9) < min_t1_pct:
        _log_reject(symbol, f"t1_entry_gap<{min_t1_pct:.3%}")
        return None
    if not (sl < price < t_list[0] <= t_list[-1]):
        _log_reject(symbol, "bounds_invalid(sl<price<t1<=tN)")
        return None

    # مسافة المقاومة بـ R
    R_val = max(price - sl, 1e-9)
    srdist_R = ((res_eff - price) / R_val) if ('res_eff' in locals() and res_eff is not None and res_eff > price) else 10.0
    if setup == "BRK" and near_res and srdist_R < 0.7:
        _log_reject(symbol, f"near_resistance_R={srdist_R:.2f}<0.70")
        return None

    # سكور شامل — نمرّر ema_align_final بدل True/ema_align
    score, bd = score_signal(
        struct_ok, rvol, atr_pct, ema_align_final, mtf_pass, srdist_R, mtf_has_frames,
        thr["RVOL_MIN"], (lo_dyn, hi_dyn),
        oi_trend=None, breadth_pct=breadth_pct,
        avwap_confluence=avwap_confluence_ok if USE_ANCHORED_VWAP else None,
        d1_ok=d1_ok, mtf_detail=mtf_detail
    )

    # تطبيق خصم ناعم لو مرّت إشارة الـEMA/VWAP بالعفو في وضع soft
    if soft_ema_penalty:
        score = max(0, score - soft_ema_penalty)

    if score < thr["SCORE_MIN"]:
        _log_reject(symbol, f"score<{thr['SCORE_MIN']} (got {score})")
        return None

    # منطقة دخول ديناميكية
    if score >= 88 or rvol >= 1.50:
        width_r = 0.14 * R_val
    elif score >= 84 or rvol >= 1.40:
        width_r = 0.15 * R_val
    elif score >= 76 or rvol >= 1.15:
        width_r = 0.25 * R_val
    else:
        width_r = 0.35 * R_val
    width_r = max(width_r, price * 0.003)
    width_r = min(width_r, ENTRY_MAX_R * R_val)
    entry_low = max(sl + 1e-6, price - width_r)
    entry_high = price
    entries = [round(entry_low, 6), round(entry_high, 6)] if entry_low < entry_high else None
    entry_out = round(sum(entries) / len(entries), 6) if entries else round(price, 6)

    tp1 = t_list[0]
    tp2 = t_list[1] if len(t_list) > 1 else t_list[0]
    tp3 = t_list[2] if len(t_list) > 2 else None
    tp_final = t_list[-1]

    # Trailing متكيّف بالسكور + قابل للتهيئة من البيئة
    if score >= 88:
        trail_mult_auto = 0.9
    elif score >= 84:
        trail_mult_auto = 1.0
    elif score >= 76:
        trail_mult_auto = 1.2
    else:
        trail_mult_auto = 1.4

    trail_mult_effective = float(os.getenv("TRAIL_ATR_MULT_TP2", str(trail_mult_auto)))

    # تحديث بصمة البار + عدّاد الانتقائية
    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_ENTRY_BAR_TS[base_sym] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # ETA للوصول إلى T1
    try:
        deltas = df["close"].diff().abs().tail(12).dropna()
        median_step = float(deltas.median()) if len(deltas) else max(1e-9, price * 0.0008)
    except Exception:
        median_step = max(1e-9, price * 0.0008)
    gap_to_t1 = abs(t1 - price)
    eta_bars = gap_to_t1 / max(median_step, 1e-9)
    base_max_bars = MAX_BARS_TO_TP1_BASE
    if atr_pct <= 0.008:
        base_max_bars = 10
    elif atr_pct >= 0.020:
        base_max_bars = 6
    if setup in ("BRK", "SWEEP"):
        base_max_bars = max(6, base_max_bars - 2)
    max_bars_to_tp1 = int(min(base_max_bars, math.ceil(eta_bars * 1.5))) if USE_MAX_BARS_TO_TP1 else None

    # Partials ديناميكي
    def _partials_for(score_: int, n_: int) -> list[float]:
        if score_ >= 88:
            base_ = [0.28, 0.24, 0.20, 0.16, 0.12]
        elif score_ >= 84:
            base_ = [0.30, 0.25, 0.20, 0.15, 0.10]
        elif score_ >= 76:
            base_ = [0.35, 0.25, 0.20, 0.12, 0.08]
        else:
            base_ = [0.40, 0.25, 0.18, 0.10, 0.07]
        return base_[:n_]
    partials = _partials_for(score, len(t_list))

    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1": MOTIVATION["tp1"].format(symbol=symbol),
        "tp2": MOTIVATION["tp2"].format(symbol=symbol),
        "tp3": MOTIVATION["tp3"].format(symbol=symbol),
        "tp4": MOTIVATION["tpX"].format(symbol=symbol),
        "tp5": MOTIVATION["tpX"].format(symbol=symbol),
        "sl": MOTIVATION["sl"].format(symbol=symbol),
        "time": MOTIVATION["time"].format(symbol=symbol),
    }

    return {
        "symbol": symbol, "side": "buy",
        "entry": round(entry_out, 6), "entries": entries,
        "sl": round(sl, 6), "targets": [round(x, 6) for x in t_list],
        "tp1": round(tp1, 6), "tp2": round(tp2, 6), "tp3": round(tp3, 6) if tp3 is not None else None,
        "tp_final": round(tp_final, 6),
        "atr": round(atr, 6), "r": round(entry_out - sl, 6),
        "score": int(score), "regime": regime, "reasons": reasons,
        "partials": partials,
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": trail_mult_effective if TRAIL_AFTER_TP2 else None,
        "max_bars_to_tp1": max_bars_to_tp1,
        "profile": RISK_MODE, "strategy_code": setup, "messages": messages,
    }

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

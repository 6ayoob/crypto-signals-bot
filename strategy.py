from __future__ import annotations
"""
strategy.py — R-based Router (BRK/PULL/RANGE/SWEEP) + S/R Clamp + MTF + Score
+ (New) dual-entry zone, 5 targets (optional), HTF close-below stop rule.

متوافق مع check_signal(symbol, ohlcv[, ohlcv_htf]).
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple
import os
import json
import pandas as pd

# ========= حساسية/سيولة/تذبذب =========
MIN_QUOTE_VOL = 20_000
VOL_MA = 20
ATR_PERIOD = 14
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200

# ========= أوضاع المخاطرة =========
RISK_MODE = os.getenv("RISK_MODE", "conservative").lower()
RISK_PROFILES = {
    "conservative": {"SCORE_MIN": 78, "ATR_BAND": (0.0018, 0.015), "RVOL_MIN": 1.05, "TP_R": (1.0, 1.8, 3.0), "HOLDOUT_BARS": 3, "MTF_STRICT": True},
    "balanced":     {"SCORE_MIN": 72, "ATR_BAND": (0.0015, 0.020), "RVOL_MIN": 1.00, "TP_R": (1.0, 2.0, 3.5), "HOLDOUT_BARS": 2, "MTF_STRICT": True},
    "aggressive":   {"SCORE_MIN": 68, "ATR_BAND": (0.0012, 0.030), "RVOL_MIN": 0.95, "TP_R": (1.2, 2.4, 4.0), "HOLDOUT_BARS": 1, "MTF_STRICT": False},
}
_cfg = RISK_PROFILES.get(RISK_MODE, RISK_PROFILES["conservative"])

# ========= مفاتيح الميزات الجديدة =========
ENABLE_MULTI_ENTRIES = os.getenv("ENABLE_MULTI_ENTRIES", "1") == "1"
ENABLE_MULTI_TARGETS = os.getenv("ENABLE_MULTI_TARGETS", "1") == "1"
ENABLE_STOP_RULE     = os.getenv("ENABLE_STOP_RULE", "1") == "1"

# منطقة الدخول (كعرض بالنسبة لـ R)
ENTRY_ZONE_WIDTH_R = float(os.getenv("ENTRY_ZONE_WIDTH_R", "0.25"))  # عرض المنطقة = 0.25R (من أسفل إلى الإغلاق الحالي)
ENTRY_MIN_PCT      = float(os.getenv("ENTRY_MIN_PCT", "0.005"))      # حد أدنى 0.5% للعرض
ENTRY_MAX_R        = float(os.getenv("ENTRY_MAX_R", "0.60"))         # حد أقصى 0.6R

# خمسة أهداف (عند التفعيل). داخليًا R-based، للعرض يمكن % حسب الست-أب.
TARGETS_MODE_BY_SETUP = {"BRK": "r", "PULL": "r", "RANGE": "pct", "SWEEP": "pct"}
TARGETS_R5   = tuple(float(x) for x in os.getenv("TARGETS_R5", "1.0,1.8,3.0,4.5,6.0").split(","))
TARGETS_PCTS = tuple(float(x) for x in os.getenv("TARGETS_PCTS", "0.03,0.06,0.09,0.12,0.15").split(","))
ALWAYS_LOG_R = True
MIN_T1_ABOVE_ENTRY = 0.015

# تقسيم الكمية (يبقى اختياري للعرض/إدارة لاحقة)
PARTIAL_FRACTIONS = [0.35, 0.25, 0.20, 0.12, 0.08]

# التريلينغ بعد هدف متقدم
TRAIL_AFTER_TP2 = True
TRAIL_AFTER_TP2_ATR = 1.0

# خروج زمني
USE_MAX_BARS_TO_TP1 = True
MAX_BARS_TO_TP1 = 6

# S/R & Fib
USE_SR = True
SR_WINDOW = 40
RES_BLOCK_NEAR = 0.004
SUP_BLOCK_NEAR = 0.003
BREAKOUT_BUFFER = 0.0015

USE_FIB = True
SWING_LOOKBACK = 60
FIB_TOL = 0.004

# وقف HTF
STOP_RULE_TF = os.getenv("STOP_RULE_TF", "H4").upper()  # H1/H4/D1
STOP_RULE_KIND = "htf_close_below"

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

# منع تكرار داخلي
_LAST_ENTRY_BAR_TS: dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: dict[str, int] = {}
HOLDOUT_BARS = _cfg["HOLDOUT_BARS"]

# ========= مؤشرات =========
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

def add_indicators(df):
    df["ema9"]   = ema(df["close"], EMA_FAST)
    df["ema21"]  = ema(df["close"], EMA_SLOW)
    df["ema50"]  = ema(df["close"], EMA_TREND)
    df["ema200"] = ema(df["close"], EMA_LONG)
    df["rsi"]    = rsi(df["close"], 14)
    df["vol_ma20"] = df["volume"].rolling(VOL_MA, min_periods=1).mean()
    df = macd_cols(df)
    df["atr"] = atr_series(df, ATR_PERIOD)
    return df

# ========= S/R & Fib =========
def get_sr_on_closed(df, window=40) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < window + 3: return None, None
    df_prev = df.iloc[:-1]; w = min(window, len(df_prev))
    resistance = df_prev["high"].rolling(w, min_periods=max(5, w//3)).max().iloc[-1]
    support    = df_prev["low"].rolling(w,  min_periods=max(5, w//3)).min().iloc[-1]
    if pd.isna(resistance) or pd.isna(support): return None, None
    return float(support), float(resistance)

def recent_swing(df, lookback=60) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < lookback + 5: return None, None
    seg = df.iloc[-(lookback+1):-1]; hhv = seg["high"].max(); llv = seg["low"].min()
    if pd.isna(hhv) or pd.isna(llv) or hhv <= llv: return None, None
    return float(hhv), float(llv)

def near_any_fib(price: float, hhv: float, llv: float, tol: float) -> Tuple[bool, str]:
    rng = hhv - llv
    if rng <= 0: return False, ""
    fib382 = hhv - rng * 0.382; fib618 = hhv - rng * 0.618
    for lvl, name in ((fib382, "Fib 0.382"), (fib618, "Fib 0.618")):
        if abs(price - lvl) / max(lvl, 1e-9) <= tol: return True, name
    return False, ""

def detect_regime(df) -> str:
    c = df["close"]; e50 = df["ema50"]
    up = (c.iloc[-1] > e50.iloc[-1]) and (e50.diff(10).iloc[-1] > 0)
    if up: return "trend"
    seg = df.iloc[-80:]
    width = (seg["high"].max() - seg["low"].min()) / max(seg["close"].iloc[-1], 1e-9)
    atrp = float(seg["atr"].iloc[-2]) / max(seg["close"].iloc[-2], 1e-9)
    return "range" if width <= 6 * atrp else "mixed"

# ========= برايس أكشن =========
def candle_quality(row) -> bool:
    o = float(row["open"]); c = float(row["close"]); h = float(row["high"]); l = float(row["low"])
    tr = max(h - l, 1e-9); body = abs(c - o); upper_wick = h - max(c, o)
    body_pct = body / tr; upwick_pct = upper_wick / tr
    return (c > o) and (body_pct >= 0.55) and (upwick_pct <= 0.35)

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
    return (float(cur["low"]) < float(prev["low"])) and (float(cur["close"]) > float(prev["close"]))

def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)

# ========= MTF =========
def _df_from_ohlcv(ohlcv: List[list]) -> Optional[pd.DataFrame]:
    try:
        df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().reset_index(drop=True)
        return add_indicators(df)
    except Exception:
        return None

def pass_mtf_filter_any(ohlcv_htf) -> bool:
    if ohlcv_htf is None: return True
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
    ok_count = 0
    for dfh in frames:
        if len(dfh) < 60: continue
        closed = dfh.iloc[-2]
        conds = [
            float(closed["close"]) > float(closed["ema50"]),
            float(closed["macd_hist"]) > 0,
            float(closed["rsi"]) > 50,
        ]
        if _cfg["MTF_STRICT"]:
            conds.append(float(dfh["ema50"].diff(5).iloc[-2]) > 0)
        ok_count += int(all(conds))
    if not frames: return True
    return ok_count >= 1

# ========= بناء الأهداف/الوقف =========
def _build_targets_r(entry: float, sl: float, tp_r: Tuple[float, ...]) -> List[float]:
    R = max(entry - sl, 1e-9)
    return [entry + r*R for r in tp_r]

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
def score_signal(struct_ok: bool, rvol: float, atr_pct: float, ema_align: bool, mtf_ok: bool, srdist_R: float) -> Tuple[int, Dict[str, float]]:
    w = {"struct": 30, "rvol": 15, "atr": 15, "ema": 15, "mtf": 15, "srdist": 10}
    sc = 0.0; bd = {}
    bd["struct"] = w["struct"] if struct_ok else 0; sc += bd["struct"]
    rvol_min = _cfg["RVOL_MIN"]
    rvol_score = min(max((rvol - rvol_min) / max(0.5, rvol_min), 0), 1) * w["rvol"]; bd["rvol"] = rvol_score; sc += rvol_score
    lo, hi = _cfg["ATR_BAND"]
    if atr_pct < lo or atr_pct > hi:
        bd["atr"] = 0
    else:
        center = (lo + hi)/2
        atr_score = (1 - abs(atr_pct - center)/max(center - lo, 1e-9)) * w["atr"]
        bd["atr"] = max(0, min(w["atr"], atr_score)); sc += bd["atr"]
    bd["ema"] = w["ema"] if ema_align else 0; sc += bd["ema"]
    bd["mtf"] = w["mtf"] if mtf_ok else 0; sc += bd["mtf"]
    srd = max(srdist_R, 0.0); srd_score = min(srd / 1.5, 1.0) * w["srdist"]; bd["srdist"] = srd_score; sc += srd_score
    return int(round(sc)), bd

# ========= المولّد =========
def check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[object] = None) -> Optional[Dict]:
    if not ohlcv or len(ohlcv) < 80: return None
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 60: return None

    df = add_indicators(df)
    if len(df) < 60: return None

    prev2  = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # منع التكرار + Holdout
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts: return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < HOLDOUT_BARS: return None

    # سيولة + تذبذب
    if price * float(closed["volume"]) < MIN_QUOTE_VOL: return None
    atr = float(df["atr"].iloc[-2]); atr_pct = atr / max(price, 1e-9)
    lo, hi = _cfg["ATR_BAND"]
    if not (lo <= atr_pct <= hi): return None

    # اتجاه/جودة
    if not (price > float(closed["open"])): return None
    ema_align = (float(closed["ema9"]) > float(closed["ema21"]) > float(closed["ema50"])) or (price > float(closed["ema50"]))
    if not ema_align: return None
    if not candle_quality(closed): return None

    # RVOL
    vma = float(closed.get("vol_ma20") or 0.0)
    rvol = (float(closed["volume"]) / (vma + 1e-9)) if vma > 0 else 0.0
    if rvol < _cfg["RVOL_MIN"]: return None

    # S/R + نظام السوق + MTF
    sup = res = None
    if USE_SR: sup, res = get_sr_on_closed(df, SR_WINDOW)
    regime = detect_regime(df)
    mtf_ok = pass_mtf_filter_any(ohlcv_htf)

    # برايس أكشن واختيار الست-أب
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
    retest_ok = float(prev["low"]) <= hhv_prev * (1.0 + 0.0005) and float(prev["low"]) >= hhv_prev * (1.0 - 0.002)
    hl_ok = float(closed["low"]) > float(prev["low"])

    seg = df.iloc[-120:]
    range_width = (seg["high"].max() - seg["low"].min())/max(seg["close"].iloc[-1],1e-9)
    range_atr = float(seg["atr"].iloc[-2])/max(price,1e-9)
    range_env = (regime == "range") or (range_width <= 6*range_atr)

    setup = None
    struct_ok = False
    reasons: List[str] = []

    if (regime in ("trend","mixed")) and breakout_ok and retest_ok and (rev_insideb or rev_engulf or candle_quality(closed)):
        setup = "BRK"; struct_ok = True; reasons += ["Breakout+Retest"]
    elif (regime in ("trend","mixed")) and (rev_hammer or rev_engulf or rev_insideb) and (
            abs(price - float(closed["ema21"])) / max(price,1e-9) <= 0.003 or (
            USE_FIB and (lambda: (lambda sw: near_any_fib(price, *sw, FIB_TOL) if all(sw) else (False, ""))((recent_swing(df, SWING_LOOKBACK))) )()[0]
        ):
        setup = "PULL"; struct_ok = True; reasons += ["Pullback Reclaim"]
    elif range_env and near_sup and (rev_hammer or candle_quality(closed)):
        setup = "RANGE"; struct_ok = True; reasons += ["Range Rotation"]
    elif had_sweep and (rev_engulf or candle_quality(closed) or price > float(closed["ema21"])):
        setup = "SWEEP"; struct_ok = True; reasons += ["Liquidity Sweep"]

    if setup is None: return None

    # SL وأهداف
    sl = _protect_sl_with_swing(df, price, atr)

    # أهداف (3 أو 5)
    if ENABLE_MULTI_TARGETS:
        disp_mode = TARGETS_MODE_BY_SETUP.get(setup, "r")
        if disp_mode == "pct":
            t_list = [price * (1 + p) for p in TARGETS_PCTS]
        else:
            t_list = _build_targets_r(price, sl, TARGETS_R5)
    else:
        t_list = _build_targets_r(price, sl, _cfg["TP_R"])
    t_list = sorted(t_list)

    # قصّ T1 لو قرب المقاومة
    t1, clamped = _clamp_t1_below_res(price, t_list[0], res, buf_pct=0.0015)
    t_list[0] = t1
    if clamped: reasons.append("T1@ResClamp")

    # رفض لو T1 صار قريب جدًا
    if (t_list[0] - price)/max(price,1e-9) < MIN_T1_ABOVE_ENTRY: return None

    if not (sl < price < t_list[0] <= t_list[-1]): return None

    # مسافة المقاومة بـ R
    R_val = max(price - sl, 1e-9)
    srdist_R = ((res - price)/R_val) if (res is not None and res > price) else 10.0

    # سكور
    score, bd = score_signal(struct_ok, rvol, atr_pct, ema_align, mtf_ok, srdist_R)
    if score < _cfg["SCORE_MIN"]: return None

    # منطقة دخول ثنائية (اختياري)
    entries = None
    if ENABLE_MULTI_ENTRIES:
        width_r = max(ENTRY_ZONE_WIDTH_R * R_val, price * ENTRY_MIN_PCT)
        width_r = min(width_r, ENTRY_MAX_R * R_val)
        entry_low  = max(sl + 1e-6, price - width_r)
        entry_high = price
        if entry_low < entry_high:
            entries = [round(entry_low, 6), round(entry_high, 6)]

    # حفظ آخر بار
    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # أسباب مختصرة
    if price > float(closed["ema50"]): reasons.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons.append("EMA9>EMA21")
    if is_hammer(closed): reasons.append("Hammer")
    if is_bull_engulf(prev, closed): reasons.append("Bull Engulf")
    if is_inside_break(prev2, prev, closed): reasons.append("InsideBreak")
    if near_res: reasons.append("NearRes")
    if near_sup: reasons.append("NearSup")
    reasons.append(f"RVOL≥{round(_cfg['RVOL_MIN'],2)}")
    confluence = reasons[:6]

    # رسائل
    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1":   MOTIVATION["tp1"].format(symbol=symbol),
        "tp2":   MOTIVATION["tp2"].format(symbol=symbol),
        "tp3":   MOTIVATION["tp3"].format(symbol=symbol),
        "tp4":   MOTIVATION["tpX"].format(symbol=symbol),
        "tp5":   MOTIVATION["tpX"].format(symbol=symbol),
        "sl":    MOTIVATION["sl"].format(symbol=symbol),
        "time":  MOTIVATION["time"].format(symbol=symbol),
    }

    # عرض للمشترك
    disp_mode = TARGETS_MODE_BY_SETUP.get(setup, "r")
    targets_display = {"mode": disp_mode, "values": list(TARGETS_PCTS if (ENABLE_MULTI_TARGETS and disp_mode=="pct") else (TARGETS_R5 if ENABLE_MULTI_TARGETS else _cfg["TP_R"]))}

    # زمن الوصول لـ T1
    max_bars_to_tp1 = MAX_BARS_TO_TP1
    if setup in ("BRK","SWEEP"): max_bars_to_tp1 = max(4, MAX_BARS_TO_TP1 - 1)

    # وقف HTF اختياري
    stop_rule = None
    if ENABLE_STOP_RULE:
        stop_rule = {"type": STOP_RULE_KIND, "tf": STOP_RULE_TF, "level": round(sl, 6)}

    # قيم متوافقة قديمة
    tp1 = t_list[0]; tp2 = t_list[1] if len(t_list) > 1 else t_list[0]
    tp3 = t_list[2] if len(t_list) > 2 else None
    tp_final = t_list[-1]

    entry_out = round(sum(entries)/len(entries), 6) if entries else round(price, 6)

    return {
        "symbol": symbol,
        "side": "LONG",
        "entry": entry_out,
        "entries": entries,                 # NEW
        "sl":    round(sl, 6),
        "targets": [round(x,6) for x in t_list],  # NEW
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp3":   round(tp3, 6) if tp3 is not None else None,
        "tp_final": round(tp_final, 6),

        "atr":   round(atr, 6),
        "r":     round(entry_out - sl, 6),
        "score": int(score),
        "regime": regime,
        "reasons": confluence,
        "confluence": confluence,

        "features": {
            "rsi": float(closed["rsi"]),
            "rvol": rvol,
            "atr_pct": atr_pct,
            "ema9": float(closed["ema9"]),
            "ema21": float(closed["ema21"]),
            "ema50": float(closed["ema50"]),
            "sup": float(sup) if sup is not None else None,
            "res": float(res) if res is not None else None,
            "setup": setup,
            "targets_display": targets_display,
            "score_breakdown": bd,
        },

        "partials": PARTIAL_FRACTIONS[:len(t_list)],
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": TRAIL_AFTER_TP2_ATR if TRAIL_AFTER_TP2 else None,
        "max_bars_to_tp1": max_bars_to_tp1 if USE_MAX_BARS_TO_TP1 else None,

        "cooldown_after_sl_min": 15,
        "cooldown_after_tp_min": 5,

        "profile": RISK_MODE,
        "strategy_code": setup,
        "messages": messages,

        "stop_rule": stop_rule,             # NEW
        "timestamp": datetime.utcnow().isoformat()
    }

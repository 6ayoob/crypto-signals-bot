# strategy.py — Router + Scoring + Guards | Unified TPs (+3/+6/+9%)
# إصلاح شامل: دمج الاستراتيجيات السابقة في 4 إعدادات أساسية، مع مُوجِّه (Router) حسب نظام السوق،
# ونظام نقاط (Score) + حراسات تنفيذية (Guards)، وأهداف موحّدة 3%/6%/9%، ووقف ديناميكي حسب ATR/البروفايل.
# متوافق مع منطق البوت الحالي (partials / trail_after_tp2 / max_bars_to_tp1 / الرسائل ...إلخ).

from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd
import math

# ========= حساسية/سيولة/تذبذب =========
MIN_QUOTE_VOL = 20_000
RVOL_MIN_HARD = 0.90
ATR_PCT_MIN   = 0.0015
ATR_PCT_MAX   = 0.06
HOLDOUT_BARS  = 2

# ========= مؤشرات أساسية =========
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200
VOL_MA, ATR_PERIOD = 20, 14
ADX_PERIOD = 14

# ========= S/R =========
USE_SR = True
SR_WINDOW = 40
RES_BLOCK_NEAR = 0.004
SUP_BLOCK_NEAR = 0.003
BREAKOUT_BUFFER = 0.0015

# ========= مقاومة قريبة وبدائل T1 =========
RESISTANCE_T1_OVERRIDE = True     # إن كانت المقاومة أقرب من +3% نُقَرِّب T1 لها مع هامش بسيط
RES_T1_BUFFER_PCT      = 0.0015   # -0.15% من مستوى المقاومة لضمان التنفيذ
MIN_T1_ABOVE_ENTRY     = 0.015    # على الأقل +1.5% فوق الدخول كي لا تصبح صفقة ضيقة جدًا

# ========= Fibonacci =========
USE_FIB = True
SWING_LOOKBACK = 60
FIB_TOL    = 0.004

# ========= MACD/RSI Policy =========
MACD_RSI_POLICY = "balanced"  # "lenient" | "balanced" | "strict"

_LAST_ENTRY_BAR_TS: dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: dict[str, int] = {}

# ========= إعدادات AUTO + أهداف/وقف ورسائل =========
ENTRY_PROFILE = "auto"   # "auto" أو "msb3" أو "vpc3" أو "dal3"

# --- أهداف موحّدة بالنِسَب (افتراضيًا) ---
TARGETS_MODE = "percent"          # "percent" (3/6/9%) أو "profile" (السلوك القديم)
TARGETS_PCTS = [0.03, 0.06, 0.09]  # نسب الأهداف من متوسط الدخول

# MSB3 (Breakout سكالب) — للسلوك legacy
FIXED_TP_PCTS   = [0.008, 0.016, 0.024]
FIXED_SL_PCT_MAX= 0.009

# VPC3 (Pullback ترند) — legacy
VPC3_TP_PCTS = [0.007, 0.013, 0.020]
VPC3_SL_ATR  = 0.8

# DAL3 (ديناميكي ATR) — legacy
DAL3_TP_ATR = [1.0, 1.8, 2.8]
DAL3_SL_ATR = 0.9

# تقسيم الكمية على الأهداف الثلاثة
PARTIAL_FRACTIONS = [0.40, 0.35, 0.25]

# تريلينغ بعد TP2
TRAIL_AFTER_TP2 = True
TRAIL_AFTER_TP2_ATR = 1.0  # SL = max(SL, current - 1×ATR)
MOVE_SL_TO_BE_ON_TP1 = True

# خروج زمني إن لم يُصب TP1 سريعًا
USE_MAX_BARS_TO_TP1 = True
MAX_BARS_TO_TP1 = 6

# تبريد بعد النتائج (يوظَّف من مدير الصفقات الخارجي)
COOLDOWN_AFTER_SL_MIN = 15
COOLDOWN_AFTER_TP_MIN = 5

# رسائل تحفيزية
MOTIVATION = {
    "entry": "🔥 دخول {symbol}! نبدأ بخطة ثلاثية الأهداف — فرصة سريعة 💪",
    "tp1":   "🎯 TP1 تحقق على {symbol}! أرباح مثبتة ونقلنا SL للتعادل — مستمرّون 👟",
    "tp2":   "🚀 TP2 على {symbol}! فعّلنا تريلينغ لحماية المكسب — نقترب من الختام 🏁",
    "tp3":   "🏁 TP3 على {symbol}! إغلاق جميل — صفقة مكتملة، رائع ✨",
    "sl":    "🛑 SL على {symbol}. الأهم حماية رأس المال — فرص أقوى قادمة 🔄",
    "time":  "⌛ خروج زمني على {symbol} — الحركة لم تتفعّل سريعًا، خرجنا بخفّة 🔎",
}

# ========= Engagement Mode (اختياري) =========
ENGAGEMENT_MODE = False
ENG_POLICY_OVERRIDE = True
ENG_RVOL_MIN_HARD = 0.85
ENG_ATR_PCT_MIN   = 0.0012
ENG_BREAKOUT_BUFFER = 0.0012
ENG_HOLDOUT_BARS    = 1

# ========= Reliability Boost =========
STRICT_MODE = True
STRICT_EMA_STACK = True
RVOL_MIN_STRICT = 1.05
STRICT_BODY_PCT_MIN = 0.55
MAX_UPWICK_PCT = 0.35
MTF_FILTER_ENABLED = True
MTF_REQUIRE_EMA_TREND = True

# ========= Regime Router =========
ADX_TREND_TH = 20
ADX_RANGE_TH = 15

# ---------- مؤشرات ----------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    d = series.diff()
    gain = d.where(d > 0, 0.0)
    loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean().replace(0, 1e-9)
    rs = ag / al
    return 100 - (100 / (1 + rs))


def macd_cols(df, fast=12, slow=26, signal=9):
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def atr_series(df, period=14):
    c = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]).abs(),
                    (df["high"]-c).abs(),
                    (df["low"]-c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def adx_series(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    up   = high.diff()
    down = -low.diff()
    plus_dm  = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr1 = (high - low).abs()
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di  = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr + 1e-9))
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr + 1e-9))
    dx = (100 * (plus_di - minus_di).abs() / ((plus_di + minus_di) + 1e-9)).fillna(0)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx


def add_indicators(df):
    df["ema9"]   = ema(df["close"], EMA_FAST)
    df["ema21"]  = ema(df["close"], EMA_SLOW)
    df["ema50"]  = ema(df["close"], EMA_TREND)
    df["ema200"] = ema(df["close"], EMA_LONG)
    df["rsi"]    = rsi(df["close"], 14)
    df["vol_ma20"] = df["volume"].rolling(VOL_MA, min_periods=1).mean()
    df = macd_cols(df)
    df["atr"] = atr_series(df, ATR_PERIOD)
    df["adx"] = adx_series(df, ADX_PERIOD)
    return df

# ---------- جودة الشموع / إطار أعلى ----------
def candle_quality(row) -> bool:
    o = float(row["open"]); c = float(row["close"]); h = float(row["high"]); l = float(row["low"])
    tr = max(h - l, 1e-9)
    body = abs(c - o)
    upper_wick = h - max(c, o)
    body_pct = body / tr
    upwick_pct = upper_wick / tr
    return (c > o) and (body_pct >= STRICT_BODY_PCT_MIN) and (upwick_pct <= MAX_UPWICK_PCT)


def ema_stack_ok(row) -> bool:
    return (float(row["ema9"]) > float(row["ema21"]) > float(row["ema50"]))


def pass_mtf_filter(ohlcv_htf: List[list]) -> bool:
    try:
        dfh = pd.DataFrame(ohlcv_htf, columns=["timestamp","open","high","low","close","volume"])
        for col in ["open","high","low","close","volume"]:
            dfh[col] = pd.to_numeric(dfh[col], errors="coerce")
        dfh = dfh.dropna().reset_index(drop=True)
        if len(dfh) < 60:
            return False
        dfh = add_indicators(dfh)
        closed_h = dfh.iloc[-2]
        conds = []
        conds.append(float(closed_h["close"]) > float(closed_h["ema50"]))
        conds.append(float(closed_h["macd_hist"]) > 0)
        conds.append(float(closed_h["rsi"]) > 50)
        if MTF_REQUIRE_EMA_TREND:
            conds.append(float(dfh["ema50"].diff(5).iloc[-2]) > 0)
        return all(conds)
    except Exception:
        return False

# ---------- أدوات S/R & Fibonacci ----------
def get_sr_on_closed(df, window=40) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < window + 3:
        return None, None
    df_prev = df.iloc[:-1]
    w = min(window, len(df_prev))
    resistance = df_prev["high"].rolling(w, min_periods=max(5, w//3)).max().iloc[-1]
    support    = df_prev["low"].rolling(w,  min_periods=max(5, w//3)).min().iloc[-1]
    if pd.isna(resistance) or pd.isna(support):
        return None, None
    return float(support), float(resistance)


def recent_swing(df, lookback=60) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < lookback + 5:
        return None, None
    seg = df.iloc[-(lookback+1):-1]
    hhv = seg["high"].max()
    llv = seg["low"].min()
    if pd.isna(hhv) or pd.isna(llv) or hhv <= llv:
        return None, None
    return float(hhv), float(llv)


def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)

# ---------- شموع وانعكاس ----------
def is_bull_engulf(prev, cur) -> bool:
    return (float(cur["close"]) > float(cur["open"]) and
            float(prev["close"]) < float(prev["open"]) and
            (float(cur["close"]) - float(cur["open"])) > (abs(float(prev["close"]) - float(prev["open"])) * 0.9) and
            float(cur["close"]) >= float(prev["open"]))


def is_hammer(cur) -> bool:
    h = float(cur["high"]); l = float(cur["low"]); o = float(cur["open"]); c = float(cur["close"])
    tr = max(h - l, 1e-9); body = abs(c - o)
    lower_wick = min(o, c) - l
    return (c > o) and (lower_wick / tr >= 0.5) and (body / tr <= 0.35) and ((h - max(o, c)) / tr <= 0.15)


def is_inside_break(pprev, prev, cur) -> bool:
    cond_inside = (float(prev["high"]) <= float(pprev["high"])) and (float(prev["low"]) >= float(pprev["low"]))
    return cond_inside and (float(cur["high"]) > float(prev["high"])) and (float(cur["close"]) > float(prev["high"]))


def swept_liquidity(prev, cur) -> bool:
    return (float(cur["low"]) < float(prev["low"])) and (float(cur["close"]) > float(prev["close"]))

# ---------- Router / Regime ----------
def detect_regime(df) -> Tuple[str, Dict[str, bool]]:
    adx_now = float(df["adx"].iloc[-2]) if "adx" in df else 0.0
    ema50_slope_up = float(df["ema50"].diff(5).iloc[-2]) > 0
    c = float(df["close"].iloc[-2])
    e50 = float(df["ema50"].iloc[-2])
    trend = (adx_now >= ADX_TREND_TH) and (c > e50) and ema50_slope_up
    rangey= (adx_now <= ADX_RANGE_TH)
    regime = "trend" if trend and not rangey else ("range" if rangey else "mixed")
    allow = {
        "breakout": regime in ("trend", "mixed"),
        "pullback": regime in ("trend", "mixed"),
        "range":    regime in ("range", "mixed"),
        "sweep":    True,
    }
    return regime, allow

# ---------- MACD/RSI Gate ----------
def macd_rsi_gate(prev_row, closed_row) -> Tuple[bool, list]:
    reasons = []
    rsi_now = float(closed_row["rsi"])
    rsi_up  = rsi_now > float(prev_row["rsi"])
    macd_h_now = float(closed_row["macd_hist"])
    macd_h_prev= float(prev_row["macd_hist"])
    macd_pos   = macd_h_now > 0
    macd_up    = macd_h_now > macd_h_prev
    ok_flags = []
    if rsi_now > 50: ok_flags.append("RSI>50")
    if rsi_up:       ok_flags.append("RSI↑")
    if macd_pos:     ok_flags.append("MACD_hist>0")
    if macd_up:      ok_flags.append("MACD_hist↑")
    k = len(ok_flags)
    policy = "lenient" if (ENGAGEMENT_MODE and ENG_POLICY_OVERRIDE) else MACD_RSI_POLICY
    if policy == "lenient": ok = k >= 1
    elif policy == "strict": ok = ("RSI>50" in ok_flags) and ("MACD_hist>0" in ok_flags) and ("MACD_hist↑" in ok_flags)
    else: ok = k >= 2
    if ok: reasons.extend(ok_flags[:2])
    return ok, reasons

# ---------- بناء الأهداف والوقف ----------
def _profile_sl(entry_price: float, atr_val: Optional[float], profile: str) -> float:
    atr = float(atr_val or 0.0)
    p = (profile or "msb3").lower()
    if p == "dal3" and atr > 0:
        sl  = entry_price - atr * DAL3_SL_ATR
    elif p == "vpc3" and atr > 0:
        sl_atr = entry_price - atr * VPC3_SL_ATR
        sl_pct = entry_price * (1 - FIXED_SL_PCT_MAX)
        sl  = min(sl_atr, sl_pct)
    else:
        if atr > 0:
            sl_atr = entry_price - atr * 0.8
            sl_pct = entry_price * (1 - FIXED_SL_PCT_MAX)
            sl  = min(sl_atr, sl_pct)
        else:
            sl  = entry_price * (1 - FIXED_SL_PCT_MAX)
    return float(sl)


def _build_targets_percent(entry_price: float) -> Tuple[float, float, float]:
    tps = [entry_price * (1 + pct) for pct in TARGETS_PCTS]
    tps.sort()
    return float(tps[0]), float(tps[1]), float(tps[2])


def _apply_resistance_override(entry: float, tp1: float, res: Optional[float]) -> Tuple[float, bool]:
    """لو المقاومة أقرب من T1؛ نُقَرِّب T1 إليها مع هامش، بشرط تبقى ≥ entry*(1+MIN_T1_ABOVE_ENTRY)."""
    if not RESISTANCE_T1_OVERRIDE or res is None:
        return tp1, False
    ideal_min = entry * (1 + MIN_T1_ABOVE_ENTRY)
    if res <= ideal_min:
        return ideal_min, True
    if res < tp1:
        adj = max(ideal_min, res * (1 - RES_T1_BUFFER_PCT))
        return adj, True
    return tp1, False


# ---------- كاشفات الإعدادات (Structure) ----------
def detect_breakout_retest(df, price: float, res: Optional[float], eff_bb: float) -> bool:
    try:
        hhv = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
    except Exception:
        return False
    breakout = price > hhv * (1.0 + eff_bb)
    prev = df.iloc[-3]
    retest = (res is not None) and (abs(float(prev["low"]) - res) / max(res, 1e-9) <= SUP_BLOCK_NEAR)
    return breakout and (retest or (res is None))


def detect_trend_pullback_reclaim(closed, prev, ema_ok: bool) -> bool:
    # بديل VWAP: استعادة EMA20 بعد سحبة
    pull = float(closed["close"]) > float(closed["ema21"]) >= float(prev["ema21"])
    return ema_ok and pull


def detect_range_rotation(df, price: float, sup: Optional[float], adx_now: float) -> bool:
    if adx_now > ADX_RANGE_TH:
        return False
    near_sup = near_level(price, sup, SUP_BLOCK_NEAR)
    # شمعة رفض من الدعم
    prev = df.iloc[-3]; closed = df.iloc[-2]
    reject = (float(closed["close"]) > float(closed["open"])) and (float(closed["low"]) <= float(prev["low"]))
    return bool(near_sup and reject)


def detect_sweep_reclaim(prev, closed) -> bool:
    swept = (float(closed["low"]) < float(prev["low"])) and (float(closed["close"]) > float(prev["close"]))
    body_up = float(closed["close"]) > float(closed["open"])
    return bool(swept and body_up)

# ---------- Score (0..100) ----------
def score_signal(struct_ok: bool, rvol: float, atr_pct: float, ema_align: bool, regime: str) -> Tuple[int, Dict[str, int]]:
    s_structure = 30 if struct_ok else 0
    # زخم/حجم: RVol
    if   rvol >= 1.8: s_rvol = 25
    elif rvol >= 1.4: s_rvol = 20
    elif rvol >= 1.2: s_rvol = 16
    elif rvol >= 1.0: s_rvol = 12
    else:             s_rvol = 0
    # سيولة/ATR
    if ATR_PCT_MIN <= atr_pct <= 0.008: s_atr = 20
    elif atr_pct <= 0.015:             s_atr = 16
    elif atr_pct <= ATR_PCT_MAX:       s_atr = 10
    else:                              s_atr = 0
    # توافق الأطر
    s_tf = 20 if ema_align else 10
    total = s_structure + s_rvol + s_atr + s_tf
    return int(min(100, total)), {"structure": s_structure, "rvol": s_rvol, "atr": s_atr, "tf": s_tf}


# ---------- المُولِّد الرئيسي ----------
def check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[List[list]] = None) -> Optional[Dict]:
    if not ohlcv or len(ohlcv) < 80: return None

    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 60: return None

    df = add_indicators(df)
    if len(df) < 60: return None

    prev2  = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # Engagement overrides
    eff_holdout = ENG_HOLDOUT_BARS if ENGAGEMENT_MODE else HOLDOUT_BARS
    eff_rvol_min = ENG_RVOL_MIN_HARD if ENGAGEMENT_MODE else RVOL_MIN_HARD
    eff_atr_min  = ENG_ATR_PCT_MIN if ENGAGEMENT_MODE else ATR_PCT_MIN
    eff_bb       = ENG_BREAKOUT_BUFFER if ENGAGEMENT_MODE else BREAKOUT_BUFFER

    # Dedupe/Holdout
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts: return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < eff_holdout: return None

    # سيولة + تذبذب
    if price * float(closed["volume"]) < MIN_QUOTE_VOL: return None
    atr = float(df["atr"].iloc[-2])
    atr_pct = atr / max(price, 1e-9)
    if atr_pct < eff_atr_min or atr_pct > ATR_PCT_MAX: return None

    # اتجاه/جودة شمعة
    if not (price > float(closed["open"])): return None
    if not ((float(closed["ema9"]) > float(closed["ema21"])) or (price > float(closed["ema50"]))): return None
    if STRICT_MODE:
        if STRICT_EMA_STACK and not ema_stack_ok(closed): return None
        if not candle_quality(closed): return None

    # RVOL
    vma = float(closed["vol_ma20"]) if not pd.isna(closed["vol_ma20"]) else 0.0
    rvol = (float(closed["volume"]) / (vma + 1e-9)) if vma > 0 else 0.0
    eff_rvol_gate = max(eff_rvol_min, RVOL_MIN_STRICT) if STRICT_MODE else eff_rvol_min
    if rvol < eff_rvol_gate: return None

    # MACD/RSI Gate
    ok_mr, mr_reasons = macd_rsi_gate(prev, closed)
    if not ok_mr: return None

    reasons = list(mr_reasons)

    # S/R
    sup = res = None
    if USE_SR: sup, res = get_sr_on_closed(df, SR_WINDOW)

    # فلتر إطار أعلى
    if MTF_FILTER_ENABLED and ohlcv_htf:
        if not pass_mtf_filter(ohlcv_htf): return None

    # Router
    regime, allow = detect_regime(df)

    # اختيار الإعداد الأنسب
    ema50_slope_up = (float(df["ema50"].diff(5).iloc[-2]) > 0)
    ema_align = (price > float(closed["ema50"])) and ema50_slope_up and (float(closed["ema9"]) > float(closed["ema21"]))

    setup_code = None
    struct_ok = False

    # 1) Breakout & Retest
    if not struct_ok and allow["breakout"] and detect_breakout_retest(df, price, res, eff_bb):
        setup_code = "BRK"; struct_ok = True; reasons.append("Breakout+Retest")

    # 2) Trend Pullback + EMA20 Reclaim
    if not struct_ok and allow["pullback"] and detect_trend_pullback_reclaim(closed, prev, ema_align):
        setup_code = "PULL"; struct_ok = True; reasons.append("Pullback Reclaim")

    # 3) Range Rotation
    if not struct_ok and allow["range"] and detect_range_rotation(df, price, sup, float(df["adx"].iloc[-2])):
        setup_code = "RANGE"; struct_ok = True; reasons.append("Range Rotation")

    # 4) Liquidity Sweep & Reclaim
    if not struct_ok and allow["sweep"] and detect_sweep_reclaim(prev, closed):
        setup_code = "SWEEP"; struct_ok = True; reasons.append("Sweep Reclaim")

    if not struct_ok: return None

    # Score
    score, breakdown = score_signal(struct_ok, rvol, atr_pct, ema_align, regime)
    if score < 70: return None  # فلتر نهائي للجودة

    # بروفايل للـ SL
    profile = ENTRY_PROFILE.lower() if ENTRY_PROFILE != "auto" else (
        "dal3" if (atr_pct >= 0.008 or rvol >= 2.0) else ("vpc3" if ema_align else "msb3")
    )

    # حساب SL
    sl = _profile_sl(price, atr, profile)
    # حماية إضافية بسوينغ لو
    try:
        swing_low = float(df.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
        if swing_low < price: sl = min(sl, swing_low)
    except Exception: pass

    # حساب الأهداف
    if TARGETS_MODE == "percent":
        tp1, tp2, tp3 = _build_targets_percent(price)
        # مقاومة قريبة؟ عدّل T1
        tp1, overridden = _apply_resistance_override(price, tp1, res)
        if overridden: reasons.append("T1@ResAdjust")
    else:
        # legacy
        if profile == "dal3" and atr > 0:
            tp1 = price + atr * DAL3_TP_ATR[0]; tp2 = price + atr * DAL3_TP_ATR[1]; tp3 = price + atr * DAL3_TP_ATR[2]
        elif profile == "vpc3" and atr > 0:
            tp1 = price * (1 + VPC3_TP_PCTS[0]); tp2 = price * (1 + VPC3_TP_PCTS[1]); tp3 = price * (1 + VPC3_TP_PCTS[2])
        else:
            tp1 = price * (1 + FIXED_TP_PCTS[0]); tp2 = price * (1 + FIXED_TP_PCTS[1]); tp3 = price * (1 + FIXED_TP_PCTS[2])

    # ترتيب منطقي
    tps = sorted([tp1, tp2, tp3]); tp1, tp2, tp3 = tps[0], tps[1], tps[2]
    if not (sl < price < tp1 <= tp2 <= tp3): return None

    # حراسة مقاومة/مسافة T1 الدنيا
    if (tp1 - price) / price < MIN_T1_ABOVE_ENTRY: return None

    # Holdout + Dedupe
    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # اقتراح حجم متكيّف
    if   atr_pct <= 0.004 and score >= 85: size_mult = 1.25
    elif atr_pct <= 0.008 and score >= 75: size_mult = 1.0
    elif atr_pct <= 0.03:                   size_mult = 0.8
    else:                                   size_mult = 0.5

    # أسباب/كونفلوينس
    if price > float(closed["ema50"]): reasons.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons.append("EMA9>EMA21")
    if is_hammer(closed): reasons.append("Hammer")
    if is_bull_engulf(prev, closed): reasons.append("Bull Engulf")
    if is_inside_break(prev2, prev, closed): reasons.append("InsideBreak")
    if near_level(price, res, RES_BLOCK_NEAR): reasons.append("NearRes")
    if TARGETS_MODE == "percent": reasons.append("TPs=+3/+6/+9%")
    reasons.append(f"RVOL≥{round(max(eff_rvol_min, RVOL_MIN_STRICT if STRICT_MODE else eff_rvol_min),2)}")
    confluence = reasons[:6]

    # رسائل
    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1":   MOTIVATION["tp1"].format(symbol=symbol),
        "tp2":   MOTIVATION["tp2"].format(symbol=symbol),
        "tp3":   MOTIVATION["tp3"].format(symbol=symbol),
        "sl":    MOTIVATION["sl"].format(symbol=symbol),
        "time":  MOTIVATION["time"].format(symbol=symbol),
    }

    # إخراج موحّد للبوت
    return {
        "symbol": symbol,
        "side": "LONG",
        "entry": round(price, 6),
        "sl":    round(sl, 6),
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp3":   round(tp3, 6),
        "tp_final": round(tp3, 6),

        "atr":   round(atr, 6),
        "r":     round(price - sl, 6),
        "score": int(score),
        "score_breakdown": breakdown,
        "router": {"regime": regime, "allow": allow, "setup": setup_code},
        "size_mult": round(size_mult, 2),

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
        },

        # إدارة لاحقة
        "partials": PARTIAL_FRACTIONS,
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": TRAIL_AFTER_TP2_ATR if TRAIL_AFTER_TP2 else None,
        "move_sl_to_be_on_tp1": MOVE_SL_TO_BE_ON_TP1,
        "max_bars_to_tp1": MAX_BARS_TO_TP1 if USE_MAX_BARS_TO_TP1 else None,
        "cooldown_after_sl_min": COOLDOWN_AFTER_SL_MIN,
        "cooldown_after_tp_min": COOLDOWN_AFTER_TP_MIN,

        # وضع الأهداف
        "targets_mode": TARGETS_MODE,
        "targets_pct": TARGETS_PCTS if TARGETS_MODE == "percent" else None,

        # بروفايل ورسائل + وسم الاستراتيجية
        "profile": profile,
        "strategy_code": setup_code,
        "messages": messages,
        "timestamp": datetime.utcnow().isoformat()
    }

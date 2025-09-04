# strategy.py — Auto S+/G3/S1 | S/R + Reversal Candles + Confluence
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd

# ========= حساسية/سيولة/تذبذب (كما هي) =========
MIN_QUOTE_VOL = 20_000
RVOL_MIN_HARD = 0.90
ATR_PCT_MIN   = 0.0015
ATR_PCT_MAX   = 0.06
HOLDOUT_BARS  = 2

# ========= مؤشرات أساسية =========
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200
VOL_MA, ATR_PERIOD = 20, 14

# ========= S/R =========
USE_SR = True
SR_WINDOW = 40
RES_BLOCK_NEAR = 0.004
SUP_BLOCK_NEAR = 0.003
BREAKOUT_BUFFER = 0.0015

# ========= Fibonacci =========
USE_FIB = True
SWING_LOOKBACK = 60
FIB_LEVELS = (0.382, 0.618)
FIB_TOL    = 0.004

# ========= MACD/RSI Policy =========
MACD_RSI_POLICY = "balanced"  # "lenient" | "balanced" | "strict"

_LAST_ENTRY_BAR_TS: dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: dict[str, int] = {}

# ========= إعدادات AUTO + أهداف/وقف ورسائل =========
ENTRY_PROFILE = "auto"   # "auto" أو "msb3" أو "vpc3" أو "dal3"

# MSB3 (Breakout سكالب) — نسب ثابتة قريبة
FIXED_TP_PCTS   = [0.008, 0.016, 0.024]  # 0.8% / 1.6% / 2.4%
FIXED_SL_PCT_MAX= 0.009                   # 0.9% (يُقارن مع ATR)

# VPC3 (Pullback ترند)
VPC3_TP_PCTS = [0.007, 0.013, 0.020]
VPC3_SL_ATR  = 0.8

# DAL3 (ديناميكي ATR)
DAL3_TP_ATR = [1.0, 1.8, 2.8]
DAL3_SL_ATR = 0.9

# تقسيم الكمية على الأهداف الثلاثة
PARTIAL_FRACTIONS = [0.40, 0.35, 0.25]

# تريلينغ بعد TP2
TRAIL_AFTER_TP2 = True
TRAIL_AFTER_TP2_ATR = 1.0  # SL = max(SL, current - 1×ATR)

# خروج زمني إن لم يُصب TP1 سريعًا (6 شموع 5m ≈ 30 دقيقة)
USE_MAX_BARS_TO_TP1 = True
MAX_BARS_TO_TP1 = 6

# تبريد بعد النتائج (يوظَّف من مدير الصفقات الخارجي)
COOLDOWN_AFTER_SL_MIN = 15
COOLDOWN_AFTER_TP_MIN = 5

# رسائل تحفيزية جاهزة
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

# ========= Reliability Boost (Strict filters) =========
STRICT_MODE = True
STRICT_EMA_STACK = True
RVOL_MIN_STRICT = 1.05
STRICT_BODY_PCT_MIN = 0.55
MAX_UPWICK_PCT = 0.35
MTF_FILTER_ENABLED = True
MTF_REQUIRE_EMA_TREND = True

# ---------- فلاتر الجودة/إطار أعلى ----------
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

# ---------- أدوات S/R & Fibo ----------
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

def near_any_fib(price: float, hhv: float, llv: float, tol: float) -> Tuple[bool, str]:
    rng = hhv - llv
    if rng <= 0:
        return False, ""
    fib382 = hhv - rng * 0.382
    fib618 = hhv - rng * 0.618
    for lvl, name in ((fib382, "Fib 0.382"), (fib618, "Fib 0.618")):
        if abs(price - lvl) / max(lvl, 1e-9) <= tol:
            return True, name
    return False, ""

def detect_regime(df) -> str:
    c = df["close"]; e200 = df.get("ema200", None)
    if e200 is None or pd.isna(e200.iloc[-1]):
        e50 = df["ema50"]
        return "trend" if (c.iloc[-1] > e50.iloc[-1] and e50.diff(10).iloc[-1] > 0) else "mean"
    return "trend" if (c.iloc[-1] > e200.iloc[-1] and e200.diff(10).iloc[-1] > 0) else "mean"

# ---------- شموع انعكاسية / سلوك ----------
def is_bull_engulf(prev, cur) -> bool:
    # جسم أخضر يبتلع جسم شمعة سابقة حمراء
    return (float(cur["close"]) > float(cur["open"]) and
            float(prev["close"]) < float(prev["open"]) and
            (float(cur["close"]) - float(cur["open"])) > (abs(float(prev["close"]) - float(prev["open"])) * 0.9) and
            float(cur["close"]) >= float(prev["open"]))

def is_hammer(cur) -> bool:
    h = float(cur["high"]); l = float(cur["low"]); o = float(cur["open"]); c = float(cur["close"])
    tr = max(h - l, 1e-9); body = abs(c - o)
    lower_wick = min(o, c) - l
    # ذيل سفلي طويل، جسم صغير قرب الأعلى
    return (c > o) and (lower_wick / tr >= 0.5) and (body / tr <= 0.35) and ((h - max(o, c)) / tr <= 0.15)

def is_inside_break(pprev, prev, cur) -> bool:
    # Inside Bar (prev داخل نطاق pprev) ثم اختراق لأعلى على cur
    cond_inside = (float(prev["high"]) <= float(pprev["high"])) and (float(prev["low"]) >= float(pprev["low"]))
    return cond_inside and (float(cur["high"]) > float(prev["high"])) and (float(cur["close"]) > float(prev["high"]))

def swept_liquidity(prev, cur) -> bool:
    # Sweep بسيط: لمس قاع سابق ثم إغلاق أعلى
    return (float(cur["low"]) < float(prev["low"])) and (float(cur["close"]) > float(prev["close"]))

def near_level(price: float, level: Optional[float], tol: float) -> bool:
    return (level is not None) and (abs(price - level) / max(level, 1e-9) <= tol)

# ---------- اختيار البروفايل تلقائيًا ----------
def _decide_profile_from_df(df) -> str:
    closed = df.iloc[-2]
    price  = float(closed["close"])
    atr    = float(df["atr"].iloc[-2]) if "atr" in df else None
    atr_pct = (atr / price) if (atr and price > 0) else 0.0

    vma = float(closed.get("vol_ma20") or 0.0)
    rvol = (float(closed["volume"]) / vma) if vma > 0 else 0.0

    ema50_now  = float(closed.get("ema50") or price)
    ema50_prev = float(df["ema50"].iloc[-7]) if len(df) >= 7 else ema50_now
    trend_ok = (price > ema50_now) and ((ema50_now - ema50_prev) > 0)

    ema21 = float(closed.get("ema21") or price)
    near_ema21 = abs(price - ema21) / max(price, 1e-9) <= 0.0025  # ±0.25%

    if atr_pct >= 0.008 or rvol >= 2.0:
        return "dal3"
    if trend_ok and near_ema21:
        return "vpc3"
    return "msb3"

def _build_targets(entry_price: float, atr_val: Optional[float], profile: str) -> Tuple[float, float, float, float]:
    atr = float(atr_val or 0.0)
    p = (profile or "msb3").lower()
    if p == "dal3" and atr > 0:
        tp1 = entry_price + atr * DAL3_TP_ATR[0]
        tp2 = entry_price + atr * DAL3_TP_ATR[1]
        tp3 = entry_price + atr * DAL3_TP_ATR[2]
        sl  = entry_price - atr * DAL3_SL_ATR
    elif p == "vpc3" and atr > 0:
        tp1 = entry_price * (1 + VPC3_TP_PCTS[0])
        tp2 = entry_price * (1 + VPC3_TP_PCTS[1])
        tp3 = entry_price * (1 + VPC3_TP_PCTS[2])
        sl_atr = entry_price - atr * VPC3_SL_ATR
        sl_pct = entry_price * (1 - FIXED_SL_PCT_MAX)
        sl  = min(sl_atr, sl_pct)
    else:  # msb3
        tp1 = entry_price * (1 + FIXED_TP_PCTS[0])
        tp2 = entry_price * (1 + FIXED_TP_PCTS[1])
        tp3 = entry_price * (1 + FIXED_TP_PCTS[2])
        if atr > 0:
            sl_atr = entry_price - atr * 0.8
            sl_pct = entry_price * (1 - FIXED_SL_PCT_MAX)
            sl  = min(sl_atr, sl_pct)
        else:
            sl  = entry_price * (1 - FIXED_SL_PCT_MAX)
    tp1, tp2, tp3 = sorted([tp1, tp2, tp3])
    return float(sl), float(tp1), float(tp2), float(tp3)

def _build_targets_s1(entry: float, sl: float) -> Tuple[float, float, float, float]:
    # هدف واحد قريب ≈ 0.5R (نُرجِع tp1=tp2=tp3 لتوافق البوت: يغلق عند TP2)
    R = max(entry - sl, 1e-9)
    tp = entry + 0.5 * R
    return float(sl), float(tp), float(tp), float(tp)

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
    if MACD_RSI_POLICY == "lenient":
        ok = k >= 1
    elif MACD_RSI_POLICY == "strict":
        ok = ("RSI>50" in ok_flags) and ("MACD_hist>0" in ok_flags) and ("MACD_hist↑" in ok_flags)
    else:
        ok = k >= 2

    if ok:
        reasons.extend(ok_flags[:2])
    return ok, reasons

# ---------- مولّد الإشارة ----------
def check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[List[list]] = None) -> Optional[Dict]:
    if not ohlcv or len(ohlcv) < 80:
        return None

    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 60:
        return None

    df = add_indicators(df)
    if len(df) < 60:
        return None

    prev2  = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # إعدادات فعّالة لوضع Engagement (لا تغيّر الثوابت الأصلية)
    eff_holdout = ENG_HOLDOUT_BARS if ENGAGEMENT_MODE else HOLDOUT_BARS
    eff_rvol_min = ENG_RVOL_MIN_HARD if ENGAGEMENT_MODE else RVOL_MIN_HARD
    eff_atr_min  = ENG_ATR_PCT_MIN if ENGAGEMENT_MODE else ATR_PCT_MIN
    eff_bb       = ENG_BREAKOUT_BUFFER if ENGAGEMENT_MODE else BREAKOUT_BUFFER

    # منع تكرار + تبريد
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts:
        return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < eff_holdout:
        return None

    # سيولة + تذبذب
    if price * float(closed["volume"]) < MIN_QUOTE_VOL:
        return None
    atr = float(df["atr"].iloc[-2])
    atr_pct = atr / max(price, 1e-9)
    if atr_pct < eff_atr_min or atr_pct > ATR_PCT_MAX:
        return None

    # اتجاه/جودة شمعة
    if not (price > float(closed["open"])):
        return None
    if not ((float(closed["ema9"]) > float(closed["ema21"])) or (price > float(closed["ema50"]))):
        return None
    if STRICT_MODE:
        if STRICT_EMA_STACK and not ema_stack_ok(closed):
            return None
        if not candle_quality(closed):
            return None

    # RVOL
    vma = float(closed["vol_ma20"]) if not pd.isna(closed["vol_ma20"]) else 0.0
    rvol = (float(closed["volume"]) / (vma + 1e-9)) if vma > 0 else 0.0
    eff_rvol_gate = max(eff_rvol_min, RVOL_MIN_STRICT) if STRICT_MODE else eff_rvol_min
    if rvol < eff_rvol_gate:
        return None

    # MACD/RSI Gate — تبديل مؤقت للـ policy إن لزم
    global MACD_RSI_POLICY
    _pol_prev = MACD_RSI_POLICY
    if ENGAGEMENT_MODE and ENG_POLICY_OVERRIDE:
        MACD_RSI_POLICY = "lenient"
    ok_mr, mr_reasons = macd_rsi_gate(prev, closed)
    MACD_RSI_POLICY = _pol_prev
    if not ok_mr:
        return None

    reasons = []
    reasons.extend(mr_reasons)

    # S/R
    sup = res = None
    if USE_SR:
        sup, res = get_sr_on_closed(df, SR_WINDOW)

    # اكتشاف النظام (للمعلومة)
    regime = detect_regime(df)

    # فلتر إطار أعلى (إن توفرت بياناته)
    if MTF_FILTER_ENABLED and ohlcv_htf:
        if not pass_mtf_filter(ohlcv_htf):
            return None

    # ========= طبقة Price Action لاختيار الاستراتيجية =========
    # شموع انعكاس
    rev_hammer  = is_hammer(closed)
    rev_engulf  = is_bull_engulf(prev, closed)
    rev_insideb = is_inside_break(prev2, prev, closed)
    had_sweep   = swept_liquidity(prev, closed)

    # قرب مستويات
    near_res = near_level(price, res, RES_BLOCK_NEAR)
    near_sup = near_level(price, sup, SUP_BLOCK_NEAR)

    # اتجاه عام نظيف
    ema50_slope_up = (float(df["ema50"].diff(5).iloc[-2]) > 0)
    trend_ok = (price > float(closed["ema50"])) and ema50_slope_up and (float(closed["ema9"]) > float(closed["ema21"]))

    # ============ S1: آمنة جدًا بهدف واحد ============
    s1_ok = False
    if trend_ok and (0.0015 <= atr_pct <= 0.0065) and (0.85 <= rvol <= 1.15):
        # Break–Retest–Go هادئ قرب مقاومة تحوّلت لدعم + شمعة انعكاس + MSS بسيط (كسر قمة ميكرو)
        mss = float(closed["high"]) > float(prev["high"])
        s1_ok = (near_res or near_level(price, float(df.iloc[:-1]["high"].rolling(10, min_periods=5).max().iloc[-1]), 0.003)) \
                and (rev_hammer or rev_engulf or rev_insideb) and mss

    # ============ G3: آمنة بثلاثة أهداف قريبة ============
    g3_ok = False
    if not s1_ok and trend_ok:
        # R→S أو HL عند مستوى واضح + شمعة انعكاسية + RVOL طبيعي
        # تقريب R→S: كسر سابق لأعلى (hhv_prev) ثم رجوع للمستوى مع شمعة انعكاس
        try:
            hhv_prev = float(df.iloc[-8:-1]["high"].max())
        except Exception:
            hhv_prev = float(prev["high"])
        broke_before = float(prev["close"]) > hhv_prev * (1.0 + eff_bb*0.5)
        hl_ok = float(closed["low"]) > float(prev["low"])  # HL مبسّط
        g3_ok = ((broke_before and near_res) or hl_ok) and (rev_hammer or rev_engulf or rev_insideb) and (0.9 <= rvol <= 1.3)

    # ============ S+: سكالب نشِط (المنطق الأصلي) ============
    splus_ok = False
    entry_tag = ""
    if not (s1_ok or g3_ok):
        # (أ) اختراق مقاومة + هامش
        if res is not None:
            hhv = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
            breakout_ok = price > hhv * (1.0 + eff_bb)
            near_res_block = (price >= res * (1 - RES_BLOCK_NEAR)) and (price <= res * (1 + RES_BLOCK_NEAR))
            if breakout_ok and not near_res_block:
                splus_ok = True
                entry_tag = "Breakout SR"
                reasons.append("Breakout SR")
        # (ب) ارتداد فيبو 0.382/0.618 مع تحسّن زخم
        if not splus_ok and USE_FIB:
            hhv2, llv2 = recent_swing(df, SWING_LOOKBACK)
            if hhv2 and llv2:
                near_fib, which = near_any_fib(price, hhv2, llv2, FIB_TOL)
                near_sup_block = sup is not None and price <= sup * (1 + SUP_BLOCK_NEAR)
                if near_fib and not near_sup_block:
                    if (float(closed["rsi"]) > float(prev["rsi"])) or (float(closed["macd_hist"]) > float(prev["macd_hist"])):
                        splus_ok = True
                        entry_tag = which
                        reasons.append(which)
        # (ج) مسار إنجيجمنت اختياري
        if not splus_ok and ENGAGEMENT_MODE:
            try:
                hhv_soft = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
                soft_break = price > hhv_soft * (1.0 + max(0.0009, eff_bb*0.8))
            except Exception:
                soft_break = False
            momentum_ok = (float(closed.get("macd_hist", 0.0)) >= 0) or (rvol >= 1.2)
            near_res_block = (res is not None) and (res * (1 - RES_BLOCK_NEAR) <= price <= res * (1 + RES_BLOCK_NEAR))
            if soft_break and momentum_ok and not near_res_block:
                splus_ok = True
                entry_tag = entry_tag or "Breakout (eng)"
                reasons.append("Engaged")

    # لا شيء صالح
    if not (s1_ok or g3_ok or splus_ok):
        return None

    # ========= بناء الأهداف/الوقف حسب الاستراتيجية =========
    profile = _decide_profile_from_df(df) if ENTRY_PROFILE == "auto" else ENTRY_PROFILE.lower()
    sl, tp1, tp2, tp3 = (0,0,0,0)
    strategy_code = "S+"
    max_bars_to_tp1 = MAX_BARS_TO_TP1

    # SL حماية إضافية بسوينغ لو
    def _protect_sl_with_swing(sl_in: float) -> float:
        try:
            swing_low = float(df.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
            if swing_low < price:
                return min(sl_in, swing_low)
        except Exception:
            pass
        return sl_in

    if s1_ok:
        # S1: هدف واحد محافظ ~0.5R — نجعل tp1=tp2=tp3
        base_sl = price - max(atr * 0.8, price * 0.002)  # حد أدنى ضيق قليلًا
        sl = _protect_sl_with_swing(base_sl)
        sl, tp1, tp2, tp3 = _build_targets_s1(price, sl)
        strategy_code = "S1"
        # وقت تعرّض أقصر لبلوغ TP1
        max_bars_to_tp1 = min(MAX_BARS_TO_TP1, 5)

    elif g3_ok:
        # G3: ثلاث أهداف قريبة (نستخدم vpc3 افتراضيًا إن توافر ATR)
        sl, tp1, tp2, tp3 = _build_targets(price, atr, "vpc3" if atr > 0 else "msb3")
        strategy_code = "G3"

    else:
        # S+: كما هو
        sl, tp1, tp2, tp3 = _build_targets(price, atr, profile)
        strategy_code = "S+"

    # تحقق ترتيب الأهداف
    if not (sl < price < tp1 <= tp2 <= tp3):
        return None

    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # أسباب/كونفلوينس
    if price > float(closed["ema50"]): reasons.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons.append("EMA9>EMA21")
    if rev_hammer: reasons.append("Hammer")
    if rev_engulf: reasons.append("Bull Engulf")
    if rev_insideb: reasons.append("InsideBreak")
    if had_sweep: reasons.append("Sweep")
    if strategy_code == "S1": reasons.append("MSS")
    if near_res: reasons.append("R→S")
    reasons.append(f"RVOL≥{round(max(eff_rvol_min, RVOL_MIN_STRICT if STRICT_MODE else eff_rvol_min),2)}")
    if entry_tag: reasons.append(entry_tag)
    confluence = reasons[:6]

    # رسائل تحفيزية
    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1":   MOTIVATION["tp1"].format(symbol=symbol),
        "tp2":   MOTIVATION["tp2"].format(symbol=symbol),
        "tp3":   MOTIVATION["tp3"].format(symbol=symbol),
        "sl":    MOTIVATION["sl"].format(symbol=symbol),
        "time":  MOTIVATION["time"].format(symbol=symbol),
    }

    return {
        "symbol": symbol,
        "side": "LONG",                      # توحيدًا مع تنسيق الرسائل في البوت
        "entry": round(price, 6),
        "sl":    round(sl, 6),
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp3":   round(tp3, 6),
        "tp_final": round(tp3, 6),

        "atr":   round(atr, 6),
        "r":     round(price - sl, 6),
        "score": 65,                         # يمكن معايرته لاحقًا
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
        },

        # إدارة لاحقة
        "partials": PARTIAL_FRACTIONS,
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": TRAIL_AFTER_TP2_ATR if TRAIL_AFTER_TP2 else None,
        "max_bars_to_tp1": max_bars_to_tp1 if USE_MAX_BARS_TO_TP1 else None,
        "cooldown_after_sl_min": COOLDOWN_AFTER_SL_MIN,
        "cooldown_after_tp_min": COOLDOWN_AFTER_TP_MIN,

        # بروفايل ورسائل + وسم الاستراتيجية
        "profile": profile,
        "strategy_code": strategy_code,      # ← مهم لعرض الرمز في القناة
        "messages": messages,

        "timestamp": datetime.utcnow().isoformat()
    }

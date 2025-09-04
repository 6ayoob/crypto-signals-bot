# strategy.py — SCALP+ (S/R + Fibo + Reversal + MTF) مع 3 أهداف ورسائل تحفيزية
# الدالة الرئيسة:
#   check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[List[list]] = None) -> Optional[dict]
# تُعيد None عند عدم وجود إشارة؛ أو dict بخصائص الصفقة المتوافقة مع bot.py.

from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd

# ========= حساسية/سيولة/تذبذب =========
MIN_QUOTE_VOL   = 20_000
RVOL_MIN_HARD   = 0.90
ATR_PCT_MIN     = 0.0015
ATR_PCT_MAX     = 0.06
HOLDOUT_BARS    = 2  # عدد الشموع الدنيا بين إشارتين على نفس الرمز

# ========= مؤشرات أساسية =========
EMA_FAST, EMA_SLOW, EMA_TREND, EMA_LONG = 9, 21, 50, 200
VOL_MA, ATR_PERIOD = 20, 14

# ========= S/R =========
USE_SR             = True
SR_WINDOW          = 40
RES_BLOCK_NEAR     = 0.004   # عدم الشراء داخل بلوك مقاومة قريب
SUP_BLOCK_NEAR     = 0.003   # تجنّب الشراء إذا كنا فوق دعم لصيق
BREAKOUT_BUFFER    = 0.0015  # هامش اختراق

# ========= Fibonacci =========
USE_FIB           = True
SWING_LOOKBACK    = 60
FIB_LEVELS        = (0.382, 0.618)
FIB_TOL           = 0.004

# ========= MACD/RSI Policy =========
MACD_RSI_POLICY   = "balanced"  # "lenient" | "balanced" | "strict"

# نافذة منع تكرار الإشارة على نفس الشمعة
_LAST_ENTRY_BAR_TS: Dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: Dict[str, int] = {}

# ========= إعدادات الأهداف/الوقف + ملفات التعريف =========
ENTRY_PROFILE = "auto"   # "auto" | "msb3" | "vpc3" | "dal3"

# MSB3 (Breakout سكالب) — نسب ثابتة قريبة
FIXED_TP_PCTS     = [0.008, 0.016, 0.024]  # 0.8% / 1.6% / 2.4%
FIXED_SL_PCT_MAX  = 0.009                  # 0.9% سقف وقف نسبي

# VPC3 (Pullback ترند)
VPC3_TP_PCTS = [0.007, 0.013, 0.020]
VPC3_SL_ATR  = 0.8

# DAL3 (ديناميكي ATR)
DAL3_TP_ATR = [1.0, 1.8, 2.8]
DAL3_SL_ATR = 0.9

# تقسيم الكمية (للاستخدام الخارجي)
PARTIAL_FRACTIONS = [0.40, 0.35, 0.25]

# تريلينغ بعد TP2
TRAIL_AFTER_TP2     = True
TRAIL_AFTER_TP2_ATR = 1.0   # SL = max(SL, close - 1×ATR)

# خروج زمني إن لم يُصب TP1 سريعًا (6 شموع 5m ≈ 30 دقيقة)
USE_MAX_BARS_TO_TP1 = True
MAX_BARS_TO_TP1     = 6

# تبريد بعد النتائج (تُستخدم من مدير الصفقات الخارجي)
COOLDOWN_AFTER_SL_MIN = 15
COOLDOWN_AFTER_TP_MIN = 5

# رسائل تحفيزية جاهزة
MOTIVATION = {
    "entry": "🔥 دخول {symbol}! خطة ثلاثية الأهداف — خطوة محسوبة 💪",
    "tp1":   "🎯 TP1 على {symbol}! ثبّت جزءًا وانقل SL للتعادل — مستمرّون 👟",
    "tp2":   "🚀 TP2 على {symbol}! فعّلنا تريلينغ لحماية المكسب — قريبون من الختام 🏁",
    "tp3":   "🏁 TP3 على {symbol}! إغلاق جميل — صفقة مكتملة ✨",
    "sl":    "🛑 SL على {symbol}. حماية رأس المال أولًا — فرص أقوى قادمة 🔄",
    "time":  "⌛ خروج زمني على {symbol} — لم تتفعّل الحركة سريعًا، خرجنا بخفّة 🔎",
}

# ========= Engagement Mode (اختياري) =========
ENGAGEMENT_MODE        = False          # عند تفعيله يزيد عدد الإشارات بحدود منضبطة
ENG_POLICY_OVERRIDE    = True
ENG_RVOL_MIN_HARD      = 0.85
ENG_ATR_PCT_MIN        = 0.0012
ENG_BREAKOUT_BUFFER    = 0.0012
ENG_HOLDOUT_BARS       = 1

# ========= Strict Filters لتعزيز الثقة =========
STRICT_MODE            = True
STRICT_EMA_STACK       = True       # EMA9>EMA21>EMA50
RVOL_MIN_STRICT        = 1.05
STRICT_BODY_PCT_MIN    = 0.55
MAX_UPWICK_PCT         = 0.35
MTF_FILTER_ENABLED     = True       # تأكيد إطار أعلى (مثلاً 15m)
MTF_REQUIRE_EMA_TREND  = True

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

# ---------- فلاتر جودة/شموع ----------
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

# ---------- S/R & Fibo ----------
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

# ---------- شموع انعكاسية (Bullish) ----------
def _body(o, c): return abs(c - o)
def _range(h, l): return max(h - l, 1e-9)

def is_bullish_engulfing(prev, curr) -> bool:
    po, pc = float(prev["open"]), float(prev["close"])
    co, cc = float(curr["open"]), float(curr["close"])
    return (pc < po) and (cc > co) and (_body(co, cc) > _body(po, pc)) and (co <= pc) and (cc >= po)

def is_hammer(row) -> bool:
    o, c, h, l = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
    rng = _range(h, l)
    body = _body(o, c)
    lower = min(o, c) - l
    upper = h - max(o, c)
    return (c >= o) and (lower >= 2.5 * body) and (upper <= body) and (body / rng >= 0.15)

def is_morning_star(prev2, prev1, curr) -> bool:
    p2o, p2c = float(prev2["open"]), float(prev2["close"])
    p1o, p1c = float(prev1["open"]), float(prev1["close"])
    co, cc   = float(curr["open"]), float(curr["close"])
    red_big  = (p2c < p2o) and (_body(p2o, p2c) / _range(float(prev2["high"]), float(prev2["low"])) >= 0.5)
    small    = (_body(p1o, p1c) / _range(float(prev1["high"]), float(prev1["low"])) <= 0.25)
    green    = (cc > co)
    mid_p2   = (p2o + p2c) / 2.0
    return red_big and small and green and (cc > mid_p2)

def detect_reversal(prev2, prev1, curr) -> Tuple[bool, str]:
    try:
        if is_bullish_engulfing(prev1, curr):  return True, "Bullish Engulfing"
        if is_hammer(curr):                    return True, "Hammer"
        if is_morning_star(prev2, prev1, curr):return True, "Morning Star"
    except Exception:
        pass
    return False, ""

# ---------- فلتر إطار أعلى ----------
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

# ---------- MACD/RSI Gate ----------
def macd_rsi_gate(prev_row, closed_row) -> Tuple[bool, list]:
    reasons = []
    rsi_now = float(closed_row["rsi"])
    rsi_up  = rsi_now > float(prev_row["rsi"])
    macd_h_now  = float(closed_row["macd_hist"])
    macd_h_prev = float(prev_row["macd_hist"])
    macd_pos    = macd_h_now > 0
    macd_up     = macd_h_now > macd_h_prev

    ok_flags = []
    if rsi_now > 50: ok_flags.append("RSI>50")
    if rsi_up:       ok_flags.append("RSI↑")
    if macd_pos:     ok_flags.append("MACD_hist>0")
    if macd_up:      ok_flags.append("MACD_hist↑")

    k = len(ok_flags)
    policy = MACD_RSI_POLICY
    if policy == "lenient":
        ok = k >= 1
    elif policy == "strict":
        ok = ("RSI>50" in ok_flags) and ("MACD_hist>0" in ok_flags) and ("MACD_hist↑" in ok_flags)
    else:
        ok = k >= 2

    if ok:
        reasons.extend(ok_flags[:2])
    return ok, reasons

# ---------- اختيار البروفايل تلقائيًا ----------
def _decide_profile_from_df(df) -> str:
    closed = df.iloc[-2]
    price  = float(closed["close"])
    atr    = float(df["atr"].iloc[-2]) if "atr" in df else 0.0
    atr_pct = (atr / price) if price > 0 else 0.0

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

# ---------- المولّد الرئيسي للإشارة ----------
def check_signal(symbol: str, ohlcv: List[list], ohlcv_htf: Optional[List[list]] = None) -> Optional[Dict]:
    """
    يُعاد dict مثل:
      {
        'symbol','side','entry','sl','tp1','tp2','tp3','tp_final',
        'atr','r','score','regime','reasons','features', 'partials', ...
      }
    """
    if not ohlcv or len(ohlcv) < 80:
        return None

    # بناء الداتا فريم
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if len(df) < 60:
        return None

    df = add_indicators(df)
    if len(df) < 60:
        return None

    # بار مغلق (قبل الأخير)
    prev2  = df.iloc[-4] if len(df) >= 4 else df.iloc[-3]
    prev1  = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # إعدادات فعّالة لوضع Engagement
    eff_holdout  = ENG_HOLDOUT_BARS if ENGAGEMENT_MODE else HOLDOUT_BARS
    eff_rvol_min = ENG_RVOL_MIN_HARD if ENGAGEMENT_MODE else RVOL_MIN_HARD
    eff_atr_min  = ENG_ATR_PCT_MIN if ENGAGEMENT_MODE else ATR_PCT_MIN
    eff_bb       = ENG_BREAKOUT_BUFFER if ENGAGEMENT_MODE else BREAKOUT_BUFFER

    # منع تكرار على نفس الشمعة/الفاصل
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts:
        return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < eff_holdout:
        return None

    # سيولة + ATR%
    if price * float(closed["volume"]) < MIN_QUOTE_VOL:
        return None
    atr = float(df["atr"].iloc[-2])
    atr_pct = atr / max(price, 1e-9)
    if atr_pct < eff_atr_min or atr_pct > ATR_PCT_MAX:
        return None

    # شمعة خضراء واتجاه خفيف
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

    # MACD/RSI Gate — تبديل مؤقت للـ policy لوضع Engagement
    global MACD_RSI_POLICY
    _pol_prev = MACD_RSI_POLICY
    if ENGAGEMENT_MODE and ENG_POLICY_OVERRIDE:
        MACD_RSI_POLICY = "lenient"
    ok_mr, mr_reasons = macd_rsi_gate(prev1, closed)
    MACD_RSI_POLICY = _pol_prev
    if not ok_mr:
        return None

    reasons: List[str] = []
    reasons.extend(mr_reasons)

    # S/R
    sup = res = None
    if USE_SR:
        sup, res = get_sr_on_closed(df, SR_WINDOW)

    # نظام السوق (للمعلومة)
    regime = detect_regime(df)

    # فلتر إطار أعلى (إن توفّرت بياناته)
    if MTF_FILTER_ENABLED and ohlcv_htf:
        if not pass_mtf_filter(ohlcv_htf):
            return None

    # ===== منطق الدخول =====
    entry_ok = False
    entry_tag = ""

    # (1) اختراق مقاومة حديثة + هامش — مع تجنّب منطقة مقاومة لصيقة
    try:
        hhv = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
    except Exception:
        hhv = None
    if (hhv is not None) and (res is not None):
        breakout_ok = price > hhv * (1.0 + eff_bb)
        near_res_block = (res * (1 - RES_BLOCK_NEAR) <= price <= res * (1 + RES_BLOCK_NEAR))
        if breakout_ok and not near_res_block:
            entry_ok = True
            entry_tag = "Breakout SR"
            reasons.append("Breakout SR")

    # (2) ارتداد فيبو 0.382/0.618 مع تحسّن زخم
    if (not entry_ok) and USE_FIB:
        hhv2, llv2 = recent_swing(df, SWING_LOOKBACK)
        if hhv2 and llv2:
            near_fib, which = near_any_fib(price, hhv2, llv2, FIB_TOL)
            near_sup_block = (sup is not None) and (price <= sup * (1 + SUP_BLOCK_NEAR))
            if near_fib and not near_sup_block:
                if (float(closed["rsi"]) > float(prev1["rsi"])) or (float(closed["macd_hist"]) > float(prev1["macd_hist"])):
                    entry_ok = True
                    entry_tag = which
                    reasons.append(which)

    # (3) انعكاس سعري واضح قرب دعم أو فوق EMA50
    if not entry_ok:
        rev_ok, rev_name = detect_reversal(prev2, prev1, closed)
        if rev_ok:
            near_sup_area = (sup is not None) and (price >= sup) and (price <= sup * (1 + SUP_BLOCK_NEAR*1.3))
            above_ema50   = price > float(closed["ema50"])
            if (near_sup_area or above_ema50) and (float(closed["rsi"]) > float(prev1["rsi"])):
                entry_ok = True
                entry_tag = f"Reversal ({rev_name})"
                reasons.append(rev_name)

    # (4) مسار تعزيز الإشعارات — اختراق أخف بشرط زخم بسيط (إن ENGAGEMENT_MODE)
    if not entry_ok and ENGAGEMENT_MODE:
        try:
            hhv_soft = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
            soft_break = price > hhv_soft * (1.0 + max(0.0009, eff_bb*0.8))
        except Exception:
            soft_break = False
        momentum_ok = (float(closed.get("macd_hist", 0.0)) >= 0) or (rvol >= 1.2)
        near_res_block2 = (res is not None) and (res * (1 - RES_BLOCK_NEAR) <= price <= res * (1 + RES_BLOCK_NEAR))
        if soft_break and momentum_ok and not near_res_block2:
            entry_ok = True
            entry_tag = entry_tag or "Breakout (eng)"
            reasons.append("Engaged")

    if not entry_ok:
        return None

    # ===== اختيار البروفايل/الأهداف/الوقف =====
    profile = _decide_profile_from_df(df) if ENTRY_PROFILE == "auto" else str(ENTRY_PROFILE).lower()
    sl, tp1, tp2, tp3 = _build_targets(price, atr, profile)

    # حماية إضافية: ضع SL تحت swing_low القريب إن كان أقل
    try:
        swing_low = float(df.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
        if swing_low < price:
            sl = min(sl, swing_low)
    except Exception:
        pass

    # تحقق ترتيب منطقي
    if not (sl < price < tp1 < tp2 < tp3):
        return None

    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # أسباب مختصرة إضافية
    if price > float(closed["ema50"]): reasons.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons.append("EMA9>EMA21")
    reasons.append(f"RVOL≥{round((eff_rvol_min if not STRICT_MODE else RVOL_MIN_STRICT),2)}")
    if entry_tag: reasons.append(entry_tag)
    reasons = reasons[:6]

    # رسائل تحفيزية ليستعملها bot.py عند TP/SL
    messages = {
        "entry": MOTIVATION["entry"].format(symbol=symbol),
        "tp1":   MOTIVATION["tp1"].format(symbol=symbol),
        "tp2":   MOTIVATION["tp2"].format(symbol=symbol),
        "tp3":   MOTIVATION["tp3"].format(symbol=symbol),
        "sl":    MOTIVATION["sl"].format(symbol=symbol),
        "time":  MOTIVATION["time"].format(symbol=symbol),
    }

    return {
        "symbol":   symbol,
        "side":     "BUY",
        "entry":    round(price, 6),
        "sl":       round(sl, 6),
        "tp1":      round(tp1, 6),
        "tp2":      round(tp2, 6),
        "tp3":      round(tp3, 6),
        "tp_final": round(tp3, 6),

        "atr":      round(atr, 6),
        "r":        round(price - sl, 6),
        "score":    65,
        "regime":   regime,
        "reasons":  reasons,

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

        # لإدارة لاحقة
        "partials": PARTIAL_FRACTIONS,
        "trail_after_tp2": TRAIL_AFTER_TP2,
        "trail_atr_mult": TRAIL_AFTER_TP2_ATR if TRAIL_AFTER_TP2 else None,
        "max_bars_to_tp1": MAX_BARS_TO_TP1 if USE_MAX_BARS_TO_TP1 else None,
        "cooldown_after_sl_min": COOLDOWN_AFTER_SL_MIN,
        "cooldown_after_tp_min": COOLDOWN_AFTER_TP_MIN,

        "profile":  profile,
        "messages": messages,

        "timestamp": datetime.utcnow().isoformat()
    }

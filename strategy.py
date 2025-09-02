# strategy.py — SCALP+ (AUTO Profiles + 3 TPs + Motivational messages)
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
        if abs(price - lvl) / lvl <= tol:
            return True, name
    return False, ""

def detect_regime(df) -> str:
    c = df["close"]; e200 = df.get("ema200", None)
    if e200 is None or pd.isna(e200.iloc[-1]):
        e50 = df["ema50"]
        return "trend" if (c.iloc[-1] > e50.iloc[-1] and e50.diff(10).iloc[-1] > 0) else "mean"
    return "trend" if (c.iloc[-1] > e200.iloc[-1] and e200.diff(10).iloc[-1] > 0) else "mean"

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

    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    cur_ts = int(closed["timestamp"])
    price  = float(closed["close"])

    # منع تكرار + تبريد
    if _LAST_ENTRY_BAR_TS.get(symbol) == cur_ts:
        return None
    cur_idx = len(df) - 2
    if cur_idx - _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000) < HOLDOUT_BARS:
        return None

    # سيولة + تذبذب
    if price * float(closed["volume"]) < MIN_QUOTE_VOL:
        return None
    atr = float(df["atr"].iloc[-2])
    atr_pct = atr / max(price, 1e-9)
    if atr_pct < ATR_PCT_MIN or atr_pct > ATR_PCT_MAX:
        return None

    # شمعة خضراء واتجاه خفيف
    if not (price > float(closed["open"])):
        return None
    if not ((float(closed["ema9"]) > float(closed["ema21"])) or (price > float(closed["ema50"]))):
        return None

    # RVOL مخفف
    vma = float(closed["vol_ma20"]) if not pd.isna(closed["vol_ma20"]) else 0.0
    rvol = (float(closed["volume"]) / (vma + 1e-9)) if vma > 0 else 0.0
    if rvol < RVOL_MIN_HARD:
        return None

    # MACD/RSI Gate
    ok_mr, mr_reasons = macd_rsi_gate(prev, closed)
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

    # ===== منطق الدخول: اختراق أو ارتداد فيبو =====
    entry_ok = False
    entry_tag = ""

    # (أ) اختراق مقاومة 40 شمعة + هامش صغير
    if res is not None:
        hhv = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
        breakout_ok = price > hhv * (1.0 + BREAKOUT_BUFFER)
        near_res_block = (price >= res * (1 - RES_BLOCK_NEAR)) and (price <= res * (1 + RES_BLOCK_NEAR))
        if breakout_ok and not near_res_block:
            entry_ok = True
            entry_tag = "Breakout SR"
            reasons.append("Breakout SR")
        elif not breakout_ok and near_res_block:
            return None

    # (ب) ارتداد في منطقة 0.382–0.618
    if not entry_ok and USE_FIB:
        hhv, llv = recent_swing(df, SWING_LOOKBACK)
        if hhv and llv:
            near_fib, which = near_any_fib(price, hhv, llv, FIB_TOL)
            near_sup_block = sup is not None and price <= sup * (1 + SUP_BLOCK_NEAR)
            if near_fib and not near_sup_block:
                if (float(closed["rsi"]) > float(prev["rsi"])) or (float(closed["macd_hist"]) > float(prev["macd_hist"])):
                    entry_ok = True
                    entry_tag = which
                    reasons.append(which)
                else:
                    return None

    if not entry_ok:
        return None

    # ===== بروفايل وأهداف/وقف =====
    profile = _decide_profile_from_df(df) if ENTRY_PROFILE == "auto" else ENTRY_PROFILE.lower()
    sl, tp1, tp2, tp3 = _build_targets(price, atr, profile)

    # حماية بالـ swing_low
    try:
        swing_low = float(df.iloc[:-1]["low"].rolling(6, min_periods=3).min().iloc[-1])
        if swing_low < price:
            sl = min(sl, swing_low)
    except Exception:
        pass

    if not (sl < price < tp1 < tp2 < tp3):
        return None

    _LAST_ENTRY_BAR_TS[symbol] = cur_ts
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # أسباب مختصرة
    if price > float(closed["ema50"]): reasons.append("Price>EMA50")
    if float(closed["ema9"]) > float(closed["ema21"]): reasons.append("EMA9>EMA21")
    reasons.append(f"RVOL≥{round(RVOL_MIN_HARD,2)}")
    reasons.append(entry_tag or profile.upper())
    reasons = reasons[:6]

    # تجهيز رسائل تحفيزية ليستعملها كود التنفيذ/الإدارة
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
        "side": "BUY",
        "entry": round(price, 6),
        "sl":    round(sl, 6),
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp3":   round(tp3, 6),
        "tp_final": round(tp3, 6),

        "atr":   round(atr, 6),
        "r":     round(price - sl, 6),
        "score": 65,
        "regime": regime,
        "reasons": reasons,

        # خصائص إضافية
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
        "max_bars_to_tp1": MAX_BARS_TO_TP1 if USE_MAX_BARS_TO_TP1 else None,
        "cooldown_after_sl_min": COOLDOWN_AFTER_SL_MIN,
        "cooldown_after_tp_min": COOLDOWN_AFTER_TP_MIN,

        # بروفايل ورسائل
        "profile": profile,
        "messages": messages,

        "timestamp": datetime.utcnow().isoformat()
    }

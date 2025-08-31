# strategy.py — BUY محسّنة: اتجاه + زخم + حجم + ATR + S/R + Score + HTF + Trail
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import pandas as pd
import math

# ==== الإعدادات الأصلية ====
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 50
RSI_MIN, RSI_MAX = 50, 70
SR_WINDOW = 50
RESISTANCE_BUFFER = 0.005
SUPPORT_BUFFER    = 0.002
VOL_MA = 20

ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5
R_MULT_TP    = 2.0

P1_R = 0.6
P2_R = 1.0

MIN_SL_PCT = 0.006
MAX_SL_PCT = 0.04

# ==== إضافات V2 ====
EMA_LONG = 200                    # لاستخدامه في كشف النظام وتأكيد الاتجاه
MIN_SCORE = 70                    # عتبة قبول الإشارة
RVOL_L1, RVOL_L2 = 1.2, 1.5       # مستويات RVOL
TRAIL_ATR_TREND = 2.5
TRAIL_ATR_MEAN  = 1.8

# ==== تحسينات V2.1 (قابلة للتهيئة) ====
# سيولة دنيا بالدولار (تقريبية): volume * close >= هذا الحد
MIN_QUOTE_VOL = 10_000            # مثال: 10 آلاف USDT خلال الشمعة
# نطاق تذبذب (ATR% من السعر) المقبول
ATR_PCT_MIN = 0.002               # 0.2% حد أدنى
ATR_PCT_MAX = 0.03                # 3% حد أعلى
# RVOL أدنى إلزامي (بالإضافة إلى نقاط الـ score)
RVOL_MIN_HARD = 1.05
# تبريد إشارات: لا تُصدر أكثر من إشارة للرمز خلال N شموع
HOLDOUT_BARS = 6

_LAST_ENTRY_BAR_TS: dict[str, int] = {}
_LAST_SIGNAL_BAR_IDX: dict[str, int] = {}

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

def get_sr_on_closed(df, window=50) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < window + 3:
        return None, None
    df_prev = df.iloc[:-1]
    w = min(window, len(df_prev))
    # حماية من NaN مع min_periods
    resistance = df_prev["high"].rolling(w, min_periods=max(5, w//3)).max().iloc[-1]
    support    = df_prev["low"].rolling(w,  min_periods=max(5, w//3)).min().iloc[-1]
    if pd.isna(resistance) or pd.isna(support):
        return None, None
    return float(support), float(resistance)

# ---------- كشف نظام السوق ----------
def detect_regime(df) -> str:
    """Trend إذا السعر فوق EMA200 وميله الأخير صاعد، وإلا Mean."""
    c = df["close"]; e200 = df.get("ema200", None)
    if e200 is None or pd.isna(e200.iloc[-1]):
        # عند عدم توفر EMA200 (بيانات قليلة) نستخدم ميل EMA50 بديلًا
        e50 = df["ema50"]
        slope50 = e50.diff(10).iloc[-1]
        return "trend" if (c.iloc[-1] > e50.iloc[-1] and slope50 > 0) else "mean"
    slope200 = e200.diff(10).iloc[-1]
    return "trend" if (c.iloc[-1] > e200.iloc[-1] and slope200 > 0) else "mean"

# ---------- تقييم/نقاط الإشارة ----------
def compute_rvol(closed_row) -> float:
    v = float(closed_row["volume"])
    vma = float(closed_row["vol_ma20"]) if not pd.isna(closed_row["vol_ma20"]) else 0.0
    return v / (vma + 1e-9) if vma > 0 else 0.0

def score_signal(df, regime: str, df_htf: Optional[pd.DataFrame] = None) -> Tuple[int, list, dict]:
    """يرجع (score, reasons, features) — يستخدم الشمعة المغلقة الأخيرة"""
    closed = df.iloc[-2]
    prev   = df.iloc[-3]
    price  = float(closed["close"])

    e9,e21,e50,e200 = closed["ema9"], closed["ema21"], closed["ema50"], closed["ema200"]
    rsi_val = float(closed["rsi"])
    hist, hist_prev = float(closed["macd_hist"]), float(prev["macd_hist"])
    rvol = compute_rvol(closed)

    # SR context (تعريف نطاق داخلي إضافي)
    sup, res = get_sr_on_closed(df, SR_WINDOW)
    breakout = False
    near_support = False
    if sup is not None and res is not None:
        hhv = float(df.iloc[:-1]["high"].rolling(SR_WINDOW, min_periods=10).max().iloc[-1])
        llv = float(df.iloc[:-1]["low"].rolling(SR_WINDOW,  min_periods=10).min().iloc[-1])
        breakout = price > hhv * (1.0 + 0.001)
        near_support = price <= llv * (1.01)

    score, reasons = 0, []

    # اتجاه
    if price > e50: score += 10; reasons.append("Price>EMA50")
    if (e9 > e21) and (e21 > e50): score += 15; reasons.append("EMA9>21>50")
    if not pd.isna(e200) and price > e200: score += 10; reasons.append("Price>EMA200")

    # زخم
    if hist > 0 and hist > hist_prev: score += 10; reasons.append("MACD hist expanding")
    elif hist > 0: score += 5; reasons.append("MACD hist>0")
    if rsi_val > 50: score += 10; reasons.append("RSI>50")
    if rsi_val < 70: score += 5; reasons.append("RSI<70")

    # حجم (RVOL)
    if rvol >= RVOL_L1: score += 10; reasons.append(f"RVOL≥{RVOL_L1}")
    if rvol >= RVOL_L2: score += 5; reasons.append(f"RVOL≥{RVOL_L2}")

    # سلوك سعري حسب النظام
    if regime == "trend" and breakout:
        score += 20; reasons.append("Breakout 50H")
    if regime == "mean" and near_support and (float(prev["rsi"]) < 45 < rsi_val):
        score += 20; reasons.append("Mean-rev bounce")

    # تأكيد إطار أعلى (إن توفر)
    if df_htf is not None and len(df_htf) >= 60:
        htfc = df_htf.iloc[-2]
        if (htfc["close"] > htfc["ema50"]):
            score += 8; reasons.append("HTF close>EMA50")
        if (htfc["ema9"] > htfc["ema21"]):
            score += 7; reasons.append("HTF EMA9>21")
        # تعزيز طفيف إذا أيضًا فوق EMA200 على HTF
        try:
            if htfc["close"] > htfc["ema200"]:
                score += 3; reasons.append("HTF close>EMA200")
        except Exception:
            pass

    score = int(min(score, 100))
    features = {
        "ema9": float(e9), "ema21": float(e21), "ema50": float(e50), "ema200": float(e200) if not pd.isna(e200) else None,
        "rsi": rsi_val, "macd_hist": hist, "rvol": rvol,
        "breakout": breakout, "near_support": near_support
    }
    return score, reasons, features

# ---------- وقف متحرك ATR ----------
def trailing_stop_from_df(df, current_stop: float, regime: str) -> float:
    """
    حدّث الوقف المتحرك بأسلوب Chandelier: أعلى قمة 22 - ATR*k
    """
    h, c = df["high"], df["close"]
    atr = df["atr"]
    mult = TRAIL_ATR_TREND if regime == "trend" else TRAIL_ATR_MEAN
    chandelier = c.rolling(22, min_periods=5).max() - atr * mult
    new_stop = max(float(current_stop), float(chandelier.iloc[-1]))
    return round(new_stop, 6)

# ---------- مولّد الإشارة الرئيسي ----------
def check_signal(symbol: str, ohlcv_5m: List[list], ohlcv_15m: Optional[List[list]] = None) -> Optional[Dict]:
    """
    يعتمد على الشمعة المغلقة الأخيرة من إطار 5m. يمكن تمرير 15m كإطار أعلى لتأكيد إضافي.
    يحافظ على نفس واجهة الإرجاع المستخدمة في بقية المشروع.
    """
    if not ohlcv_5m or len(ohlcv_5m) < 80:
        return None

    df = pd.DataFrame(ohlcv_5m, columns=["timestamp","open","high","low","close","volume"])
    # تأكد أن الأعمدة float لتفادي casting لاحق
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    if len(df) < 60:
        return None

    df = add_indicators(df)
    if len(df) < 60:
        return None

    # مؤشرات الشمعة المغلقة
    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    last_ts_closed = int(closed["timestamp"])
    price = float(closed["close"])

    # منع التكرار على نفس الشمعة
    if _LAST_ENTRY_BAR_TS.get(symbol) == last_ts_closed:
        return None

    # تبريد إشارات لعدد N شموع
    last_idx = _LAST_SIGNAL_BAR_IDX.get(symbol, -10_000)
    cur_idx = len(df) - 2  # مؤشر الشمعة المغلقة
    if cur_idx - last_idx < HOLDOUT_BARS:
        return None

    # فلاتر أصلية (محافظة لضمان التوافق السابق)
    if price < float(closed["ema50"]):
        return None
    if not (RSI_MIN < float(closed["rsi"]) < RSI_MAX):
        return None
    if not pd.isna(closed["vol_ma20"]) and float(closed["volume"]) < float(closed["vol_ma20"]):
        return None
    if float(closed["close"]) <= float(closed["open"]):
        return None

    # فلتر سيولة بالدولار
    quote_vol = float(closed["volume"]) * price
    if quote_vol < MIN_QUOTE_VOL:
        return None

    # فلاتر S/R
    sup, res = get_sr_on_closed(df, SR_WINDOW)
    if sup is not None and res is not None:
        if price >= res * (1 - RESISTANCE_BUFFER):
            return None
        if price <= sup * (1 + SUPPORT_BUFFER):
            return None

    # تقاطع EMA9/21 + MACD
    crossed = (float(prev["ema9"]) < float(prev["ema21"])) and (float(closed["ema9"]) > float(closed["ema21"]))
    macd_ok = float(closed["macd"]) > float(closed["macd_signal"])
    if not (crossed and macd_ok):
        return None

    # نظام السوق
    regime = detect_regime(df)

    # إطار أعلى (اختياري)
    df_htf = None
    if ohlcv_15m and len(ohlcv_15m) >= 60:
        df_htf = pd.DataFrame(ohlcv_15m, columns=["timestamp","open","high","low","close","volume"])
        for col in ["open","high","low","close","volume"]:
            df_htf[col] = pd.to_numeric(df_htf[col], errors="coerce")
        df_htf = df_htf.dropna().reset_index(drop=True)
        if len(df_htf) >= 60:
            df_htf = add_indicators(df_htf)

    # حساب النقاط
    score, reasons, features = score_signal(df, regime, df_htf)
    if score < MIN_SCORE:
        return None

    # RVOL دنيا إلزامية
    if features["rvol"] < RVOL_MIN_HARD:
        return None

    # SL هجيني (ATR و/أو سوينغ لو)
    atr = float(df["atr"].iloc[-2])
    swing_low = float(df.iloc[:-1]["low"].rolling(10, min_periods=3).min().iloc[-1])

    # فلتر تذبذب: ATR% من السعر ضمن النطاق
    atr_pct = atr / max(price, 1e-9)
    if atr_pct < ATR_PCT_MIN or atr_pct > ATR_PCT_MAX:
        return None

    sl_atr = price - ATR_SL_MULT * atr
    sl_hybrid = min(sl_atr, swing_low)

    dist_pct = (price - sl_hybrid) / max(price, 1e-9)
    if dist_pct < MIN_SL_PCT:
        sl_hybrid = price * (1 - MIN_SL_PCT)
    elif dist_pct > MAX_SL_PCT:
        return None

    sl = float(sl_hybrid)
    R  = price - sl
    if R <= 0:
        return None

    # الأهداف
    tp1 = price + P1_R * R
    tp2 = price + P2_R * R
    tp_final = price + R_MULT_TP * R

    _LAST_ENTRY_BAR_TS[symbol] = last_ts_closed
    _LAST_SIGNAL_BAR_IDX[symbol] = cur_idx

    # معلومات إضافية للمخاطر/التنفيذ
    trail_mult = TRAIL_ATR_TREND if regime == "trend" else TRAIL_ATR_MEAN

    return {
        "symbol": symbol,
        "side": "BUY",
        "entry": round(price, 6),
        "sl":    round(sl, 6),
        "tp1":   round(tp1, 6),
        "tp2":   round(tp2, 6),
        "tp_final": round(tp_final, 6),
        "atr":   round(atr, 6),
        "r":     round(R, 6),
        "score": int(score),
        "regime": regime,
        "reasons": reasons[:6],             # مختصر
        "features": features,               # قيَم مهمة للشفافية/Audit
        "trail_atr_mult": trail_mult,       # لاستخدامه في التتبّع
        "timestamp": datetime.utcnow().isoformat()
    }

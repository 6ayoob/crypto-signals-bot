# strategy.py — توليد إشارة BUY بهدفين قريبين + SL (محسّن وثابت)
from datetime import datetime
from typing import Dict, List, Optional
import math
import pandas as pd

# -----------------------------
# معلمات قابلة للتعديل
# -----------------------------
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
RSI_PERIOD = 14
SR_WINDOW = 50

RESISTANCE_BUFFER = 0.005   # 0.5% تحت المقاومة
SUPPORT_BUFFER    = 0.002   # 0.2% فوق الدعم

VOL_LOOKBACK = 20           # متوسط حجم للتصفية
MIN_RSI = 50
MAX_RSI = 70

TP1_R_MULT = 0.8            # (entry - SL) * 0.8
TP2_R_MULT = 1.6            # (entry - SL) * 1.6
SWING_LOW_WINDOW = 10       # لاستخراج SL من آخر قاع

# -----------------------------
# مؤشرات
# -----------------------------
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    d = series.diff()
    gain = d.where(d > 0, 0.0)
    loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean().replace(0, 1e-9)
    rs = ag / al
    return 100 - (100 / (1 + rs))

def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    return df

def indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema9"] = ema(df["close"], EMA_FAST)
    df["ema21"] = ema(df["close"], EMA_SLOW)
    df["ema50"] = ema(df["close"], EMA_TREND)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df = macd(df)
    return df

# -----------------------------
# دعم ومقاومة
# -----------------------------
def get_sr(df: pd.DataFrame, window: int = SR_WINDOW) -> (Optional[float], Optional[float]):
    if len(df) < 5:
        return None, None
    prev = df.iloc[:-1]
    w = min(window, len(prev))
    res = float(prev["high"].rolling(w).max().iloc[-1])
    sup = float(prev["low"].rolling(w).min().iloc[-1])
    return sup, res

# -----------------------------
# فحص إشارة BUY
# -----------------------------
def check_signal(symbol: str, ohlcv: List[List[float]]) -> Optional[Dict]:
    """
    يعيد dict للإشارة أو None
    dict: {symbol, side, entry, sl, tp1, tp2, timestamp}
    """
    # بيانات كافية
    if not ohlcv or len(ohlcv) < max(60, SWING_LOW_WINDOW + 5):
        return None

    # بناء الداتا
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df = indicators(df).dropna().copy()
    if len(df) < max(60, SWING_LOW_WINDOW + 5):
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["close"])

    # فلتر حجم (اختياري: حجم الشمعة >= متوسط آخر N)
    if len(df) >= VOL_LOOKBACK:
        avg_vol = float(df["volume"].rolling(VOL_LOOKBACK).mean().iloc[-1])
        if avg_vol > 0 and float(last["volume"]) < avg_vol:
            # print(f"[{symbol}] رفض: حجم أقل من المتوسط")
            return None

    # اتجاه EMA50
    if price < float(last["ema50"]):
        # print(f"[{symbol}] رفض: أقل من EMA50")
        return None

    # RSI نطاق زخم
    rsi_v = float(last["rsi"])
    if not (MIN_RSI < rsi_v < MAX_RSI):
        # print(f"[{symbol}] رفض: RSI خارج النطاق")
        return None

    # دعم/مقاومة — ابتعد قليلًا عن الأطراف
    sup, res = get_sr(df)
    if sup and res:
        if price >= res * (1 - RESISTANCE_BUFFER):
            # print(f"[{symbol}] رفض: قرب مقاومة")
            return None
        if price <= sup * (1 + SUPPORT_BUFFER):
            # print(f"[{symbol}] رفض: قرب دعم")
            return None

    # تقاطع متوسطات + MACD تأكيد
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"]:
        # وقف خسارة: أدنى قاع آخر SWING_LOW_WINDOW شموع (قبل الشمعة الحالية بواحدة)
        swing_low = float(df["low"].rolling(SWING_LOW_WINDOW).min().iloc[-2])
        sl = swing_low

        # حماية: SL يجب أن يكون أقل من السعر الحالي بفارق منطقي
        if not (sl < price and (price - sl) / price >= 0.003):  # ≥0.3% فرق
            # print(f"[{symbol}] رفض: SL قريب جدًا/غير منطقي")
            return None

        # أهداف قريبة
        r = price - sl
        tp1 = price + r * TP1_R_MULT
        tp2 = price + r * TP2_R_MULT

        # حماية: تأكد أن الأهداف أعلى من الدخول
        if tp1 <= price or tp2 <= price:
            return None

        # تنسيق أرقام (6 خانات كافية لمعظم الأزواج USDT)
        def _r6(x): return float(f"{x:.6f}")

        return {
            "symbol": symbol,
            "side": "BUY",
            "entry": _r6(price),
            "sl": _r6(sl),
            "tp1": _r6(tp1),
            "tp2": _r6(tp2),
            "timestamp": datetime.utcnow().isoformat()
        }

    # لا توجد إشارة
    return None

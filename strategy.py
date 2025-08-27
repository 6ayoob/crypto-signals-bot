# strategy.py — توليد إشارة BUY بهدفين قريبين + SL
from datetime import datetime
from typing import Dict, List
import pandas as pd

def ema(series, period): return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    d = series.diff()
    gain = d.where(d > 0, 0.0)
    loss = -d.where(d < 0, 0.0)
    ag = gain.ewm(alpha=1/period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, adjust=False).mean().replace(0, 1e-9)
    rs = ag / al
    return 100 - (100 / (1 + rs))

def macd(df, fast=12, slow=26, signal=9):
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["macd"] = df["ema_fast"] - df["ema_slow"]
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    return df

def indicators(df):
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["ema50"] = ema(df["close"], 50)
    df["rsi"] = rsi(df["close"], 14)
    return macd(df)

def get_sr(df, window=50):
    if len(df) < 5: return None, None
    prev = df.iloc[:-1]
    w = min(window, len(prev))
    res = prev["high"].rolling(w).max().iloc[-1]
    sup = prev["low"].rolling(w).min().iloc[-1]
    return sup, res

def check_signal(symbol: str, ohlcv: list) -> Dict | None:
    """يعيد dict للإشارة أو None"""
    if not ohlcv or len(ohlcv) < 60: return None
    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df = indicators(df)
    last, prev = df.iloc[-1], df.iloc[-2]
    price = float(last["close"])

    # فلاتر
    if price < last["ema50"]: return None              # اتجاه صاعد
    if not (50 < last["rsi"] < 70): return None        # زخم مقبول
    sup, res = get_sr(df)
    if sup and res:
        if price >= res * 0.995 or price <= sup * 1.002:  # بعيد عن مناطق ضغط
            return None

    # تقاطع + MACD
    if prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"]:
        sl = float(df["low"].rolling(10).min().iloc[-2])
        tp1 = price + (price - sl) * 0.8   # هدف قريب 1
        tp2 = price + (price - sl) * 1.6   # هدف قريب 2
        return {
            "symbol": symbol, "side": "BUY",
            "entry": round(price, 6), "sl": round(sl, 6),
            "tp1": round(tp1, 6), "tp2": round(tp2, 6),
            "timestamp": datetime.utcnow().isoformat()
        }
    return None

# strategy.py — إشارة BUY محسّنة: اتجاه + زخم + حجم + ATR + S/R + أهداف 0.6R/1.0R/2.0R
from datetime import datetime
from typing import Dict, List, Optional
import pandas as pd

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

_LAST_ENTRY_BAR_TS = {}

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
    return df

def atr_series(df, period=14):
    c = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]).abs(), (df["high"]-c).abs(), (df["low"]-c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def add_indicators(df):
    df["ema9"]  = ema(df["close"], EMA_FAST)
    df["ema21"] = ema(df["close"], EMA_SLOW)
    df["ema50"] = ema(df["close"], EMA_TREND)
    df["rsi"]   = rsi(df["close"], 14)
    df["vol_ma20"] = df["volume"].rolling(VOL_MA).mean()
    df = macd_cols(df)
    df["atr"] = atr_series(df, ATR_PERIOD)
    return df

def get_sr_on_closed(df, window=50):
    if len(df) < window + 3:
        return None, None
    df_prev = df.iloc[:-1]
    w = min(window, len(df_prev))
    resistance = df_prev["high"].rolling(w).max().iloc[-1]
    support    = df_prev["low"].rolling(w).min().iloc[-1]
    return support, resistance

def check_signal(symbol: str, ohlcv_5m: List[list], ohlcv_15m: Optional[List[list]] = None) -> Optional[Dict]:
    if not ohlcv_5m or len(ohlcv_5m) < 80:
        return None

    df = pd.DataFrame(ohlcv_5m, columns=["timestamp","open","high","low","close","volume"])
    df = add_indicators(df)
    if len(df) < 60:
        return None

    prev   = df.iloc[-3]
    closed = df.iloc[-2]
    last_ts_closed = int(closed["timestamp"])

    if _LAST_ENTRY_BAR_TS.get(symbol) == last_ts_closed:
        return None

    price = float(closed["close"])

    if price < closed["ema50"]:
        return None
    if not (RSI_MIN < closed["rsi"] < RSI_MAX):
        return None
    if not pd.isna(closed["vol_ma20"]) and closed["volume"] < closed["vol_ma20"]:
        return None
    if closed["close"] <= closed["open"]:
        return None

    sup, res = get_sr_on_closed(df, SR_WINDOW)
    if sup and res:
        if price >= res * (1 - RESISTANCE_BUFFER):
            return None
        if price <= sup * (1 + SUPPORT_BUFFER):
            return None

    crossed = (prev["ema9"] < prev["ema21"]) and (closed["ema9"] > closed["ema21"])
    macd_ok = closed["macd"] > closed["macd_signal"]
    if not (crossed and macd_ok):
        return None

    atr = float(df["atr"].iloc[-2])
    swing_low = float(df.iloc[:-1]["low"].rolling(10).min().iloc[-1])

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

    tp1 = price + 0.6 * R
    tp2 = price + 1.0 * R
    tp_final = price + 2.0 * R

    _LAST_ENTRY_BAR_TS[symbol] = last_ts_closed

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
        "timestamp": datetime.utcnow().isoformat()
    }

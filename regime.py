# regime.py
import os

def _f(k, dflt):  # helper env
    v = os.getenv(k)
    if v is None: return dflt
    try: return float(v)
    except: return dflt

def detect_regime(btc_rvol: float, breadth: float) -> str:
    """يعيد 'chop' أو 'trend'"""
    mode = os.getenv("REGIME_MODE", "auto").lower()
    if mode == "trend": return "trend"
    if mode == "chop":  return "chop"
    rv_thr = _f("REGIME_CHOP_RVOL", 0.75)
    br_thr = _f("REGIME_CHOP_BREADTH", 0.55)
    if btc_rvol < rv_thr or breadth < br_thr:
        return "chop"
    return "trend"

def regime_thresholds(regime: str):
    rvol_min = _f("RVOL_MIN_TREND", 0.80) if regime=="trend" else _f("RVOL_MIN_CHOP", 0.60)
    min_bar_quote = _f("MIN_BAR_QUOTE_VOL_USD_TREND", 800.0) if regime=="trend" else _f("MIN_BAR_QUOTE_VOL_USD_CHOP", 500.0)
    cut = float(os.getenv("SCORE_CUTOFF_TREND" if regime=="trend" else "SCORE_CUTOFF_CHOP", 60))
    return rvol_min, min_bar_quote, cut

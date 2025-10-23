# scoring.py
import os, math

def clamp(x, lo, hi): return max(lo, min(hi, x))

def base_score(feats: dict, regime: str) -> float:
    score = 50.0
    rv = feats.get("rvol", 0.0)
    # مساهمة RVOL (من 0.6 إلى 1.2 يعطي +0..12 نقطة تقريباً)
    score += clamp((rv - 0.60) * 20.0, 0.0, 12.0)

    # محاذاة EMAs/VWAP/AVWAP
    if feats.get("align", False): score += 10
    else: score -= 6

    # close<=open عقوبة خفيفة
    if feats.get("close_le_open", False): score -= 5

    # زخم/سبايك بسيط
    z = feats.get("z_rvol", 0.0)
    if z >= float(os.getenv("RVOL_SPIKE_Z", "1.0")):
        score += 4

    return score

def apply_strat_bonus(strat: str, feats: dict) -> float:
    b = 0.0
    if strat == "ema_breakout":
        if feats.get("align", False) and feats.get("break_above_ema", False):
            b += 6
    elif strat == "avwap_reclaim":
        if feats.get("reclaim_vwap", False):
            b += 7
        if os.getenv("RECLAIM_USE_WICK","0") in ("1","true","yes"):
            if feats.get("wick_reclaim", False):
                b += 3
    elif strat == "impulse_pb":
        if feats.get("impulse_bar", False) and feats.get("shallow_retrace", False):
            b += 8
    return b

def hard_guards_ok(feats: dict, regime: str) -> bool:
    # سبريد/سليبيج/عمق/حجب/ATR%… لازم تكون مقبولة
    if feats.get("spread_bad") or feats.get("slippage_bad") or feats.get("depth_bad"):
        return False
    if feats.get("holdout", 0) > 0:
        return False
    # استثناء ATR%: اسمح به مع Spike وحجم مصغّر
    atr_out = feats.get("atr_outside", False)
    allow_spike = os.getenv("ALLOW_ATR_OUTSIDE_WITH_SPIKE","1") in ("1","true","yes")
    if atr_out and not (allow_spike and feats.get("z_rvol",0)>=float(os.getenv("RVOL_SPIKE_Z","1.0"))):
        return False
    return True

def size_multiplier_for_exceptions(feats: dict) -> float:
    m = 1.0
    if feats.get("atr_outside", False) and feats.get("z_rvol",0)>=float(os.getenv("RVOL_SPIKE_Z","1.0")):
        m *= float(os.getenv("EXC_POSITION_SIZE_MULT","0.5"))
    if feats.get("wick_reclaim", False) and os.getenv("RECLAIM_USE_WICK","0") in ("1","true","yes"):
        m *= float(os.getenv("RECLAIM_WICK_SIZE_MULT","0.5"))
    return m

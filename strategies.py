# strategies.py
import os

IMP_PB_LOOKBACK = int(os.getenv("IMP_PB_LOOKBACK","5"))
IMP_PB_MAX_RETRACE_PCT = float(os.getenv("IMP_PB_MAX_RETRACE_PCT","0.35"))

def extract_features(sym, bar, ctx) -> dict:
    """
    bar: أحدث شمعة + سياق (emas,vwap,avwap,vol,zscores,spread,depth,holdout,...)
    ctx: بيانات سوق عامة (rvol_btc,breadth)، ومؤشرات مسبقة الحساب لكل رمز
    """
    feats = {}
    feats["rvol"] = bar.rvol
    feats["z_rvol"] = bar.z_rvol
    feats["align"] = (bar.price>bar.ema20>bar.vwap) or (bar.price>bar.vwap>bar.ema20)
    feats["close_le_open"] = bar.close <= bar.open
    feats["break_above_ema"] = bar.cross_up_ema20

    # reclaim
    feats["reclaim_vwap"] = bar.reclaim_vwap_close
    feats["wick_reclaim"] = bar.reclaim_vwap_wick  # وفّرها إن موجودة

    # impulse + pullback
    feats["impulse_bar"] = bar.impulse_z >= float(os.getenv("RVOL_SPIKE_Z","1.0"))
    feats["shallow_retrace"] = (0.0 <= bar.retrace_from_impulse_pct <= IMP_PB_MAX_RETRACE_PCT and bar.pullback_low_above_ema20)

    # guards
    feats["spread_bad"] = bar.spread_pct > ctx.max_spread_pct
    feats["slippage_bad"] = bar.expected_slip_pct > ctx.max_slip_pct
    feats["depth_bad"] = bar.depth_usd_5bps < ctx.depth_min_usd
    feats["holdout"] = bar.holdout_days_remaining
    feats["atr_outside"] = not (ctx.atr_min_pct <= bar.atr_pct_outside <= ctx.atr_max_pct)
    return feats

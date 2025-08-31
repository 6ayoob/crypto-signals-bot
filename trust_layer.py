# trust_layer.py — طبقة ثقة للمشتركين: بطاقة، Audit ID، سجل JSONL، تقرير يومي
import json, time, hashlib
from pathlib import Path

def make_audit_id(symbol: str, entry: float, score: int) -> str:
    base = f"{time.strftime('%Y-%m-%d')}_{symbol}_{round(float(entry), 4)}_{int(score)}"
    h = hashlib.md5(base.encode()).hexdigest()[:6]
    return f"{base}_{h}"

def format_signal_card(sig: dict, risk_pct: float = 0.005, daily_cap_r: float = 2.0) -> str:
    sym = sig["symbol"]; sc = sig.get("score", 0); reg = sig.get("regime","?")
    px = sig["entry"]; sl = sig["sl"]; tp1 = sig["tp1"]; tp2 = sig["tp2"]; tp3 = sig["tp_final"]
    reasons = ", ".join(sig.get("reasons", [])[:6]) or "-"
    trail = sig.get("trail_atr_mult", 0)
    audit_id = make_audit_id(sym, px, sc)

    lines = [
        f"🔔 إشارة شراء — {sym}",
        f"Score: {sc}/100  | Regime: {reg}",
        f"Entry: {px}",
        f"SL: {sl}  (خطر {round(px - sl, 6)})",
        f"TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | Trail: {trail}×ATR",
        f"أسباب: {reasons}",
        f"سياسة المخاطر: {round(risk_pct*100,2)}% لكل صفقة | حد يومي: −{daily_cap_r}R | تبريد عند 3 خسائر",
        f"Audit ID: {audit_id}"
    ]
    return "\n".join(lines)

def _append_jsonl(path, obj):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def log_signal(sig: dict, status="opened", log_path="logs/signals_log.jsonl"):
    event = {
        "ts": int(time.time()),
        "audit_id": make_audit_id(sig["symbol"], sig["entry"], sig.get("score",0)),
        "symbol": sig["symbol"], "entry": sig.get("entry"), "sl": sig.get("sl"),
        "tp1": sig.get("tp1"), "tp2": sig.get("tp2"), "tp3": sig.get("tp_final"),
        "score": sig.get("score"), "regime": sig.get("regime"),
        "reasons": sig.get("reasons", [])[:6], "status": status
    }
    _append_jsonl(log_path, event)
    return event["audit_id"]

def log_close(audit_id: str, symbol: str, exit_px: float, r_multiple: float,
              reason="tp/stop/manual", log_path="logs/signals_log.jsonl"):
    event = {
        "ts": int(time.time()), "audit_id": audit_id, "symbol": symbol,
        "exit": exit_px, "r": r_multiple, "status": "closed", "reason": reason
    }
    _append_jsonl(log_path, event)

def summarize_day(log_path="logs/signals_log.jsonl"):
    import datetime
    today = time.strftime("%Y-%m-%d")
    opens, closes = [], []
    p = Path(log_path)
    if not p.exists(): return None
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                d = datetime.datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
                if d == today:
                    if e.get("status") == "closed": closes.append(e)
                    elif e.get("status") == "opened": opens.append(e)
            except: pass
    wins = [e for e in closes if e.get("r", 0) > 0]
    total_r = round(sum(e.get("r",0) for e in closes), 3)
    win_rate = round(100*len(wins)/max(1,len(closes)), 1)
    return {
        "signals": len(opens),
        "closed": len(closes),
        "win_rate": win_rate,
        "total_r": total_r,
        "best": max([e.get("r",0) for e in closes], default=0),
        "worst": min([e.get("r",0) for e in closes], default=0),
        "avg_score": round(sum(o.get("score",0) for o in opens)/max(1,len(opens)),1) if opens else 0
    }

# database.py — SQLAlchemy models & helpers (PostgreSQL/SQLite)
# تحديث: دعم entries/targets/stop_rule + last_hit_idx، وهجرة خفيفة آمنة.

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime,
    Float, Text, text, select, func
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("db")
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    return url

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL") or "sqlite:///local.db"
DATABASE_URL = _normalize_db_url(DATABASE_URL)

engine_kwargs = dict(pool_pre_ping=True, future=True)
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()

def _utcnow() -> datetime: return datetime.now(timezone.utc)

def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None: return None
    try:
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt

# ---------- Models ----------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True, unique=False, nullable=True)
    trial_used = Column(Boolean, default=False, nullable=False)
    end_at = Column(DateTime, nullable=True)             # UTC
    plan = Column(String(8), nullable=True)
    last_tx_hash = Column(String(128), nullable=True)
    first_paid_at = Column(DateTime, nullable=True)
    referral_code = Column(String(32), unique=False, index=True, nullable=True)
    referred_by = Column(BigInteger, index=True, nullable=True)
    ref_bonus_awarded = Column(Boolean, default=False, nullable=False)
    ref_bonus_days = Column(Integer, default=0, nullable=False)
    marketing_variant = Column(String(8), nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    side = Column(String(4), nullable=False)
    entry = Column(Float, nullable=False)
    sl    = Column(Float, nullable=False)
    tp1   = Column(Float, nullable=False)
    tp2   = Column(Float, nullable=False)
    tp_final   = Column(Float, nullable=True)
    audit_id   = Column(String(64), index=True, nullable=True)
    score      = Column(Integer, nullable=True)
    regime     = Column(String(16), nullable=True)
    reasons    = Column(Text, nullable=True)      # CSV بسيط
    qty        = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    r_multiple = Column(Float, nullable=True)
    # NEW
    entries_json   = Column(Text, nullable=True)  # JSON list[float]
    targets_json   = Column(Text, nullable=True)  # JSON list[float]
    stop_rule_json = Column(Text, nullable=True)  # JSON dict
    last_hit_idx   = Column(Integer, default=0, nullable=False)  # آخر هدف تم بلوغه (index يبدأ من 0)
    # حالة
    status = Column(String(8), default="open", index=True, nullable=False)  # open | closed
    result = Column(String(8), nullable=True)  # tp1 | tp2 | tp3 | tp4 | tp5 | sl
    opened_at = Column(DateTime, default=_utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

class Lock(Base):
    __tablename__ = "locks"
    name = Column(String(64), primary_key=True)
    holder = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

# ---------- Migration ----------
def _add_column_sql(table: str, col: str, dialect: str) -> str:
    if table == "users":
        mapping_pg = {
            "tg_user_id":"BIGINT","trial_used":"BOOLEAN DEFAULT FALSE","end_at":"TIMESTAMPTZ","plan":"VARCHAR(8)",
            "last_tx_hash":"VARCHAR(128)","first_paid_at":"TIMESTAMPTZ","referral_code":"VARCHAR(32)","referred_by":"BIGINT",
            "ref_bonus_awarded":"BOOLEAN DEFAULT FALSE","ref_bonus_days":"INTEGER DEFAULT 0","marketing_variant":"VARCHAR(8)",
            "last_seen_at":"TIMESTAMPTZ","created_at":"TIMESTAMPTZ NOT NULL DEFAULT NOW()","updated_at":"TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        }
        mapping_sq = {
            "tg_user_id":"BIGINT","trial_used":"BOOLEAN","end_at":"TIMESTAMP","plan":"VARCHAR(8)",
            "last_tx_hash":"VARCHAR(128)","first_paid_at":"TIMESTAMP","referral_code":"VARCHAR(32)","referred_by":"BIGINT",
            "ref_bonus_awarded":"BOOLEAN","ref_bonus_days":"INTEGER","marketing_variant":"VARCHAR(8)",
            "last_seen_at":"TIMESTAMP","created_at":"TIMESTAMP","updated_at":"TIMESTAMP",
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    elif table == "trades":
        mapping_pg = {
            "result":"VARCHAR(8)","opened_at":"TIMESTAMPTZ NOT NULL DEFAULT NOW()","closed_at":"TIMESTAMPTZ",
            "status":"VARCHAR(8) NOT NULL DEFAULT 'open'","tp_final":"DOUBLE PRECISION","audit_id":"VARCHAR(64)",
            "score":"INTEGER","regime":"VARCHAR(16)","reasons":"TEXT","qty":"DOUBLE PRECISION","exit_price":"DOUBLE PRECISION",
            "r_multiple":"DOUBLE PRECISION","created_at":"TIMESTAMPTZ NOT NULL DEFAULT NOW()","updated_at":"TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            # NEW:
            "entries_json":"TEXT","targets_json":"TEXT","stop_rule_json":"TEXT","last_hit_idx":"INTEGER DEFAULT 0",
        }
        mapping_sq = {
            "result":"VARCHAR(8)","opened_at":"TIMESTAMP","closed_at":"TIMESTAMP",
            "status":"VARCHAR(8)","tp_final":"REAL","audit_id":"VARCHAR(64)",
            "score":"INTEGER","regime":"VARCHAR(16)","reasons":"TEXT","qty":"REAL","exit_price":"REAL",
            "r_multiple":"REAL","created_at":"TIMESTAMP","updated_at":"TIMESTAMP",
            # NEW:
            "entries_json":"TEXT","targets_json":"TEXT","stop_rule_json":"TEXT","last_hit_idx":"INTEGER",
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    else:
        typ = "TEXT"
    if dialect == "postgresql":
        return f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col}" {typ};'
    return f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ};'

def _ensure_indexes(connection, dialect: str):
    if dialect == "postgresql":
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON "users" (tg_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_referred_by ON "users" (referred_by);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_referral_code ON "users" (referral_code);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_status ON "trades" (status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_opened_at ON "trades" (opened_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_audit_id ON "trades" (audit_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_symbol_status ON "trades" (symbol, status);'))
        try:
            connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_audit_id_not_null ON "trades" (audit_id) WHERE audit_id IS NOT NULL;'))
        except Exception: pass
    else:
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_referred_by ON users (referred_by);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_referral_code ON users (referral_code);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_status ON trades (status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_opened_at ON trades (opened_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_audit_id ON trades (audit_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_symbol_status ON trades (symbol, status);'))

def _dedupe_users_on_tg_user_id(connection, dialect: str):
    dup_sql = 'SELECT tg_user_id FROM "users" WHERE tg_user_id IS NOT NULL GROUP BY tg_user_id HAVING COUNT(*) > 1;' if dialect == "postgresql" else \
              'SELECT tg_user_id FROM users WHERE tg_user_id IS NOT NULL GROUP BY tg_user_id HAVING COUNT(*) > 1;'
    result = connection.execute(text(dup_sql)).fetchall()
    dups = [r[0] for r in result if r[0] is not None]
    for tg_id in dups:
        if dialect == "postgresql":
            del_sql = """
                DELETE FROM "users"
                WHERE tg_user_id = :tg AND id NOT IN (
                    SELECT id FROM "users" WHERE tg_user_id = :tg ORDER BY id ASC LIMIT 1
                );
            """
        else:
            del_sql = """
                DELETE FROM users
                WHERE tg_user_id = :tg AND id NOT IN (
                    SELECT id FROM users WHERE tg_user_id = :tg ORDER BY id ASC LIMIT 1
                );
            """
        connection.execute(text(del_sql), {"tg": tg_id})

def _ensure_unique_index_users_tg_user_id(connection, dialect: str):
    _dedupe_users_on_tg_user_id(connection, dialect)
    if dialect == "postgresql":
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tg_user_id ON "users" (tg_user_id);'))
    else:
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tg_user_id ON users (tg_user_id);'))

def _lightweight_migrate():
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    dialect = engine.dialect.name
    with engine.begin() as conn:
        # users
        try: cols_users = {c["name"] for c in insp.get_columns("users")}
        except Exception: cols_users = set()
        required_users = ["tg_user_id","trial_used","end_at","plan","last_tx_hash","first_paid_at","referral_code","referred_by","ref_bonus_awarded","ref_bonus_days","marketing_variant","last_seen_at","created_at","updated_at"]
        for c in required_users:
            if c not in cols_users:
                try: conn.execute(text(_add_column_sql("users", c, dialect)))
                except Exception as e: logger.warning(f"ALTER users ADD {c} failed: {e}")
        if dialect == "postgresql":
            try:
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "created_at" SET DEFAULT NOW();'))
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "updated_at" SET DEFAULT NOW();'))
                conn.execute(text('UPDATE "users" SET "updated_at" = COALESCE("updated_at", "created_at");'))
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "updated_at" SET NOT NULL;'))
            except Exception as e:
                logger.warning(f"users.updated_at default/not null adjust failed: {e}")

        # trades
        try: cols_trades = {c["name"] for c in insp.get_columns("trades")}
        except Exception: cols_trades = set()
        required_trades = ["result","opened_at","closed_at","status","tp_final","audit_id","score","regime","reasons","qty","exit_price","r_multiple","created_at","updated_at","entries_json","targets_json","stop_rule_json","last_hit_idx"]
        for c in required_trades:
            if c not in cols_trades:
                try: conn.execute(text(_add_column_sql("trades", c, dialect)))
                except Exception as e: logger.warning(f"ALTER trades ADD {c} failed: {e}")
        if dialect == "postgresql":
            try:
                conn.execute(text('ALTER TABLE "trades" ALTER COLUMN "opened_at" SET DEFAULT NOW();'))
                conn.execute(text('ALTER TABLE "trades" ALTER COLUMN "created_at" SET DEFAULT NOW();'))
                conn.execute(text('ALTER TABLE "trades" ALTER COLUMN "updated_at" SET DEFAULT NOW();'))
                conn.execute(text('UPDATE "trades" SET "created_at" = COALESCE("created_at", NOW()) WHERE "created_at" IS NULL;'))
                conn.execute(text('UPDATE "trades" SET "updated_at" = COALESCE("updated_at", "created_at");'))
                conn.execute(text('ALTER TABLE "trades" ALTER COLUMN "created_at" SET NOT NULL;'))
                conn.execute(text('ALTER TABLE "trades" ALTER COLUMN "updated_at" SET NOT NULL;'))
            except Exception as e:
                logger.warning(f"trades created/updated defaults adjust failed: {e}")

        # indexes
        try: _ensure_indexes(conn, dialect)
        except Exception as e: logger.warning(f"CREATE INDEX failed: {e}")

        # unique users.tg_user_id
        try: _ensure_unique_index_users_tg_user_id(conn, dialect)
        except Exception as e: logger.warning(f"Ensure unique users.tg_user_id failed: {e}")

def init_db():
    try:
        _lightweight_migrate()
        logger.info("db: Database ready.")
    except Exception as e:
        logger.exception(f"DB INIT ERROR: {e}")
        raise

@contextmanager
def get_session():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

# ---------- Subscription ----------
def _get_or_create_user(s, tg_user_id: int) -> User:
    users = s.execute(select(User).where(User.tg_user_id == tg_user_id).order_by(User.id.asc())).scalars().all()
    if not users:
        u = User(tg_user_id=tg_user_id, trial_used=False, end_at=None)
        s.add(u); s.flush()
        return u
    keep = users[0]
    for extra in users[1:]:
        try: s.delete(extra)
        except Exception: pass
    s.flush()
    return keep

def is_active(s, tg_user_id: int) -> bool:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u or not u.end_at: return False
    return (_as_aware(u.end_at) or _utcnow()) > _utcnow()

def start_trial(s, tg_user_id: int) -> bool:
    u = _get_or_create_user(s, tg_user_id)
    if u.trial_used: return False
    u.trial_used = True
    base = _utcnow()
    if u.end_at and _as_aware(u.end_at) and _as_aware(u.end_at) > base:
        base = _as_aware(u.end_at)
    u.end_at = base + timedelta(days=1)
    u.plan = "trial"
    return True

def approve_paid(s, tg_user_id: int, plan: str, duration: timedelta, tx_hash: Optional[str] = None) -> datetime:
    u = _get_or_create_user(s, tg_user_id)
    now = _utcnow()
    base = _as_aware(u.end_at) if (u.end_at and _as_aware(u.end_at) and _as_aware(u.end_at) > now) else now
    u.end_at = base + duration
    u.plan = plan
    if tx_hash: u.last_tx_hash = tx_hash
    if plan in ("2w","4w") and u.first_paid_at is None:
        u.first_paid_at = now
    s.flush()
    return u.end_at

# ---------- Referrals ----------
def _default_referral_code_for(uid: int) -> str: return f"ref_{uid}"

def ensure_referral_code(s, tg_user_id: int) -> str:
    u = _get_or_create_user(s, tg_user_id)
    if not u.referral_code:
        code = _default_referral_code_for(tg_user_id)
        exist = s.execute(select(User.id).where(User.referral_code == code)).first()
        if exist: code = f"{code}_{u.id}"
        u.referral_code = code; s.flush()
    return u.referral_code

def resolve_referrer_uid_by_code(s, code: str) -> Optional[int]:
    if not code: return None
    raw = code.strip()
    uid = None
    if raw.startswith("ref_") and raw[4:].isdigit():
        uid = int(raw[4:])
    elif raw.isdigit():
        uid = int(raw)
    if uid is not None:
        exists = s.execute(select(User.id).where(User.tg_user_id == uid)).first()
        if exists: return uid
    u = s.execute(select(User).where(User.referral_code == raw)).scalar_one_or_none()
    return int(u.tg_user_id) if (u and u.tg_user_id) else None

def link_referred_by(s, target_tg_user_id: int, ref_code: str) -> tuple[bool, str]:
    """
    يربط الإحالة مرة واحدة (idempotent).
    يرجع (ok, reason): 'already_linked' | 'invalid_or_self' | 'ok'
    """
    u = _get_or_create_user(s, target_tg_user_id)
    if u.referred_by:
        return False, "already_linked"
    ref_uid = resolve_referrer_uid_by_code(s, ref_code)
    if not ref_uid or int(ref_uid) == int(target_tg_user_id):
        return False, "invalid_or_self"
    u.referred_by = int(ref_uid)
    s.flush()
    return True, "ok"

def apply_referral_bonus_if_eligible(s, target_tg_user_id: int, bonus_days: int = 2) -> bool:
    u = _get_or_create_user(s, target_tg_user_id)
    if not u.referred_by or u.ref_bonus_awarded: return False
    if u.plan not in ("2w","4w"): return False
    u.end_at = (_as_aware(u.end_at) or _utcnow()) + timedelta(days=int(bonus_days))
    u.ref_bonus_days = int((u.ref_bonus_days or 0) + int(bonus_days))
    u.ref_bonus_awarded = True
    s.flush(); return True

def get_ref_stats(s, referrer_tg_user_id: int) -> Dict[str, Any]:
    joined = s.execute(
        select(func.count(User.id)).where(User.referred_by == referrer_tg_user_id)
    ).scalar() or 0
    paid = s.execute(
        select(func.count(User.id)).where(
            User.referred_by == referrer_tg_user_id, User.plan.in_(("2w", "4w"))
        )
    ).scalar() or 0
    bonus_days = s.execute(
        select(func.coalesce(func.sum(User.ref_bonus_days), 0)).where(User.referred_by == referrer_tg_user_id)
    ).scalar() or 0

    # المفاتيح المتوقعة في bot.py:
    out = {
        "referred_count": int(joined),
        "paid_count": int(paid),
        "total_bonus_days": int(bonus_days),
    }
    # مفاتيحك السابقة (توافق خلفي اختياري):
    out.update({
        "joined": out["referred_count"],
        "paid_converted": out["paid_count"],
        "total_bonus_days_distributed": out["total_bonus_days"],
    })
    return out

# ---------- Trades ----------
def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> int:
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2, status="open")
    s.add(t); s.flush(); return t.id

def add_trade_sig(s, sig: dict, audit_id: Optional[str] = None, qty: Optional[float] = None) -> int:
    if audit_id:
        exists = s.execute(select(Trade.id).where(Trade.audit_id == audit_id)).first()
        if exists: return int(exists[0])

    entries_json = json.dumps(sig.get("entries")) if sig.get("entries") else None
    targets_json = json.dumps(sig.get("targets")) if sig.get("targets") else None
    stop_rule_json = json.dumps(sig.get("stop_rule")) if sig.get("stop_rule") else None

    entry_value = float(sig["entry"])
    if sig.get("entries"):
        try:
            es = [float(x) for x in sig["entries"] if x is not None]
            if es: entry_value = sum(es)/len(es)
        except Exception:
            pass

    t = Trade(
        symbol=sig["symbol"], side=sig["side"],
        entry=float(entry_value), sl=float(sig["sl"]),
        tp1=float(sig["tp1"]), tp2=float(sig["tp2"]),
        tp_final=float(sig.get("tp_final")) if sig.get("tp_final") is not None else None,
        score=int(sig.get("score")) if sig.get("score") is not None else None,
        regime=str(sig.get("regime")) if sig.get("regime") is not None else None,
        reasons=",".join(sig.get("reasons", [])) if isinstance(sig.get("reasons"), list) else (sig.get("reasons") or None),
        audit_id=audit_id, qty=float(qty) if qty is not None else None,
        status="open", opened_at=_utcnow(),
        entries_json=entries_json, targets_json=targets_json, stop_rule_json=stop_rule_json, last_hit_idx=0
    )
    s.add(t); s.flush(); return t.id

def update_last_hit_idx(s, trade_id: int, idx: int):
    t = s.get(Trade, trade_id)
    if not t: return
    t.last_hit_idx = int(max(0, idx))
    t.updated_at = _utcnow()
    s.flush()

def has_open_trade_on_symbol(s, symbol: str) -> bool:
    return (s.execute(select(func.count(Trade.id)).where(Trade.symbol == symbol, Trade.status == "open")).scalar() or 0) > 0

def count_open_trades(s) -> int:
    return s.execute(select(func.count(Trade.id)).where(Trade.status == "open")).scalar() or 0

def close_trade(s, trade_id: int, result: str, exit_price: Optional[float] = None, r_multiple: Optional[float] = None):
    t = s.get(Trade, trade_id)
    if not t: return
    t.status = "closed"; t.result = result; t.closed_at = _utcnow()
    if exit_price is not None:
        try: t.exit_price = float(exit_price)
        except Exception: pass
    if r_multiple is not None:
        try: t.r_multiple = float(r_multiple)
        except Exception: pass
    s.flush()

def list_active_user_ids(s) -> List[int]:
    now = _utcnow()
    rows = s.execute(select(User.tg_user_id).where(User.end_at != None, User.end_at > now)).all()  # noqa: E711
    return [r[0] for r in rows if r[0]]

def list_users_expiring_within(s, hours: int = 4) -> List[int]:
    now = _utcnow(); soon = now + timedelta(hours=int(hours))
    rows = s.execute(select(User.tg_user_id).where(User.end_at != None, User.end_at > now, User.end_at <= soon)).all()
    return [r[0] for r in rows if r[0]]

def list_recent_paid(s, days: int = 7) -> List[int]:
    since = _utcnow() - timedelta(days=int(days))
    rows = s.execute(select(User.tg_user_id).where(User.first_paid_at != None, User.first_paid_at >= since)).all()
    return [r[0] for r in rows if r[0]]

# ---------- Helpers for targets/entries ----------
def trade_targets_list(t: Trade) -> List[float]:
    try:
        if t.targets_json:
            arr = json.loads(t.targets_json)
            arr = [float(x) for x in arr if x is not None]
            if arr: return arr
    except Exception:
        pass
    out = [t.tp1, t.tp2]
    if t.tp_final and t.tp_final > max(out): out.append(t.tp_final)
    return [float(x) for x in out]

def trade_entries_list(t: Trade) -> Optional[List[float]]:
    try:
        if t.entries_json:
            arr = json.loads(t.entries_json)
            arr = [float(x) for x in arr if x is not None]
            return arr if arr else None
    except Exception:
        pass
    return None

# ---------- Reports ----------
def _period_stats(s, since: datetime) -> dict:
    signals = s.execute(select(func.count(Trade.id)).where(Trade.opened_at >= since)).scalar() or 0
    open_now = s.execute(select(func.count(Trade.id)).where(Trade.status == "open")).scalar() or 0
    tp1 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp1", Trade.closed_at >= since)).scalar() or 0
    tp2 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp2", Trade.closed_at >= since)).scalar() or 0
    tp3 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp3", Trade.closed_at >= since)).scalar() or 0
    tp4 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp4", Trade.closed_at >= since)).scalar() or 0
    tp5 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp5", Trade.closed_at >= since)).scalar() or 0
    sl  = s.execute(select(func.count(Trade.id)).where(Trade.result == "sl",  Trade.closed_at >= since)).scalar() or 0

    r_sum = 0.0
    for t in s.execute(select(Trade).where(Trade.status == "closed", Trade.closed_at >= since)).scalars().all():
        if t.r_multiple is not None:
            r_sum += float(t.r_multiple); continue
        try:
            risk = max(float(t.entry) - float(t.sl), 1e-9)
            if t.result in ("tp1","tp2","tp3","tp4","tp5"):
                tgt = trade_targets_list(t)
                hit = {"tp1":0,"tp2":1,"tp3":2,"tp4":3,"tp5":4}.get(t.result, 0)
                hit = min(hit, len(tgt)-1)
                r_sum += (float(tgt[hit]) - float(t.entry)) / risk
            elif t.result == "sl":
                r_sum += -1.0
        except Exception:
            pass

    wins = tp1 + tp2 + tp3 + tp4 + tp5
    losses = sl
    total = wins + losses
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0

    return {"signals": signals, "open": open_now, "tp1": tp1, "tp2": tp2, "tp_total": wins, "sl": sl, "win_rate": win_rate, "r_sum": round(r_sum, 2)}

def get_stats_24h(s) -> dict: return _period_stats(s, _utcnow() - timedelta(hours=24))
def get_stats_7d(s) -> dict:  return _period_stats(s, _utcnow() - timedelta(days=7))

# ---------- Leader Lock ----------
class Dummy: pass

def try_acquire_leader_lock(name: str, holder: str) -> bool:
    with SessionLocal() as s:
        if s.get(Lock, name): return False
        s.add(Lock(name=name, holder=holder))
        try: s.commit(); return True
        except IntegrityError: s.rollback(); return False

def acquire_or_steal_leader_lock(name: str, holder: str, ttl_seconds: int = 300) -> bool:
    now = _utcnow(); expiry = now - timedelta(seconds=int(ttl_seconds))
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row is None:
            s.add(Lock(name=name, holder=holder, created_at=now))
            try: s.commit(); return True
            except Exception: s.rollback(); return False
        row_ct = _as_aware(row.created_at)
        if row_ct is not None and row_ct < expiry:
            row.holder = holder; row.created_at = now
            try: s.commit(); return True
            except Exception: s.rollback(); return False
        return False

def heartbeat_leader_lock(name: str, holder: str) -> bool:
    now = _utcnow()
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row is None or row.holder != holder: return False
        row.created_at = now
        try: s.commit(); return True
        except Exception: s.rollback(); return False

def release_leader_lock(name: str, holder: str) -> None:
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row and row.holder == holder:
            s.delete(row)
            try: s.commit()
            except Exception: s.rollback()

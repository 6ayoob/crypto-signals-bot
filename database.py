# database.py — SQLAlchemy models & helpers (PostgreSQL/SQLite)
# متوافق مع bot.py V2 + Leader Lock (TTL) — تحسينات: منع تكرار TxID (payments)، فهارس إضافية، UTC صارم.

import os
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime,
    Float, Text, text, select, func, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("db")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ---------------------------
# اتصال قاعدة البيانات
# ---------------------------
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

# ---------------------------
# Helpers (وقت UTC موحّد)
# ---------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """حوّل أي datetime إلى UTC-aware (يفترض UTC إن كان naive)."""
    if dt is None:
        return None
    try:
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt

# ---------------------------
# النماذج (Models)
# ---------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True, unique=False, nullable=True)
    trial_used = Column(Boolean, default=False, nullable=False)
    end_at = Column(DateTime, nullable=True)             # UTC
    plan = Column(String(8), nullable=True)              # "trial" | "2w" | "4w"
    last_tx_hash = Column(String(128), nullable=True)    # رقم المرجع/Tx (آخر عملية)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    # أساسية
    symbol = Column(String(20), index=True, nullable=False)
    side = Column(String(4), nullable=False)   # "BUY"
    entry = Column(Float, nullable=False)
    sl    = Column(Float, nullable=False)
    tp1   = Column(Float, nullable=False)
    tp2   = Column(Float, nullable=False)
    # إضافية V2
    tp_final   = Column(Float, nullable=True)
    audit_id   = Column(String(64), index=True, nullable=True)
    score      = Column(Integer, nullable=True)
    regime     = Column(String(16), nullable=True)
    reasons    = Column(Text, nullable=True)           # JSON/CSV نصي
    qty        = Column(Float, nullable=True)          # كمية اختيارية
    exit_price = Column(Float, nullable=True)          # سعر الإغلاق الفعلي
    r_multiple = Column(Float, nullable=True)          # R المحسوب عند الإغلاق
    # حالة
    status = Column(String(8), default="open", index=True, nullable=False)  # open | closed
    result = Column(String(8), nullable=True)  # tp1 | tp2 | sl
    opened_at = Column(DateTime, default=_utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)

# قفل القيادة (Leader)
class Lock(Base):
    __tablename__ = "locks"
    name = Column(String(64), primary_key=True)
    holder = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

# جـدول المدفوعات (لمنع تكرار TxID + تتبّع مبالغ)
class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True, nullable=False)
    plan = Column(String(8), nullable=True)           # "2w" | "4w" | None
    amount_usdt = Column(Float, nullable=True)
    tx_hash = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("tx_hash", name="uq_payments_tx_hash"),
    )

# ---------------------------
# تهيئة + هجرة خفيفة (إضافة أعمدة/فهارس)
# ---------------------------
def _add_column_sql(table: str, col: str, dialect: str) -> str:
    if table == "users":
        mapping_pg = {
            "tg_user_id": "BIGINT",
            "trial_used": "BOOLEAN DEFAULT FALSE",
            "end_at": "TIMESTAMPTZ",
            "plan": "VARCHAR(8)",
            "last_tx_hash": "VARCHAR(128)",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()"
        }
        mapping_sq = {
            "tg_user_id": "BIGINT",
            "trial_used": "BOOLEAN",
            "end_at": "TIMESTAMP",
            "plan": "VARCHAR(8)",
            "last_tx_hash": "VARCHAR(128)",
            "created_at": "TIMESTAMP"
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    elif table == "trades":
        mapping_pg = {
            "result": "VARCHAR(8)",
            "opened_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "closed_at": "TIMESTAMPTZ",
            "status": "VARCHAR(8) NOT NULL DEFAULT 'open'",
            "tp_final": "DOUBLE PRECISION",
            "audit_id": "VARCHAR(64)",
            "score": "INTEGER",
            "regime": "VARCHAR(16)",
            "reasons": "TEXT",
            "qty": "DOUBLE PRECISION",
            "exit_price": "DOUBLE PRECISION",
            "r_multiple": "DOUBLE PRECISION",
        }
        mapping_sq = {
            "result": "VARCHAR(8)",
            "opened_at": "TIMESTAMP",
            "closed_at": "TIMESTAMP",
            "status": "VARCHAR(8)",
            "tp_final": "REAL",
            "audit_id": "VARCHAR(64)",
            "score": "INTEGER",
            "regime": "VARCHAR(16)",
            "reasons": "TEXT",
            "qty": "REAL",
            "exit_price": "REAL",
            "r_multiple": "REAL",
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    else:
        typ = "TEXT"

    if dialect == "postgresql":
        return f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col}" {typ};'
    return f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ};'  # SQLite لا يدعم IF NOT EXISTS للأعمدة

def _ensure_indexes(connection, dialect: str):
    # فهارس إضافية مفيدة للاستعلامات
    if dialect == "postgresql":
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON "users" (tg_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_end_at ON "users" (end_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_plan ON "users" (plan);'))

        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_status ON "trades" (status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_opened_at ON "trades" (opened_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_audit_id ON "trades" (audit_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_symbol_status ON "trades" (symbol, status);'))

        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_payments_user ON "payments" (tg_user_id);'))
        # قيْد uq_payments_tx_hash مُعرّف على الموديل

def _lightweight_migrate():
    # ينشئ الجداول المعروفة (ومنها payments) إن لم تكن موجودة
    Base.metadata.create_all(bind=engine)

    insp = inspect(engine)
    dialect = engine.dialect.name

    with engine.begin() as conn:
        # users — أعمدة قديمة إن نقصت
        try:
            cols_users = {c["name"] for c in insp.get_columns("users")}
        except Exception:
            cols_users = set()
        required_users = ["tg_user_id", "trial_used", "end_at", "plan", "last_tx_hash", "created_at"]
        for c in required_users:
            if c not in cols_users:
                try:
                    conn.execute(text(_add_column_sql("users", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER users ADD {c} failed: {e}")

        # trades — أعمدة إضافية V2 إن نقصت
        try:
            cols_trades = {c["name"] for c in insp.get_columns("trades")}
        except Exception:
            cols_trades = set()
        required_trades = [
            "result", "opened_at", "closed_at", "status",
            "tp_final", "audit_id", "score", "regime", "reasons",
            "qty", "exit_price", "r_multiple"
        ]
        for c in required_trades:
            if c not in cols_trades:
                try:
                    conn.execute(text(_add_column_sql("trades", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER trades ADD {c} failed: {e}")

        # payments — create_all ينشئ الجدول + قيود، نضيف فقط فهارس إضافية عند الحاجة
        try:
            _ensure_indexes(conn, dialect)
        except Exception as e:
            logger.warning(f"CREATE INDEX failed: {e}")

def init_db():
    try:
        _lightweight_migrate()
        logger.info("db: Database ready.")
    except Exception as e:
        logger.exception(f"DB INIT ERROR: {e}")
        raise

# ---------------------------
# جلسة سياقية
# ---------------------------
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

# ---------------------------
# وظائف الاشتراك
# ---------------------------
def _get_or_create_user(s, tg_user_id: int) -> User:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u:
        u = User(tg_user_id=tg_user_id, trial_used=False, end_at=None)
        s.add(u); s.flush()
    return u

def is_active(s, tg_user_id: int) -> bool:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u or not u.end_at:
        return False
    return _as_aware(u.end_at) > _utcnow()

def start_trial(s, tg_user_id: int) -> bool:
    u = _get_or_create_user(s, tg_user_id)
    if u.trial_used:
        return False
    u.trial_used = True
    base = _utcnow()
    if u.end_at and _as_aware(u.end_at) > base:
        base = _as_aware(u.end_at)
    u.end_at = base + timedelta(days=1)
    u.plan = "trial"
    return True

def was_tx_used(s, tx_hash: str) -> bool:
    """تحقّق سريع: هل تم استخدام رقم المرجع سابقًا؟"""
    if not tx_hash:
        return False
    cnt = s.execute(select(func.count(Payment.id)).where(Payment.tx_hash == tx_hash)).scalar() or 0
    return cnt > 0

def record_payment_if_new(s, tg_user_id: int, plan: Optional[str], amount_usdt: Optional[float], tx_hash: str) -> int:
    """يسجل دفعًا جديدًا أو يطلق IntegrityError لو كان tx_hash مكررًا."""
    if not tx_hash:
        raise ValueError("tx_hash is required")
    p = Payment(tg_user_id=tg_user_id, plan=plan, amount_usdt=amount_usdt, tx_hash=tx_hash)
    s.add(p)
    try:
        s.flush()  # سيطلق IntegrityError لو tx_hash مكرر
    except IntegrityError as e:
        s.rollback()
        raise IntegrityError("Duplicate tx_hash", params=None, orig=e)  # رسالة أوضح
    return p.id

def approve_paid(s, tg_user_id: int, plan: str, duration_days: int, tx_hash: str | None = None) -> datetime:
    """
    تفعيل مدفوع:
    - يضيف المدة للخطة الحالية (يمدّد إن كان نشطًا).
    - يسجّل الدفع في جدول payments إن توفّر tx_hash (ويمنع التكرار).
    - يُحدّث آخر مرجع في users.last_tx_hash.
    """
    u = _get_or_create_user(s, tg_user_id)
    now = _utcnow()
    base = _as_aware(u.end_at) if (u.end_at and _as_aware(u.end_at) and _as_aware(u.end_at) > now) else now
    end_at = base + timedelta(days=int(duration_days))

    # منع إعادة استخدام المراجع
    if tx_hash:
        record_payment_if_new(s, tg_user_id=tg_user_id, plan=plan, amount_usdt=None, tx_hash=tx_hash)
        u.last_tx_hash = tx_hash

    u.end_at = end_at
    u.plan = plan
    s.flush()
    return u.end_at

# ---------------------------
# وظائف الصفقات
# ---------------------------
def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> int:
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2, status="open")
    s.add(t); s.flush()
    return t.id

def add_trade_sig(s, sig: dict, audit_id: str | None = None, qty: float | None = None) -> int:
    t = Trade(
        symbol=sig["symbol"],
        side=sig["side"],
        entry=float(sig["entry"]),
        sl=float(sig["sl"]),
        tp1=float(sig["tp1"]),
        tp2=float(sig["tp2"]),
        tp_final=float(sig.get("tp_final")) if sig.get("tp_final") is not None else None,
        score=int(sig.get("score")) if sig.get("score") is not None else None,
        regime=str(sig.get("regime")) if sig.get("regime") is not None else None,
        reasons=",".join(sig.get("reasons", [])) if isinstance(sig.get("reasons"), list) else (sig.get("reasons") or None),
        audit_id=audit_id,
        qty=float(qty) if qty is not None else None,
        status="open",
        opened_at=_utcnow(),
    )
    s.add(t); s.flush()
    return t.id

def has_open_trade_on_symbol(s, symbol: str) -> bool:
    return (s.execute(select(func.count(Trade.id)).where(Trade.symbol == symbol, Trade.status == "open")).scalar() or 0) > 0

def count_open_trades(s) -> int:
    return s.execute(select(func.count(Trade.id)).where(Trade.status == "open")).scalar() or 0

def close_trade(s, trade_id: int, result: str, exit_price: float | None = None, r_multiple: float | None = None):
    t = s.get(Trade, trade_id)
    if not t:
        return
    t.status = "closed"
    t.result = result
    t.closed_at = _utcnow()
    if exit_price is not None:
        try: t.exit_price = float(exit_price)
        except Exception: pass
    if r_multiple is not None:
        try: t.r_multiple = float(r_multiple)
        except Exception: pass
    s.flush()

def list_active_user_ids(s) -> list[int]:
    now = _utcnow()
    rows = s.execute(select(User.tg_user_id).where(User.end_at != None, User.end_at > now)).all()
    return [r[0] for r in rows if r[0]]

# ---------------------------
# إحصائيات التقارير
# ---------------------------
def _period_stats(s, since: datetime) -> dict:
    signals = s.execute(
        select(func.count(Trade.id)).where(Trade.opened_at >= since)
    ).scalar() or 0

    open_now = s.execute(
        select(func.count(Trade.id)).where(Trade.status == "open")
    ).scalar() or 0

    tp1 = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "tp1", Trade.closed_at >= since)
    ).scalar() or 0
    tp2 = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "tp2", Trade.closed_at >= since)
    ).scalar() or 0
    sl = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "sl", Trade.closed_at >= since)
    ).scalar() or 0

    r_sum = 0.0
    closed_rows = s.execute(
        select(Trade).where(Trade.status == "closed", Trade.closed_at >= since)
    ).scalars().all()
    for t in closed_rows:
        if t.r_multiple is not None:
            r_sum += float(t.r_multiple); continue
        try:
            risk = max(float(t.entry) - float(t.sl), 1e-9)
            if t.result == "tp1":
                r_sum += (float(t.tp1) - float(t.entry)) / risk
            elif t.result == "tp2":
                r_sum += (float(t.tp2) - float(t.entry)) / risk
            elif t.result == "sl":
                r_sum += -1.0
        except Exception:
            pass

    wins = tp1 + tp2
    losses = sl
    total = wins + losses
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0

    return {
        "signals": signals,
        "open": open_now,
        "tp1": tp1,
        "tp2": tp2,
        "tp_total": tp1 + tp2,
        "sl": sl,
        "win_rate": win_rate,
        "r_sum": round(r_sum, 2),
    }

def get_stats_24h(s) -> dict:
    since = _utcnow() - timedelta(hours=24)
    return _period_stats(s, since)

def get_stats_7d(s) -> dict:
    since = _utcnow() - timedelta(days=7)
    return _period_stats(s, since)

# ---------------------------
# Leader Lock APIs
# ---------------------------
def try_acquire_leader_lock(name: str, holder: str) -> bool:
    with SessionLocal() as s:
        if s.get(Lock, name):
            return False
        s.add(Lock(name=name, holder=holder))
        try:
            s.commit(); return True
        except IntegrityError:
            s.rollback(); return False

def acquire_or_steal_leader_lock(name: str, holder: str, ttl_seconds: int = 300) -> bool:
    now = _utcnow()
    expiry = now - timedelta(seconds=int(ttl_seconds))
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row is None:
            s.add(Lock(name=name, holder=holder, created_at=now))
            try:
                s.commit(); return True
            except Exception:
                s.rollback(); return False
        row_ct = _as_aware(row.created_at)
        if row_ct is not None and row_ct < expiry:
            row.holder = holder
            row.created_at = now
            try:
                s.commit(); return True
            except Exception:
                s.rollback(); return False
        return False

def heartbeat_leader_lock(name: str, holder: str) -> bool:
    now = _utcnow()
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row is None:
            return False
        if row.holder != holder:
            return False
        row.created_at = now
        try:
            s.commit(); return True
        except Exception:
            s.rollback(); return False

def release_leader_lock(name: str, holder: str) -> None:
    with SessionLocal() as s:
        row = s.get(Lock, name)
        if row and row.holder == holder:
            s.delete(row)
            try:
                s.commit()
            except Exception:
                s.rollback()

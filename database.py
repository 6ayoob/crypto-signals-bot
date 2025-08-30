# database.py — نماذج SQLAlchemy ووظائف الاشتراكات والصفقات

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Float, Boolean, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ضبط URL قاعدة البيانات
DB_URL = os.getenv("DATABASE_URL", "sqlite:///data.sqlite3")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg2://", 1)

engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()

UTC = timezone.utc

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)          # auto
    tg_user_id = Column(Integer, unique=True, index=True, nullable=False)
    trial_used = Column(Boolean, default=False)
    end_at = Column(DateTime(timezone=True), nullable=True)  # اشتراك فعال حتى
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(Integer, index=True, nullable=False)
    plan = Column(String(8))           # "2w" or "4w"
    amount_usd = Column(Float, default=0.0)
    tx_hash = Column(String(128))
    status = Column(String(32), default="submitted")  # submitted/approved/rejected
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), index=True, nullable=False)
    side = Column(String(8), default="BUY")
    entry = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=False)
    status = Column(String(16), default="open")  # open/tp1/closed_tp2/closed_sl
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

def init_db():
    Base.metadata.create_all(engine)

@contextmanager
def get_session() -> Session:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

# ===== اشتراك المستخدم =====
def is_active(s: Session, tg_user_id: int) -> bool:
    u = s.query(User).filter_by(tg_user_id=tg_user_id).first()
    if not u or not u.end_at:
        return False
    return u.end_at > datetime.now(UTC)

def start_trial(s: Session, tg_user_id: int, days: int = 1) -> bool:
    u = s.query(User).filter_by(tg_user_id=tg_user_id).first()
    if not u:
        u = User(tg_user_id=tg_user_id, trial_used=False)
        s.add(u)
        s.flush()
    if u.trial_used:
        return False
    u.trial_used = True
    u.end_at = (datetime.now(UTC) + timedelta(days=days))
    return True

def approve_paid(s: Session, tg_user_id: int, plan: str, duration_days: int, tx_hash: Optional[str] = None):
    # سجّل الدفع
    amount = 0.0
    if plan == "2w": amount = 30.0
    if plan == "4w": amount = 60.0
    p = Payment(tg_user_id=tg_user_id, plan=plan, amount_usd=amount, tx_hash=tx_hash, status="approved")
    s.add(p)

    # حدّث/أنشئ المستخدم
    u = s.query(User).filter_by(tg_user_id=tg_user_id).first()
    if not u:
        u = User(tg_user_id=tg_user_id, trial_used=True)  # لا يهم، دفع بالفعل
        s.add(u)
        s.flush()

    now = datetime.now(UTC)
    base = u.end_at if u.end_at and u.end_at > now else now
    u.end_at = base + timedelta(days=duration_days)
    s.flush()
    return u.end_at

# ===== الصفقات =====
OPENLIKE_STATUSES = ("open", "tp1")

def add_trade(s: Session, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> Trade:
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2, status="open")
    s.add(t)
    s.flush()
    return t

def count_open_trades(s: Session) -> int:
    return s.query(Trade).filter(Trade.status.in_(OPENLIKE_STATUSES)).count()

def update_trade_status(s: Session, trade_id: int, new_status: str) -> Optional[Trade]:
    t = s.get(Trade, trade_id)
    if not t:
        return None
    t.status = new_status
    s.flush()
    return t

def list_trades_with_status(s: Session, statuses: Tuple[str, ...]) -> List[Trade]:
    return list(s.query(Trade).filter(Trade.status.in_(statuses)).all())

# إحصاءات للـ 24 ساعة الأخيرة (للتقرير اليومي)
def trades_stats_last_24h(s: Session):
    since = datetime.now(UTC) - timedelta(hours=24)
    q = s.query(Trade).filter(Trade.created_at >= since)
    total = q.count()
    closed_tp2 = q.filter(Trade.status == "closed_tp2").count()
    closed_sl = q.filter(Trade.status == "closed_sl").count()
    tp1_only = q.filter(Trade.status == "tp1").count()
    open_now = q.filter(Trade.status == "open").count()
    return {
        "total": total,
        "closed_tp2": closed_tp2,
        "closed_sl": closed_sl,
        "tp1_only": tp1_only,
        "open": open_now,
    }

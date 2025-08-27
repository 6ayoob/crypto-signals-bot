# database.py — SQLAlchemy models + helpers
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, func
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from contextlib import contextmanager
from config import DATABASE_URL, TRIAL_DAYS

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    subscriptions = relationship("Subscription", back_populates="user")

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_at = Column(DateTime, default=datetime.utcnow)
    end_at = Column(DateTime, nullable=False)
    plan = Column(String, nullable=False)  # trial | 2w | 4w
    tx_hash = Column(String, nullable=True)
    user = relationship("User", back_populates="subscriptions")

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)  # BUY/SELL
    entry = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=False)
    is_open = Column(Boolean, default=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String, nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)

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

# Subscriptions helpers
def get_or_create_user(s, tg_id: int) -> User:
    u = s.query(User).filter_by(tg_id=tg_id).first()
    if not u:
        u = User(tg_id=tg_id)
        s.add(u); s.flush()
    return u

def is_active(s, tg_id: int) -> bool:
    now = datetime.utcnow()
    u = s.query(User).filter_by(tg_id=tg_id).first()
    if not u: return False
    sub = (s.query(Subscription)
           .filter(Subscription.user_id == u.id, Subscription.end_at >= now)
           .order_by(Subscription.end_at.desc()).first())
    return bool(sub)

def start_trial(s, tg_id: int):
    u = get_or_create_user(s, tg_id)
    now = datetime.utcnow()
    s.add(Subscription(user_id=u.id, start_at=now, end_at=now + timedelta(days=TRIAL_DAYS), plan="trial"))

def approve_paid(s, tg_id: int, plan: str, duration: timedelta, tx_hash: Optional[str] = None):
    u = get_or_create_user(s, tg_id)
    now = datetime.utcnow()
    s.add(Subscription(user_id=u.id, start_at=now, end_at=now + duration, plan=plan, tx_hash=tx_hash))

# Trades helpers (بسيطة مبدئيًا، يمكنك التوسعة لاحقًا)
def count_open_trades(s) -> int:
    return s.query(func.count(Trade.id)).filter(Trade.is_open == True).scalar() or 0

def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> Trade:
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2)
    s.add(t); s.flush()
    return t

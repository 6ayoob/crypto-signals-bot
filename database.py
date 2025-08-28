# database.py — SQLAlchemy models + helpers (محسّن لـ Render/Prod)
from datetime import datetime, timedelta
from typing import Optional
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, func, Index
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from config import DATABASE_URL, TRIAL_DAYS

# -------------------------------------------------
# Engine & Session (إعدادات مستقرة على Render)
# -------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,         # يتعامل مع اتصالات ميتة
    pool_size=5,                # حجم معقول للـ worker
    max_overflow=5,             # سماح بتمدد بسيط
    future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()

UTCNOW = datetime.utcnow  # مرجع واحد لو أردت تبديله لاحقًا

# -------------------------------------------------
# Models
# -------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=UTCNOW)
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    start_at = Column(DateTime, default=UTCNOW, nullable=False)
    end_at = Column(DateTime, nullable=False, index=True)
    plan = Column(String, nullable=False)  # trial | 2w | 4w
    tx_hash = Column(String, nullable=True)
    user = relationship("User", back_populates="subscriptions")

# فهارس مفيدة للاستعلامات
Index("idx_subscriptions_user_end", Subscription.user_id, Subscription.end_at)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)  # BUY/SELL
    entry = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=False)
    is_open = Column(Boolean, default=True, index=True)
    opened_at = Column(DateTime, default=UTCNOW)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String, nullable=True)

Index("idx_trades_opened_isopen", Trade.opened_at, Trade.is_open)


# -------------------------------------------------
# Schema init
# -------------------------------------------------
def init_db():
    Base.metadata.create_all(bind=engine)


# -------------------------------------------------
# Session helper
# -------------------------------------------------
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


# -------------------------------------------------
# Subscription helpers
# -------------------------------------------------
def get_or_create_user(s, tg_id: int) -> User:
    u = s.query(User).filter_by(tg_id=tg_id).first()
    if not u:
        u = User(tg_id=tg_id)
        s.add(u)
        s.flush()
    return u


def active_until(s, tg_id: int) -> Optional[datetime]:
    """يعيد آخر تاريخ نهاية لاشتراك نشط (إن وجد)."""
    now = UTCNOW()
    u = s.query(User).filter_by(tg_id=tg_id).first()
    if not u:
        return None
    sub = (
        s.query(Subscription)
        .filter(Subscription.user_id == u.id, Subscription.end_at >= now)
        .order_by(Subscription.end_at.desc())
        .first()
    )
    return sub.end_at if sub else None


def is_active(s, tg_id: int) -> bool:
    return active_until(s, tg_id) is not None


def has_used_trial(s, tg_id: int) -> bool:
    """تحقق ما إذا كان المستخدم قد استخدم التجربة من قبل (حتى لو انتهت)."""
    u = s.query(User).filter_by(tg_id=tg_id).first()
    if not u:
        return False
    return s.query(Subscription).filter(Subscription.user_id == u.id, Subscription.plan == "trial").first() is not None


def start_trial(s, tg_id: int) -> bool:
    """
    يبدأ تجربة مجانية إن لم يكن لديه تجربة سابقة.
    يعيد True إذا بدأ التجربة، False إذا كان سبق واستخدمها.
    """
    if has_used_trial(s, tg_id):
        return False
    u = get_or_create_user(s, tg_id)
    now = UTCNOW()
    s.add(
        Subscription(
            user_id=u.id,
            start_at=now,
            end_at=now + timedelta(days=TRIAL_DAYS),
            plan="trial"
        )
    )
    return True


def approve_paid(s, tg_id: int, plan: str, duration: timedelta, tx_hash: Optional[str] = None):
    """
    يضيف/يمدد اشتراك مدفوع:
    - إن كان لديه اشتراك نشط: نمدّ من تاريخ الانتهاء.
    - إن لم يكن نشطًا: نبدأ من الآن.
    """
    u = get_or_create_user(s, tg_id)
    now = UTCNOW()
    current_end = active_until(s, tg_id)
    start_at = now if current_end is None or current_end < now else current_end
    end_at = start_at + duration
    s.add(
        Subscription(
            user_id=u.id,
            start_at=start_at,
            end_at=end_at,
            plan=plan,
            tx_hash=tx_hash
        )
    )
    return end_at


# -------------------------------------------------
# Trade helpers
# -------------------------------------------------
def count_open_trades(s) -> int:
    return int(s.query(func.count(Trade.id)).filter(Trade.is_open.is_(True)).scalar() or 0)


def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> Trade:
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2)
    s.add(t)
    s.flush()
    return t


def close_trade(s, trade_id: int, reason: str, close_price: Optional[float] = None) -> Optional[Trade]:
    t = s.query(Trade).filter(Trade.id == trade_id, Trade.is_open.is_(True)).first()
    if not t:
        return None
    t.is_open = False
    t.closed_at = UTCNOW()
    if close_price is not None:
        # يمكن لاحقًا حساب PnL وتخزينه إذا رغبت
        pass
    t.close_reason = reason
    s.flush()
    return t

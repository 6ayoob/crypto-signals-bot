# database.py — طبقة البيانات (SQLite + SQLAlchemy 2.0)
# جداول: User, Subscription, Trade
# خدمات: init_db, get_session, is_active, start_trial, approve_paid, count_open_trades, add_trade

from __future__ import annotations
import os
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, String, Integer, BigInteger, Float, DateTime,
    Boolean, ForeignKey, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

logger = logging.getLogger("db")

# ===================== الإعداد =====================
DB_URL = os.getenv("DATABASE_URL", "sqlite:///data.sqlite3")

# SQLite يحتاج هذا الخيار للعمل مع Threads (لتوافق async + executor)
engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


class Base(DeclarativeBase):
    pass


# ===================== النماذج =====================
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    first_name: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    last_name: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User id={self.id} trial_used={self.trial_used}>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(16))  # 'trial' | '2w' | '4w' | etc.
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped["User"] = relationship(back_populates="subscriptions")

    def __repr__(self) -> str:
        return f"<Sub uid={self.user_id} plan={self.plan} end={self.end_at.isoformat()}>"


# صفقات (تسجيل خفيف للأغراض المعلوماتية)
class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8), default="BUY")  # BUY فقط حاليًا
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/closed (لاحقًا)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_trades_symbol_status", "symbol", "status"),
    )

    def __repr__(self) -> str:
        return f"<Trade {self.symbol} {self.side} entry={self.entry} status={self.status}>"


# ===================== تهيئة القاعدة =====================
def init_db() -> None:
    """إنشاء الجداول إن لم تكن موجودة."""
    Base.metadata.create_all(engine)
    logger.info("Database ready.")


@contextmanager
def get_session() -> Session:
    """مدير جلسة مريح مع commit/rollback تلقائي."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ===================== الدوال المساعدة =====================
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_user(s: Session, user_id: int, username: str | None = None,
                first_name: str | None = None, last_name: str | None = None) -> User:
    user = s.get(User, user_id)
    if not user:
        user = User(id=user_id, username=username, first_name=first_name, last_name=last_name)
        s.add(user)
        s.flush()
    # تحديث بسيط إن تغيّر الاسم/اليوزر
    changed = False
    if username and user.username != username:
        user.username = username; changed = True
    if first_name and user.first_name != first_name:
        user.first_name = first_name; changed = True
    if last_name and user.last_name != last_name:
        user.last_name = last_name; changed = True
    if changed:
        user.updated_at = _now_utc()
    return user


def is_active(s: Session, user_id: int) -> bool:
    """هل للمستخدم أي اشتراك نشط (الآن أو لاحقًا)؟"""
    now = _now_utc()
    q = s.query(Subscription).filter(
        Subscription.user_id == user_id,
        Subscription.end_at >= now
    ).limit(1)
    return s.query(q.exists()).scalar() or False


def start_trial(s: Session, user_id: int) -> bool:
    """
    تفعيل تجربة مجانية ليوم واحد مرة واحدة فقط.
    يرجع False إن سبق استخدامها.
    """
    user = ensure_user(s, user_id)
    if user.trial_used:
        return False

    now = _now_utc()
    # إن كان لديه اشتراك فعّال، نبدأ بعد نهاية آخر اشتراك كي لا يضيع الوقت
    last_end = _last_active_end(s, user_id) or now
    start_at = max(now, last_end)
    end_at = start_at + timedelta(days=1)

    sub = Subscription(
        user_id=user_id,
        plan="trial",
        start_at=start_at,
        end_at=end_at,
        tx_hash=None
    )
    s.add(sub)
    user.trial_used = True
    user.updated_at = now
    logger.info(f"Trial started for user {user_id} until {end_at.isoformat()}")
    return True


def approve_paid(s: Session, user_id: int, plan: str, duration_days: int, tx_hash: str | None = None) -> datetime:
    """
    تفعيل اشتراك مدفوع. إذا كان لديه اشتراك نشط، يتم التمديد (stack).
    يرجع end_at للاشتراك الجديد.
    """
    user = ensure_user(s, user_id)
    now = _now_utc()

    base_start = _last_active_end(s, user_id)
    # إذا لديه اشتراك فعّال، ابدأ من نهاية آخر اشتراك؛ وإلا من الآن
    start_at = base_start if (base_start and base_start > now) else now
    end_at = start_at + timedelta(days=duration_days)

    sub = Subscription(
        user_id=user_id,
        plan=plan,
        start_at=start_at,
        end_at=end_at,
        tx_hash=tx_hash
    )
    s.add(sub)
    user.updated_at = now
    logger.info(f"Paid sub '{plan}' for user {user_id} until {end_at.isoformat()}")
    return end_at


def _last_active_end(s: Session, user_id: int) -> Optional[datetime]:
    """يجلب نهاية أحدث اشتراك (حتى لو غير نشط الآن)."""
    sub = (s.query(Subscription)
             .filter(Subscription.user_id == user_id)
             .order_by(Subscription.end_at.desc())
             .first())
    return sub.end_at if sub else None


def count_open_trades(s: Session) -> int:
    return s.query(Trade).filter(Trade.status == "open").count()


def add_trade(s: Session, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> Trade:
    t = Trade(
        symbol=symbol,
        side=side,
        entry=float(entry),
        sl=float(sl),
        tp1=float(tp1),
        tp2=float(tp2),
        status="open",
    )
    s.add(t)
    s.flush()
    return t

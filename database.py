# database.py
import os
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, DateTime, Boolean, Float,
    Index, text
)
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("db")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
else:
    engine = create_engine("sqlite:///bot.db", future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

# ---------------- Models ----------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True)  # قد يُضاف عبر ensure_schema
    trial_used = Column(Boolean, default=False)
    end_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Index("ix_users_tg_user_id", User.tg_user_id)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(32))
    side = Column(String(8))
    entry = Column(Float)
    sl = Column(Float)
    tp1 = Column(Float)
    tp2 = Column(Float)
    status = Column(String(16), default="open")  # open/closed/tp/sl
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(16), nullable=True)  # tp1/tp2/sl/manual

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True)
    plan = Column(String(8))           # "2w" | "4w"
    base_amount = Column(Float)        # 30 أو 60
    code_frac = Column(Float)          # مثل 0.004 لتوليد مبلغ مميّز
    expected_amount = Column(Float)    # المبلغ النهائي المطلوب دفعه
    status = Column(String(16), default="pending")  # pending/paid/cancelled/expired
    tx_hash = Column(String(128), nullable=True)
    paid_amount = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)    # صلاحية الفاتورة

Index("ix_invoices_user_status", Invoice.tg_user_id, Invoice.status)

# --------------- Schema patch ---------------
def ensure_schema(engine):
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.exec_driver_sql("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;
            """)
            conn.exec_driver_sql("""
                CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id);
            """)
        # SQLite يغطيه create_all عادةً
    logger.info("db: schema checked/updated.")

# --------------- Helpers ---------------
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

def init_db():
    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)
    logger.info("Database ready.")

# --------------- Subscription logic ---------------
def _get_user(s, tg_user_id: int) -> User:
    u = s.query(User).filter(User.tg_user_id == tg_user_id).first()
    if not u:
        u = User(tg_user_id=tg_user_id, created_at=datetime.utcnow())
        s.add(u)
        s.flush()
    return u

def is_active(s, tg_user_id: int) -> bool:
    u = _get_user(s, tg_user_id)
    return bool(u.end_at and u.end_at > datetime.utcnow())

def start_trial(s, tg_user_id: int, days: int = 1) -> bool:
    u = _get_user(s, tg_user_id)
    if u.trial_used:
        return False
    u.trial_used = True
    now = datetime.utcnow()
    u.end_at = max(u.end_at or now, now) + timedelta(days=days)
    s.add(u)
    return True

def approve_paid(s, tg_user_id: int, plan: str, duration_days: int, tx_hash: str | None = None):
    u = _get_user(s, tg_user_id)
    now = datetime.utcnow()
    u.end_at = max(u.end_at or now, now) + timedelta(days=duration_days)
    s.add(u)
    # لو الفاتورة موجودة نحدّثها
    inv = s.query(Invoice).filter(
        Invoice.tg_user_id == tg_user_id,
        Invoice.status == "pending"
    ).order_by(Invoice.created_at.desc()).first()
    if inv and tx_hash:
        inv.status = "paid"
        inv.tx_hash = tx_hash
        inv.paid_at = datetime.utcnow()
        s.add(inv)
    return u.end_at

# --------------- Invoices ---------------
from random import randint

def _suggest_code_frac(s, base_amount: float) -> float:
    """
    يولد كسور مميزة 0.001 .. 0.009 للتفريق بين المشتركين (وتحويل الفحص إلى تلقائي).
    يتفادا تضارب مع فواتير معلقة لنفس المبلغ.
    """
    used = {
        round(inv.expected_amount - inv.base_amount, 6)
        for inv in s.query(Invoice).filter(
            Invoice.status == "pending",
            Invoice.base_amount == base_amount
        ).all()
    }
    # جرّب حتى تجد كسرًا غير مستخدم
    for _ in range(20):
        cand = round(randint(1, 9) / 1000.0, 6)  # 0.001 .. 0.009
        if cand not in used:
            return cand
    # احتياط: 0.01 خطوة أصغر
    for i in range(10, 100):
        cand = round(i / 10000.0, 6)  # 0.0010 .. 0.0099
        if cand not in used:
            return cand
    return 0.0

def create_invoice(s, tg_user_id: int, plan: str, base_amount: float, validity_minutes: int = 120) -> Invoice:
    code_frac = _suggest_code_frac(s, base_amount)
    inv = Invoice(
        tg_user_id=tg_user_id,
        plan=plan,
        base_amount=base_amount,
        code_frac=code_frac,
        expected_amount=round(base_amount + code_frac, 6),
        status="pending",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=validity_minutes)
    )
    s.add(inv)
    s.flush()
    return inv

def get_invoice(s, invoice_id: int) -> Invoice | None:
    return s.query(Invoice).filter(Invoice.id == invoice_id).first()

def list_pending_invoices(s, within_hours: int = 24) -> list[Invoice]:
    since = datetime.utcnow() - timedelta(hours=within_hours)
    return s.query(Invoice).filter(
        Invoice.status == "pending",
        Invoice.created_at >= since
    ).all()

def mark_invoice_paid(s, invoice_id: int, tx_hash: str, paid_amount: float):
    inv = get_invoice(s, invoice_id)
    if not inv:
        return False
    inv.status = "paid"
    inv.tx_hash = tx_hash
    inv.paid_amount = paid_amount
    inv.paid_at = datetime.utcnow()
    s.add(inv)
    return True

# --------------- Trades ---------------
def count_open_trades(s) -> int:
    return s.query(Trade).filter(Trade.status == "open").count()

def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float):
    t = Trade(
        symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
        status="open", created_at=datetime.utcnow()
    )
    s.add(t)
    s.flush()
    return t.id

def close_trade(s, trade_id: int, reason: str):
    t = s.query(Trade).filter(Trade.id == trade_id).first()
    if not t:
        return False
    t.status = "closed"
    t.closed_at = datetime.utcnow()
    t.close_reason = reason
    s.add(t)
    return True

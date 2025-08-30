# database.py — SQLAlchemy models & helpers (PostgreSQL/SQLite)
import os
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime,
    Float, text, select, func
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import inspect

logger = logging.getLogger("db")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ---------------------------
# اتصال قاعدة البيانات
# ---------------------------
def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        # SQLAlchemy يفضّل postgresql+psycopg2
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
# النماذج (Models)
# ---------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True, unique=False, nullable=True)  # نجعلها موجودة دائمًا؛ فريدة منطقيًا
    trial_used = Column(Boolean, default=False, nullable=False)
    end_at = Column(DateTime, nullable=True)       # تاريخ انتهاء الاشتراك/التجربة (UTC)
    plan = Column(String(8), nullable=True)        # "2w" | "4w"
    last_tx_hash = Column(String(128), nullable=True)  # رقم المرجع/هاش آخر دفعة
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), index=True, nullable=False)
    side = Column(String(4), nullable=False)  # "BUY"
    entry = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=False)
    status = Column(String(8), default="open", index=True, nullable=False)  # open | closed
    result = Column(String(8), nullable=True)  # tp1 | tp2 | sl
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    closed_at = Column(DateTime, nullable=True)

# ---------------------------
# تهيئة + هجرة خفيفة (إضافة أعمدة ناقصة)
# ---------------------------
def _add_column_sql(table: str, col: str, dialect: str) -> str:
    # أنواع عامة لكل محرك
    if table == "users":
        mapping_pg = {
            "tg_user_id": "BIGINT",
            "trial_used": "BOOLEAN DEFAULT FALSE",
            "end_at": "TIMESTAMP",
            "plan": "VARCHAR(8)",
            "last_tx_hash": "VARCHAR(128)",
            "created_at": "TIMESTAMP"
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
            "closed_at": "TIMESTAMP",
        }
        mapping_sq = {
            "result": "VARCHAR(8)",
            "closed_at": "TIMESTAMP",
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    else:
        typ = "TEXT"
    if dialect == "postgresql":
        return f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col}" {typ};'
    # SQLite لا يدعم IF NOT EXISTS للأعمدة
    return f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ};'

def _ensure_indexes(connection, dialect: str):
    if dialect == "postgresql":
        # فهرس/فريد على tg_user_id لتسريع الاستعلام (قد يفشل إذا توجد قيم NULL كثيرة)
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON "users" (tg_user_id);'))
        # عدم فرض UNIQUE لتفادي فشل الهجرة على قواعد قديمة بها صفوف بدون tg_user_id

def _lightweight_migrate():
    insp = inspect(engine)
    dialect = engine.dialect.name  # 'postgresql' أو 'sqlite'

    # أنشئ الجداول إذا لم تكن موجودة
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        # users
        cols_users = {c["name"] for c in insp.get_columns("users")}
        required_users = ["tg_user_id", "trial_used", "end_at", "plan", "last_tx_hash", "created_at"]
        for c in required_users:
            if c not in cols_users:
                try:
                    conn.execute(text(_add_column_sql("users", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER users ADD {c} failed: {e}")

        # trades
        cols_trades = {c["name"] for c in insp.get_columns("trades")}
        required_trades = ["result", "closed_at"]
        for c in required_trades:
            if c not in cols_trades:
                try:
                    conn.execute(text(_add_column_sql("trades", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER trades ADD {c} failed: {e}")

        # فهارس
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
def _utcnow():
    return datetime.now(timezone.utc)

def _get_or_create_user(s, tg_user_id: int) -> User:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u:
        u = User(tg_user_id=tg_user_id, trial_used=False, end_at=None)
        s.add(u)
        s.flush()
    return u

def is_active(s, tg_user_id: int) -> bool:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u or not u.end_at:
        return False
    return u.end_at > _utcnow()

def start_trial(s, tg_user_id: int) -> bool:
    """
    تفعيل تجربة يوم واحد لمرة واحدة فقط.
    إن كانت مستخدمة سابقًا يُرجع False.
    """
    u = _get_or_create_user(s, tg_user_id)
    if u.trial_used:
        return False
    u.trial_used = True
    # يمكن جعلها لا تمتد إن كان لديه اشتراك نشط؛ هنا نمدد يومًا من الآن أو من نهاية الاشتراك
    base = _utcnow()
    if u.end_at and u.end_at > base:
        base = u.end_at
    u.end_at = base + timedelta(days=1)
    u.plan = "trial"
    return True

def approve_paid(s, tg_user_id: int, plan: str, duration_days: int, tx_hash: str | None = None) -> datetime:
    """
    تأكيد دفع المستخدم وتمديد الاشتراك.
    يرجع تاريخ الانتهاء الجديد (UTC).
    """
    u = _get_or_create_user(s, tg_user_id)
    now = _utcnow()
    base = u.end_at if (u.end_at and u.end_at > now) else now
    u.end_at = base + timedelta(days=int(duration_days))
    u.plan = plan
    if tx_hash:
        u.last_tx_hash = tx_hash
    s.flush()
    return u.end_at

# ---------------------------
# وظائف الصفقات
# ---------------------------
def add_trade(s, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float) -> int:
    t = Trade(
        symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2,
        status="open"
    )
    s.add(t)
    s.flush()
    return t.id

def count_open_trades(s) -> int:
    return s.execute(select(func.count(Trade.id)).where(Trade.status == "open")).scalar() or 0

def close_trade(s, trade_id: int, result: str):
    """
    إغلاق صفقة وتسجيل النتيجة: 'tp1' | 'tp2' | 'sl'
    """
    t = s.get(Trade, trade_id)
    if not t:
        return
    t.status = "closed"
    t.result = result
    t.closed_at = _utcnow()
    s.flush()

# ---------------------------
# إحصائيات التقارير
# ---------------------------
def _period_stats(s, since: datetime) -> dict:
    # إشارات (عدد الصفقات المفتوحة خلال الفترة)
    signals = s.execute(
        select(func.count(Trade.id)).where(Trade.opened_at >= since)
    ).scalar() or 0

    open_now = s.execute(
        select(func.count(Trade.id)).where(Trade.status == "open")
    ).scalar() or 0

    # نتائج مغلقة خلال الفترة
    tp1 = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "tp1", Trade.closed_at >= since)
    ).scalar() or 0
    tp2 = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "tp2", Trade.closed_at >= since)
    ).scalar() or 0
    sl = s.execute(
        select(func.count(Trade.id)).where(Trade.result == "sl", Trade.closed_at >= since)
    ).scalar() or 0

    # حساب R المحققة تقريبيًا لكل صفقة مغلقة خلال الفترة
    r_sum = 0.0
    closed_rows = s.execute(
        select(Trade).where(Trade.status == "closed", Trade.closed_at >= since)
    ).scalars().all()
    for t in closed_rows:
        risk = max(t.entry - t.sl, 1e-9)  # للصفقة الطويلة
        if t.result == "tp1":
            r_sum += (t.tp1 - t.entry) / risk
        elif t.result == "tp2":
            r_sum += (t.tp2 - t.entry) / risk
        elif t.result == "sl":
            r_sum += -1.0

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

# database.py — SQLAlchemy models & helpers (PostgreSQL/SQLite)
# + نظام الإحالات (يوم مجاني للمُحيل عند تفعيل المدعو اشتراكًا مدفوعًا)
# + تحسينات خفيفة آمنة ومتوافقة مع bot.py الحالي
#
# ملاحظات:
# - عند approve_paid() إذا كان المستخدم تمت إحالته ولم يُكافأ مُحيله بعد، يُمنح المُحيل REF_BONUS_HOURS (افتراضي 24 ساعة).
# - أضفنا ref_code (كود إحالة فريد) وreferred_by (معرّف المُحيل) في جدول users.
# - أضفنا جدول referrals لتسجيل الحِراك الائتماني وعدم تكراره.
# - أبقينا جميع دوال الإحصاءات والـ Leader Lock كما هي.

import os
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean, DateTime,
    Float, Text, text, select, func, UniqueConstraint, Index
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger("db")
if not logging.getLogger().hasHandlers():
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
# Helpers
# ---------------------------
def _utcnow():
    return datetime.now(timezone.utc)

def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    try:
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt

# بيئة الإحالات
REF_BONUS_HOURS = int(os.getenv("REF_BONUS_HOURS", "24"))          # عدد ساعات المكافأة للمُحيل
ALLOW_SELF_REFERRAL = os.getenv("ALLOW_SELF_REFERRAL", "0").strip() in ("1","true","yes","on")

# ---------------------------
# النماذج (Models)
# ---------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, index=True, unique=False, nullable=True)

    trial_used = Column(Boolean, default=False, nullable=False)
    end_at = Column(DateTime, nullable=True)             # UTC
    plan = Column(String(8), nullable=True)              # "trial" | "2w" | "4w" | "gift1d"
    last_tx_hash = Column(String(128), nullable=True)

    # إحالات
    ref_code = Column(String(16), index=True, unique=False, nullable=True)  # نجعلها فريدة بفهرس
    referred_by = Column(Integer, nullable=True)  # user.id للمُحيل (بدون FK لتبسيط الهجرة)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        # فهارس إضافية
        Index("ix_users_ref_code", "ref_code"),
    )

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
    qty        = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    r_multiple = Column(Float, nullable=True)
    # حالة
    status = Column(String(8), default="open", index=True, nullable=False)  # open | closed
    result = Column(String(8), nullable=True)  # tp1 | tp2 | sl
    opened_at = Column(DateTime, default=_utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    # created/updated
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

class Lock(Base):
    __tablename__ = "locks"
    name = Column(String(64), primary_key=True)
    holder = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    referrer_user_id = Column(Integer, nullable=False)  # user.id للمُحيل
    referee_user_id  = Column(Integer, nullable=False)  # user.id للمدعو
    credited_hours   = Column(Integer, nullable=False, default=0)
    credited_at      = Column(DateTime, nullable=True)
    note             = Column(String(128), nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("referrer_user_id", "referee_user_id", name="ux_ref_pair"),
        Index("ix_ref_referrer", "referrer_user_id"),
        Index("ix_ref_referee", "referee_user_id"),
    )

# ---------------------------
# تهيئة + هجرة خفيفة
# ---------------------------
def _add_column_sql(table: str, col: str, dialect: str) -> str:
    if table == "users":
        mapping_pg = {
            "tg_user_id": "BIGINT",
            "trial_used": "BOOLEAN DEFAULT FALSE",
            "end_at": "TIMESTAMPTZ",
            "plan": "VARCHAR(8)",
            "last_tx_hash": "VARCHAR(128)",
            "ref_code": "VARCHAR(16)",
            "referred_by": "INTEGER",
            "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        }
        mapping_sq = {
            "tg_user_id": "BIGINT",
            "trial_used": "BOOLEAN",
            "end_at": "TIMESTAMP",
            "plan": "VARCHAR(8)",
            "last_tx_hash": "VARCHAR(128)",
            "ref_code": "VARCHAR(16)",
            "referred_by": "INTEGER",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
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
            "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
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
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
        }
        typ = mapping_pg[col] if dialect == "postgresql" else mapping_sq[col]
    else:
        typ = "TEXT"

    if dialect == "postgresql":
        return f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{col}" {typ};'
    return f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ};'  # SQLite

def _ensure_indexes(connection, dialect: str):
    # users
    if dialect == "postgresql":
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON "users" (tg_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_status ON "trades" (status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_opened_at ON "trades" (opened_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_audit_id ON "trades" (audit_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_symbol_status ON "trades" (symbol, status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_ref_code ON "users" (ref_code);'))
        # referrals
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_pair ON "referrals"(referrer_user_id, referee_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ref_referrer ON "referrals"(referrer_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ref_referee ON "referrals"(referee_user_id);'))
    else:
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_tg_user_id ON users (tg_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_status ON trades (status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_opened_at ON trades (opened_at);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_audit_id ON trades (audit_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_trades_symbol_status ON trades (symbol, status);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_users_ref_code ON users (ref_code);'))
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_pair ON referrals(referrer_user_id, referee_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ref_referrer ON referrals(referrer_user_id);'))
        connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ref_referee ON referrals(referee_user_id);'))

def _dedupe_users_on_tg_user_id(connection, dialect: str):
    dup_sql = 'SELECT tg_user_id FROM "users" WHERE tg_user_id IS NOT NULL GROUP BY tg_user_id HAVING COUNT(*) > 1;' \
        if dialect == "postgresql" else \
        'SELECT tg_user_id FROM users WHERE tg_user_id IS NOT NULL GROUP BY tg_user_id HAVING COUNT(*) > 1;'
    result = connection.execute(text(dup_sql)).fetchall()
    dups = [r[0] for r in result if r[0] is not None]
    for tg_id in dups:
        if dialect == "postgresql":
            del_sql = '''
                DELETE FROM "users"
                WHERE tg_user_id = :tg AND id NOT IN (
                    SELECT id FROM "users" WHERE tg_user_id = :tg ORDER BY id ASC LIMIT 1
                );
            '''
        else:
            del_sql = '''
                DELETE FROM users
                WHERE tg_user_id = :tg AND id NOT IN (
                    SELECT id FROM users WHERE tg_user_id = :tg ORDER BY id ASC LIMIT 1
                );
            '''
        connection.execute(text(del_sql), {"tg": tg_id})

def _ensure_unique_index_users_tg_user_id(connection, dialect: str):
    _dedupe_users_on_tg_user_id(connection, dialect)
    if dialect == "postgresql":
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tg_user_id ON "users" (tg_user_id);'))
    else:
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tg_user_id ON users (tg_user_id);'))

def _ensure_unique_index_users_ref_code(connection, dialect: str):
    # ref_code قد يكون فارغًا لبعض المستخدمين القدامى — لا بأس؛ الفهرس الفريد يسمح بالقيم الفارغة في Postgres.
    if dialect == "postgresql":
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_ref_code ON "users" (ref_code);'))
    else:
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_ref_code ON users (ref_code);'))

def _lightweight_migrate():
    Base.metadata.create_all(bind=engine)

    insp = inspect(engine)
    dialect = engine.dialect.name

    with engine.begin() as conn:
        # users — تأكد من الأعمدة
        try:
            cols_users = {c["name"] for c in insp.get_columns("users")}
        except Exception:
            cols_users = set()
        required_users = ["tg_user_id", "trial_used", "end_at", "plan", "last_tx_hash",
                          "ref_code", "referred_by", "created_at", "updated_at"]
        for c in required_users:
            if c not in cols_users:
                try:
                    conn.execute(text(_add_column_sql("users", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER users ADD {c} failed: {e}")
        if dialect == "postgresql":
            try:
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "created_at" SET DEFAULT NOW();'))
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "updated_at" SET DEFAULT NOW();'))
                conn.execute(text('UPDATE "users" SET "updated_at" = COALESCE("updated_at", "created_at");'))
                conn.execute(text('ALTER TABLE "users" ALTER COLUMN "updated_at" SET NOT NULL;'))
            except Exception as e:
                logger.warning(f"users.updated_at default/not null adjust failed: {e}")

        # trades — تأكد من الأعمدة
        try:
            cols_trades = {c["name"] for c in insp.get_columns("trades")}
        except Exception:
            cols_trades = set()
        required_trades = [
            "result", "opened_at", "closed_at", "status",
            "tp_final", "audit_id", "score", "regime", "reasons",
            "qty", "exit_price", "r_multiple",
            "created_at", "updated_at",
        ]
        for c in required_trades:
            if c not in cols_trades:
                try:
                    conn.execute(text(_add_column_sql("trades", c, dialect)))
                except Exception as e:
                    logger.warning(f"ALTER trades ADD {c} failed: {e}")

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

        # referrals — تأكد من وجود الجدول (Base.create_all سيُنشئه)، ثم المؤشرات
        try:
            _ = insp.get_columns("referrals")  # سيثير خطأ إن لم توجد
        except Exception:
            try:
                Base.metadata.create_all(bind=engine)
            except Exception as e:
                logger.warning(f"create referrals table failed: {e}")

        # مؤشرات عامة
        try:
            _ensure_indexes(conn, dialect)
        except Exception as e:
            logger.warning(f"CREATE INDEX failed: {e}")

        # الفهرس/القيد الفريد على users.tg_user_id + users.ref_code
        try:
            _ensure_unique_index_users_tg_user_id(conn, dialect)
        except Exception as e:
            logger.warning(f"Ensure unique users.tg_user_id failed: {e}")
        try:
            _ensure_unique_index_users_ref_code(conn, dialect)
        except Exception as e:
            logger.warning(f"Ensure unique users.ref_code failed: {e}")

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
# وظائف مساندة (إحالات)
# ---------------------------
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # بدون 0OI لتجنّب اللبس

def _b32(n: int) -> str:
    if n <= 0:
        return "2"
    out = []
    base = len(_ALPHABET)
    while n > 0:
        n, r = divmod(n, base)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))[-8:]  # حتى 8 حروف تكفي وتجمل

def _gen_ref_code_for(tg_user_id: int) -> str:
    return f"R{_b32(abs(int(tg_user_id or 0)))}"

def get_or_create_user(s, tg_user_id: int) -> User:
    users = s.execute(
        select(User).where(User.tg_user_id == tg_user_id).order_by(User.id.asc())
    ).scalars().all()
    if not users:
        u = User(tg_user_id=tg_user_id, trial_used=False, end_at=None)
        # عيّن ref_code مبدئي
        try:
            u.ref_code = _gen_ref_code_for(tg_user_id)
        except Exception:
            u.ref_code = None
        s.add(u); s.flush()
        return u
    keep = users[0]
    for extra in users[1:]:
        try:
            s.delete(extra)
        except Exception:
            pass
    # ضمن ref_code
    if not keep.ref_code:
        try:
            keep.ref_code = _gen_ref_code_for(keep.tg_user_id)
        except Exception:
            pass
    s.flush()
    return keep

def find_user_by_ref_code(s, code: str) -> User | None:
    if not code:
        return None
    return s.execute(select(User).where(User.ref_code == code)).scalar_one_or_none()

def attach_referrer_by_code(s, tg_user_id: int, ref_code: str) -> bool:
    """
    يربط المستخدم (tg_user_id) بمُحيل عبر ref_code (مرة واحدة فقط).
    تُستدعى مثلًا عند /start ref=CODE.
    """
    u = get_or_create_user(s, tg_user_id)
    if u.referred_by:
        return False
    ref = find_user_by_ref_code(s, (ref_code or "").strip().upper())
    if not ref:
        return False
    if (not ALLOW_SELF_REFERRAL) and (ref.id == u.id):
        return False
    u.referred_by = ref.id
    s.flush()
    return True

def _award_referrer_if_eligible(s, referee: User) -> None:
    """
    يمنح المُحيل يومًا مجانيًا (REF_BONUS_HOURS) إذا كان referee ممتلكًا لحقل referred_by
    ولم تُسجل له مكافأة مسبقًا.
    """
    if not referee or not referee.referred_by:
        return
    referrer = s.get(User, referee.referred_by)
    if not referrer:
        return
    # لا تُكرّر المكافأة لنفس الزوج
    already = s.execute(
        select(Referral).where(Referral.referrer_user_id == referrer.id,
                               Referral.referee_user_id == referee.id)
    ).scalar_one_or_none()
    if already and already.credited_at:
        return

    # امنح المكافأة
    if not already:
        already = Referral(referrer_user_id=referrer.id,
                           referee_user_id=referee.id,
                           credited_hours=REF_BONUS_HOURS,
                           credited_at=_utcnow(),
                           note="paid_plan_credit")
        s.add(already)
    else:
        already.credited_hours = REF_BONUS_HOURS
        already.credited_at = _utcnow()

    now = _utcnow()
    base = _as_aware(referrer.end_at) if (referrer.end_at and _as_aware(referrer.end_at) and _as_aware(referrer.end_at) > now) else now
    referrer.end_at = base + timedelta(hours=int(REF_BONUS_HOURS))
    s.flush()

# ---------------------------
# وظائف الاشتراك
# ---------------------------
def is_active(s, tg_user_id: int) -> bool:
    u = s.execute(select(User).where(User.tg_user_id == tg_user_id)).scalar_one_or_none()
    if not u or not u.end_at:
        return False
    return _as_aware(u.end_at) > _utcnow()

def start_trial(s, tg_user_id: int) -> bool:
    u = get_or_create_user(s, tg_user_id)
    if u.trial_used:
        return False
    u.trial_used = True
    base = _utcnow()
    if u.end_at and _as_aware(u.end_at) > base:
        base = _as_aware(u.end_at)
    u.end_at = base + timedelta(days=1)
    u.plan = "trial"
    return True

def approve_paid(s, tg_user_id: int, plan: str, duration_days: int, tx_hash: str | None = None) -> datetime:
    u = get_or_create_user(s, tg_user_id)
    now = _utcnow()
    base = _as_aware(u.end_at) if (u.end_at and _as_aware(u.end_at) and _as_aware(u.end_at) > now) else now
    u.end_at = base + timedelta(days=int(duration_days))
    u.plan = plan
    if tx_hash:
        u.last_tx_hash = tx_hash
    s.flush()

    # منح المُحيل مكافأة إحالة (مرة واحدة) بعد أي تفعيل مدفوع
    try:
        if plan in ("2w", "4w"):  # نمنح فقط عند باقة مدفوعة (وليس gift1d مثلاً)
            _award_referrer_if_eligible(s, u)
    except Exception as e:
        logger.warning(f"Referral bonus grant failed: {e}")

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
    signals = s.execute(select(func.count(Trade.id)).where(Trade.opened_at >= since)).scalar() or 0
    open_now = s.execute(select(func.count(Trade.id)).where(Trade.status == "open")).scalar() or 0
    tp1 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp1", Trade.closed_at >= since)).scalar() or 0
    tp2 = s.execute(select(func.count(Trade.id)).where(Trade.result == "tp2", Trade.closed_at >= since)).scalar() or 0
    sl = s.execute(select(func.count(Trade.id)).where(Trade.result == "sl", Trade.closed_at >= since)).scalar() or 0

    r_sum = 0.0
    for t in s.execute(select(Trade).where(Trade.status == "closed", Trade.closed_at >= since)).scalars().all():
        if t.r_multiple is not None:
            r_sum += float(t.r_multiple); continue
        try:
            risk = max(float(t.entry) - float(t.sl), 1e-9)
            if t.result == "tp1":   r_sum += (float(t.tp1) - float(t.entry)) / risk
            elif t.result == "tp2": r_sum += (float(t.tp2) - float(t.entry)) / risk
            elif t.result == "sl":  r_sum += -1.0
        except Exception:
            pass

    wins = tp1 + tp2
    losses = sl
    total = wins + losses
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0

    return {"signals": signals, "open": open_now, "tp1": tp1, "tp2": tp2, "tp_total": tp1 + tp2, "sl": sl, "win_rate": win_rate, "r_sum": round(r_sum, 2)}

def get_stats_24h(s) -> dict:
    return _period_stats(s, _utcnow() - timedelta(hours=24))

def get_stats_7d(s) -> dict:
    return _period_stats(s, _utcnow() - timedelta(days=7))

# ---------------------------
# Leader Lock APIs
# ---------------------------
def try_acquire_leader_lock(name: str, holder: str) -> bool:
    with SessionLocal() as s:
        if s.get(Lock, name): return False
        s.add(Lock(name=name, holder=holder))
        try: s.commit(); return True
        except IntegrityError:
            s.rollback(); return False

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

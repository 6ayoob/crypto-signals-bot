from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime, timedelta

engine = create_engine("sqlite:///bot.db")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class Subscription(Base):
    __tablename__ = "subscriptions"
    user_id = Column(Integer, primary_key=True)
    end_at = Column(DateTime)
    trial_used = Column(Boolean, default=False)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String)
    side = Column(String)
    entry = Column(String)
    sl = Column(String)
    tp1 = Column(String)
    tp2 = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(engine)

def get_session():
    return SessionLocal()

# ✅ اشتراك فعال؟
def is_active(session, user_id):
    sub = session.get(Subscription, user_id)
    return bool(sub and sub.end_at > datetime.utcnow())

# ✅ تجربة مجانية يوم واحد
def start_trial(session, user_id):
    sub = session.get(Subscription, user_id)
    if sub and sub.trial_used:
        return False
    end_at = datetime.utcnow() + timedelta(days=1)
    if not sub:
        sub = Subscription(user_id=user_id, end_at=end_at, trial_used=True)
        session.add(sub)
    else:
        sub.end_at = end_at
        sub.trial_used = True
    session.commit()
    return True

# ✅ تفعيل مدفوع
def approve_paid(session, user_id, plan, duration, tx_hash=None):
    end_at = datetime.utcnow() + timedelta(days=duration)
    sub = session.get(Subscription, user_id)
    if not sub:
        sub = Subscription(user_id=user_id, end_at=end_at, trial_used=True)
        session.add(sub)
    else:
        sub.end_at = end_at
    session.commit()
    return end_at

# ✅ الصفقات
def count_open_trades(session):
    return session.query(Trade).count()

def add_trade(session, symbol, side, entry, sl, tp1, tp2):
    t = Trade(symbol=symbol, side=side, entry=entry, sl=sl, tp1=tp1, tp2=tp2)
    session.add(t)
    session.commit()

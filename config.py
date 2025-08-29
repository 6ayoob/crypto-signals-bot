# config.py — يُقرأ من المتغيرات البيئية فقط (لا أسرار داخل الكود)
import os

def _parse_admin_ids(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]

def _parse_channel_id(s: str):
    """يدعم -100... كعدد أو @username كسلسلة"""
    if not s:
        return None
    s = s.strip()
    if (s.startswith("-") and s[1:].isdigit()) or s.isdigit():
        try:
            return int(s)
        except Exception:
            pass
    return s  # @username

# أسرار/تعريفات أساسية
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # إجباري
TELEGRAM_CHANNEL_ID = _parse_channel_id(os.getenv("TELEGRAM_CHANNEL_ID"))  # إجباري (int -100... أو @username)
ADMIN_USER_IDS = _parse_admin_ids(os.getenv("ADMIN_USER_IDS", ""))  # اختياري (قائمة مفصولة بفواصل)
USDT_TRC20_WALLET = os.getenv("USDT_TRC20_WALLET", "")

# إعدادات عامة مع قيم افتراضية
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "8"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Riyadh")
DAILY_REPORT_HOUR_LOCAL = int(os.getenv("DAILY_REPORT_HOUR_LOCAL", "9"))
PRICE_2_WEEKS_USD = float(os.getenv("PRICE_2_WEEKS_USD", "30"))
PRICE_4_WEEKS_USD = float(os.getenv("PRICE_4_WEEKS_USD", "60"))
SUB_DURATION_2W = int(os.getenv("SUB_DURATION_2W", "14"))
SUB_DURATION_4W = int(os.getenv("SUB_DURATION_4W", "28"))

# فحوصات مبكرة مفيدة أثناء التشغيل
if not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("Set TELEGRAM_CHANNEL_ID to -100... (private channel) or @username (public).")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment.")

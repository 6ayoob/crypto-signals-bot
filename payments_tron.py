# payments_tron.py — تحقق تلقائي من دفعات USDT على TRON (TRC20) باستخدام "رقم المرجع (TxID)"
# تحسينات: Retries + Backoff، كاش لِـ decimals، سماحية مبلغ بسيطة، رسائل أوضح، جلسة Requests.
import os
import re
import time
import math
import requests
from typing import Optional, Tuple, Dict, Any, Callable
from functools import lru_cache

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io").rstrip("/")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "").strip()  # إن تُرك فارغاً سنطابق بالرمز "USDT"
RECEIVER_WALLET = (os.getenv("USDT_TRC20_WALLET") or "").strip()  # مطلوب
TIMEOUT = int(os.getenv("TRONGRID_TIMEOUT", "15"))
MIN_CONF = int(os.getenv("TRON_MIN_CONF", "0"))
AMOUNT_TOLERANCE_USDT = float(os.getenv("AMOUNT_TOLERANCE_USDT", "0.01"))  # سماحية فروقات بسيطة

# تطابق أي TxID سداسي بطول 64 حتى لو ضمن رابط
_TXID_RE = re.compile(r"(?i)\b([A-F0-9]{64})\b")

# جلسة requests مشتركة
_session = requests.Session()
def _headers() -> dict:
    hdr = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def _with_retries(fn: Callable[[], requests.Response], max_attempts: int = 3, base_delay: float = 0.7) -> requests.Response:
    """
    تنفيذ الطلب مع إعادة محاولة تلقائية على الحالات الشائعة:
    - 429 Too Many Requests (يحترم Retry-After إن وجد)
    - 5xx أخطاء مزود
    - أخطاء شبكة/مهلة
    """
    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = fn()
            if resp.status_code == 429:
                # حاول احترام Retry-After، وإلا Backoff
                ra = resp.headers.get("Retry-After")
                try:
                    sleep_for = float(ra)
                except Exception:
                    sleep_for = base_delay * (2 ** (attempt - 1))
                time.sleep(min(5.0, sleep_for))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            time.sleep(base_delay * (2 ** (attempt - 1)))
    # إذا فشلت كل المحاولات
    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown error while calling TronGrid")

def extract_txid(ref_or_url: str) -> Optional[str]:
    """
    يستخرج رقم المرجع (TxID) من نصّ/رابط قام المستخدم بلصقه (Tronscan / TronLink / نص خام).
    أمثلة:
      - 7F3D...ABCD (64 hex)
      - https://tronscan.org/#/transaction/<txid>
      - tronlink://transaction?hash=<txid>
    """
    if not ref_or_url:
        return None
    m = _TXID_RE.search(ref_or_url.strip())
    return m.group(1) if m else None

# ---------------------------
# استدعاءات TronGrid
# ---------------------------
def _get_json(url: str) -> Dict[str, Any] | None:
    def do():
        return _session.get(url, timeout=TIMEOUT, headers=_headers())
    resp = _with_retries(do)
    try:
        return resp.json()
    except Exception:
        return None

def get_tx(txid: str) -> Optional[dict]:
    """
    تفاصيل معاملة عبر TronGrid:
      GET /v1/transactions/{txid}
    """
    data = _get_json(f"{TRONGRID_BASE}/v1/transactions/{txid}")
    if not isinstance(data, dict):
        return None
    arr = data.get("data")
    return arr[0] if isinstance(arr, list) and arr else None

def get_tx_events(txid: str) -> list[dict]:
    """
    أحداث المعاملة (أدق لاستخراج تحويلات TRC20):
      GET /v1/transactions/{txid}/events
    """
    data = _get_json(f"{TRONGRID_BASE}/v1/transactions/{txid}/events") or {}
    arr = data.get("data")
    return arr if isinstance(arr, list) else []

def get_latest_block_number() -> Optional[int]:
    """
    آخر رقم بلوك (لاحتساب التأكيدات عند ضبط MIN_CONF>0).
    TronGrid: GET /v1/blocks?limit=1&sort=-number
    """
    data = _get_json(f"{TRONGRID_BASE}/v1/blocks?limit=1&sort=-number") or {}
    arr = data.get("data")
    if isinstance(arr, list) and arr:
        num = arr[0].get("number")
        try:
            return int(num)
        except Exception:
            return None
    return None

@lru_cache(maxsize=64)
def get_token_decimals(contract_addr: str) -> Optional[int]:
    """
    يجلب Decimals لعنوان عقد TRC20:
      GET /v1/contracts/{contract}
    (مخزّن في كاش لتقليل الاستدعاءات)
    """
    if not contract_addr:
        return None
    data = _get_json(f"{TRONGRID_BASE}/v1/contracts/{contract_addr}") or {}
    arr = data.get("data")
    if isinstance(arr, list) and arr:
        token_info = arr[0].get("tokenInfo") or {}
        dec = token_info.get("decimals")
        try:
            return int(dec) if dec is not None else None
        except Exception:
            return None
    return None

# ---------------------------
# مساعدات مطابقة/مبالغ
# ---------------------------
def _ok_symbol_or_contract_from_event(ev: dict) -> bool:
    """
    يطابق نوع الأصل داخل حدث/سجل.
    إن زُوِّد عقد USDT عبر env سنطابق به؛ وإلا سنبحث عن الرمز "USDT" إن توفر.
    """
    contract = ev.get("contract_address") or ev.get("contract") or ev.get("contractAddress")
    symbol = (ev.get("symbol") or ev.get("token_symbol") or "").upper()
    if USDT_CONTRACT:
        return contract == USDT_CONTRACT
    return symbol == "USDT"

def _amount_from_transfer_event(ev: dict) -> Optional[float]:
    """
    يستخرج المبلغ من حدث TRC20. بعض الصيغ تضع القيمة خامًا (integer) مع حقل decimals،
    وأخرى تتطلب جلب decimals من العقد.
    """
    try:
        value = ev.get("value") or ev.get("amount") or ev.get("amount_str")
        if value is None:
            return None
        decimals = ev.get("decimals")
        if decimals is None:
            contract = ev.get("contract_address") or ev.get("contract") or ev.get("contractAddress")
            decimals = get_token_decimals(contract) if contract else 6
        decimals = int(decimals or 6)
        # القيمة قد تكون str/int — نحولها لـ float بأمان
        return float(str(value)) / (10 ** decimals)
    except Exception:
        return None

def _enough_confirmations(tx: dict) -> bool:
    """
    إن MIN_CONF==0 نكتفي بفحص نجاح المعاملة.
    عند MIN_CONF>0 نحاول حساب (latest_block - tx.blockNumber) >= MIN_CONF
    إن تعذّر الحصول على البلوكات نعتبرها كافية (Fail-open) لتفادي تعطيل المستخدمين.
    """
    if MIN_CONF <= 0:
        return True
    try:
        tx_block = tx.get("blockNumber") or tx.get("blockNumberRaw") or tx.get("block")  # حسب ما ترسله TronGrid
        tx_block = int(tx_block) if tx_block is not None else None
    except Exception:
        tx_block = None

    latest = get_latest_block_number()
    if tx_block is not None and latest is not None:
        return (latest - tx_block) >= MIN_CONF
    return True  # تعذّر التقدير: سمح بالمرور

def _amount_ok(amount: float, min_amount: float) -> bool:
    """يسمح بفارق بسيط (tolerance) لعلاج فروقات الكسور."""
    return (amount + 1e-9) + AMOUNT_TOLERANCE_USDT >= float(min_amount)

# ---------------------------
# نقطة الدخول المستخدمة في البوت
# ---------------------------
def find_trc20_transfer_to_me(ref_or_url: str, min_amount: float) -> Tuple[bool, str]:
    """
    التحقق أن المعاملة بالـ «رقم المرجع (TxID)» تمّت بنجاح وتحمل تحويل USDT TRC20 إلى محفظتنا بالمبلغ المطلوب.
    ترجع (ok, info):
      - ok=True  => info = المبلغ المستلم كنص
      - ok=False => info = سبب الفشل (رسالة عربية واضحة)
    """
    if not RECEIVER_WALLET:
        return False, "المحفظة غير مهيأة لدى الخادم. الرجاء إبلاغ الدعم (USDT_TRC20_WALLET مفقود)."

    txid = extract_txid(ref_or_url or "")
    if not txid:
        return False, "لم أفهم «رقم المرجع». أرسل رقم المرجع (64 خانة) أو رابط المعاملة من Tronscan."

    # 1) جلب المعاملة والتأكد من نجاحها
    try:
        tx = get_tx(txid)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "Unknown")
        return False, f"تعذّر الاتصال بواجهة TRON (HTTP {code}). جرّب لاحقًا أو تأكد من رقم المرجع."
    except Exception as e:
        return False, f"فشل الاتصال بواجهة TRON: {e}"

    if not tx:
        return False, "لم أجد معاملة بهذا «رقم المرجع». تأكد من نسخه صحيحًا أو أرسل رابط Tronscan."

    # TronGrid يعيد ret = [{contractRet:"SUCCESS"}]
    ret = tx.get("ret") or []
    if not ret or (ret[0].get("contractRet") != "SUCCESS"):
        return False, "المعاملة ليست «ناجحة» بعد (قد تكون قيد التنفيذ). أعد المحاولة لاحقًا."

    if not _enough_confirmations(tx):
        return False, "المعاملة رُصدت لكن ننتظر مزيدًا من التأكيدات على الشبكة."

    # 2) محاولة دقيقة عبر /events
    try:
        events = get_tx_events(txid)
    except Exception:
        events = []

    # 2.a) تحليل أحداث TRC20 الكلاسيكية
    for ev in events:
        event_name = (ev.get("event_name") or "").lower()
        result = ev.get("result") or {}
        to_addr = (result.get("to") or ev.get("to_address") or ev.get("to") or "").strip()
        if event_name != "transfer" and "to" not in result and "to_address" not in ev:
            continue

        # فلترة الأصل (USDT أو العقد المحدد)
        ev_for_match = {
            "contract_address": ev.get("contract_address") or ev.get("contract") or ev.get("contractAddress"),
            "symbol": (ev.get("token_symbol") or ev.get("symbol") or "").upper(),
        }
        if not _ok_symbol_or_contract_from_event(ev_for_match):
            continue

        if to_addr != RECEIVER_WALLET:
            continue

        value = result.get("value") or ev.get("amount") or ev.get("amount_str")
        ev_for_amount = {
            "decimals": ev.get("decimals"),
            "value": value,
            "contract_address": ev_for_match["contract_address"]
        }
        amount = _amount_from_transfer_event(ev_for_amount)
        if amount is None:
            continue

        if _amount_ok(amount, min_amount):
            return True, f"{amount:.6f}"

    # 2.b) احتياط: بعض عوائد /v1/transactions/{txid} قد تحتوي trc20TransferInfo/tokenTransferInfo
    trc20_list = tx.get("trc20TransferInfo") or tx.get("tokenTransferInfo") or []
    for ev in trc20_list:
        to_addr = (ev.get("to_address") or ev.get("to") or "").strip()
        if not _ok_symbol_or_contract_from_event(ev):
            continue
        if to_addr != RECEIVER_WALLET:
            continue
        amount = _amount_from_transfer_event(ev)
        if amount is None:
            continue
        if _amount_ok(amount, min_amount):
            return True, f"{amount:.6f}"

    return False, "لم أعثر على إيداع USDT صالح إلى محفظتنا بهذه المعاملة وبالمبلغ المطلوب."

# ملاحظة: لإظهار تلميح ودّي في واجهات البوت:
REFERENCE_HINT = (
    "🔎 <b>رقم المرجع (TxID)</b> هو رقم مكوّن من 64 رمزًا يُميّز تحويلك على شبكة TRON.\n"
    "يمكنك نسخه من محفظتك (TronLink/Trust/Tronscan) أو إلصاق رابط المعاملة، وسألتقطه تلقائيًا."
)

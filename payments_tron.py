# payments_tron.py — تحقق تلقائي من دفعات USDT على TRON (TRC20) باستخدام "رقم المرجع (TxID)"
# - يدعم لصق رقم المرجع مباشرة أو رابط Tronscan/TronLink لاستخراج الـ TxID تلقائياً.
# - يعتمد TronGrid API (يفضّل وضع TRONGRID_API_KEY لتفادي حدود المعدل).
# - إن لم تحدد عقد USDT في env سنطابق بالرمز "USDT".
#
# بيئة التشغيل (Environment):
#   TRONGRID_BASE          (اختياري) افتراضياً https://api.trongrid.io
#   TRONGRID_API_KEY       (اختياري لكنه مُستحسن)
#   USDT_TRC20_CONTRACT    (اختياري) إن وضعته سنطابق بالعقد بدلاً من الرمز
#   USDT_TRC20_WALLET      (مطلوب)  محفظتك على TRON ليتحقق الإيداع إليها
#   TRONGRID_TIMEOUT       (اختياري) مهلة الطلب بالثواني، افتراضياً 15
#   TRON_MIN_CONF          (اختياري) حد أدنى للتأكيدات، افتراضياً 0 (ترون غالباً فوري)
#
# واجهة الاستدعاء في البوت:
#   ok, info = find_trc20_transfer_to_me(ref_or_url, min_amount)
#   - ok=True  => تم العثور على تحويل صالح؛ info = "المبلغ المستلم"
#   - ok=False => فشل التحقق؛ info = سبب واضح بالعربية للمستخدم/الأدمن

import os
import re
import requests
from typing import Optional, Tuple

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "")  # إن تُرك فارغاً سنطابق بالرمز "USDT"
RECEIVER_WALLET = os.getenv("USDT_TRC20_WALLET")      # محفظتك — مطلوب
TIMEOUT = int(os.getenv("TRONGRID_TIMEOUT", "15"))
MIN_CONF = int(os.getenv("TRON_MIN_CONF", "0"))

# تطابق أي سلاسل TxID سداسية بطول 64 حتى لو ضمن رابط
_TXID_RE = re.compile(r"(?i)\b([A-F0-9]{64})\b")

def _headers() -> dict:
    hdr = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def extract_txid(ref_or_url: str) -> Optional[str]:
    """
    يستخرج رقم المرجع (TxID) من نصّ/رابط قام المستخدم بلصقه (Tronscan / TronLink / نص خام).
    أمثلة مدعومة:
      - 7F3D...ABCD (64 hex)
      - https://tronscan.org/#/transaction/<txid>
      - tronlink://transaction?hash=<txid>
    """
    if not ref_or_url:
        return None
    m = _TXID_RE.search(ref_or_url.strip())
    return m.group(1) if m else None

def get_tx(txid: str) -> Optional[dict]:
    """
    يجلب تفاصيل معاملة عبر TronGrid:
      GET /v1/transactions/{txid}
    يرجع dict للمعاملة أو None إن لم توجد.
    """
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}"
    r = requests.get(url, timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    data = r.json()
    arr = data.get("data") if isinstance(data, dict) else None
    return arr[0] if arr else None

def _ok_symbol_or_contract(ev: dict) -> bool:
    """
    يطابق نوع الأصل: إمّا وفق عقد USDT المحدّد، أو بواسطة الرمز "USDT" إن لم يُحدَّد عقد.
    """
    contract = ev.get("contract_address")
    symbol = (ev.get("symbol") or "").upper()
    if USDT_CONTRACT:
        return contract == USDT_CONTRACT
    return symbol == "USDT"

def _amount_from_event(ev: dict) -> Optional[float]:
    """
    يحسب المبلغ الفعلي من سجّل تحويل TRC20 وفق الحقول المتاحة.
    """
    try:
        decimals = int(ev.get("decimals") or 6)
        raw_val = ev.get("amount_str") or ev.get("amount") or "0"
        return float(raw_val) / (10 ** decimals)
    except Exception:
        return None

def _enough_confirmations(tx: dict) -> bool:
    """
    TRON عادةً يؤكد بسرعة وتظهر الحالة SUCCESS.
    إن رغبت بالتحقق من الارتفاع إلى عدد تأكيدات معيّن، يمكن استخدام block/confirmation_count لو توفر.
    هنا نكتفي بـ MIN_CONF == 0 (افتراضياً).
    """
    if MIN_CONF <= 0:
        return True
    # إن توفر حقول تأكيدات مستقبلاً يمكن إضافتها هنا.
    return True

def find_trc20_transfer_to_me(ref_or_url: str, min_amount: float) -> Tuple[bool, str]:
    """
    التحقق أن المعاملة بالـ «رقم المرجع (TxID)» تمّت بنجاح وتحمل تحويل USDT TRC20 إلى محفظتنا بالمبلغ المطلوب.
    ترجع (ok, info):
      - ok=True  => info = المبلغ المستلم كنص
      - ok=False => info = سبب الفشل (رسالة عربية واضحة)
    """
    if not RECEIVER_WALLET:
        return False, "المحفظة غير مهيأة لدى الخادم. الرجاء إبلاغ الدعم (USDT_TRC20_WALLET مفقود)."

    txid = extract_txid(ref_or_url)
    if not txid:
        return False, "لم أفهم «رقم المرجع». أرسل رقم المرجع المكوّن من 64 رمزاً (أو أرسل رابط المعاملة من Tronscan)."

    try:
        tx = get_tx(txid)
    except requests.HTTPError as e:
        return False, f"تعذّر الاتصال بواجهة TRON (HTTP {e.response.status_code}). جرّب بعد قليل أو تأكد من رقم المرجع."
    except Exception as e:
        return False, f"فشل الاتصال بواجهة TRON: {e}"

    if not tx:
        return False, "لم أجد معاملة بهذا «رقم المرجع». تأكد من نسخه بشكل صحيح أو أرسل رابط Tronscan للمعاملة."

    # حالة التنفيذ
    ret = tx.get("ret") or []
    if not ret or ret[0].get("contractRet") != "SUCCESS":
        return False, "المعاملة ليست ناجحة بعد (قد تكون قيد التنفيذ). أعد المحاولة لاحقاً."

    if not _enough_confirmations(tx):
        return False, "المعاملة رُصدت ولكن ننتظر مزيداً من التأكيدات على الشبكة."

    # ابحث عن سجلات تحويل TRC20 داخل المعاملة
    trc20_list = tx.get("trc20TransferInfo") or tx.get("tokenTransferInfo") or []
    if not trc20_list:
        return False, "لا توجد سجلات تحويل TRC20 في هذه المعاملة. تأكد أنك أرسلت USDT (TRC20)."

    for ev in trc20_list:
        to_addr = ev.get("to_address")
        if not _ok_symbol_or_contract(ev):
            continue
        if to_addr != RECEIVER_WALLET:
            continue

        amount = _amount_from_event(ev)
        if amount is None:
            continue

        if amount + 1e-9 >= float(min_amount):
            # نجاح
            return True, f"{amount:.6f}"

    return False, "لم أعثر على إيداع USDT صالح إلى محفظتنا بهذه المعاملة وبالمبلغ المطلوب."

# ملاحظة: لإظهار تلميح ودّي في واجهات البوت:
REFERENCE_HINT = (
    "🔎 *رقم المرجع (TxID)* هو رقم مكوّن من 64 رمزاً يُميّز تحويلك على شبكة TRON.\n"
    "يمكنك نسخه من محفظتك (TronLink/Trust/Tronscan) أو إلصاق رابط المعاملة هنا، وسألتقطه تلقائياً."
)

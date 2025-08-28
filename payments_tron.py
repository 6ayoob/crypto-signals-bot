# payments_tron.py — تحقق تلقائي من دفعات USDT على TRON (TRC20) مع تحسينات
import os
import time
import requests
from typing import Dict, List, Optional, Tuple

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io").rstrip("/")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")  # مفتاح TronGrid (موصى به)
USDT_CONTRACT = (os.getenv("USDT_TRC20_CONTRACT", "") or "").strip()  # عقد USDT (اختياري)
RECEIVER_WALLET = (os.getenv("USDT_TRC20_WALLET", "") or "").strip()  # محفظتك (مطلوب)

TIMEOUT = 15
RETRIES = 2           # محاولتان إضافيتان
BACKOFF_SEC = 0.8     # تأخير بين المحاولات

def _headers() -> Dict[str, str]:
    hdr: Dict[str, str] = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def _get_json(url: str) -> Optional[Dict]:
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=_headers())
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == RETRIES:
                raise
            time.sleep(BACKOFF_SEC * (attempt + 1))
    return None  # لن يصل هنا غالبًا

def _extract_tx_object(data: Dict) -> Optional[Dict]:
    """TronGrid غالبًا يرجع {"data":[...]} — نأخذ أول عنصر إن وُجد."""
    if isinstance(data, dict):
        arr = data.get("data") or []
        return arr[0] if arr else None
    return None

def get_tx(txid: str) -> Optional[Dict]:
    """يجلب تفاصيل المعاملة العامة (نجاح/فشل + ملخص)."""
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}"
    data = _get_json(url)
    return _extract_tx_object(data) if data else None

def get_tx_events(txid: str) -> List[Dict]:
    """
    جلب أحداث المعاملة (خصوصًا Transfer لعقود TRC20).
    بعض حسابات TronGrid تتطلب هذا الاندبوينت لاستخراج التحويلات.
    """
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}/events"
    data = _get_json(url) or {}
    return data.get("data") or []

def get_tx_trc20(txid: str) -> List[Dict]:
    """
    اندبوينت بديل يتوفر في بعض إصدارات TronGrid (ليست موحدة دائمًا).
    نجربه كخطة C لو لم نجد شيئًا في /events.
    """
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}/trc20"
    try:
        data = _get_json(url) or {}
        return data.get("data") or []
    except Exception:
        return []

def _parse_amount(raw: Optional[str], decimals: Optional[int]) -> Optional[float]:
    try:
        d = int(decimals or 6)
        # بعض الاستجابات تعطي amount كعدد صحيح أو نص
        val = float(str(raw))
        # إن كان raw بالـ base (غير شائع هنا)، يكون الاسم amount_str/amount
        # لكن لو كان val كبير جدًا قد يدل على أنه raw بدون تقسيم — نتعامل مع الحقول الأخرى في الأعلى غالبًا.
        return val / (10 ** d) if val > 10_000 else val
    except Exception:
        return None

def _match_usdt(contract: Optional[str], symbol: Optional[str]) -> bool:
    # إن حدّدت عقد USDT فاعتمد عليه فقط
    if USDT_CONTRACT:
        return (contract or "").strip() == USDT_CONTRACT
    # وإلا تحقّق بالرمز
    return (symbol or "").upper() == "USDT"

def _match_receiver(to_addr: Optional[str]) -> bool:
    return (to_addr or "").strip() == RECEIVER_WALLET

def _scan_trc20_info_list(items: List[Dict], min_amount: float) -> Tuple[bool, str]:
    for ev in items:
        to_addr = ev.get("to_address") or ev.get("to")
        contract = ev.get("contract_address") or ev.get("contract")
        symbol = (ev.get("symbol") or "").upper()

        # أولوية amount_str ثم amount
        raw_val = ev.get("amount_str", ev.get("amount"))
        decimals = ev.get("decimals", 6)

        amount = _parse_amount(raw_val, decimals)
        if amount is None:
            continue

        if not _match_usdt(contract, symbol):
            continue
        if not _match_receiver(to_addr):
            continue
        if amount + 1e-9 >= float(min_amount):
            return True, f"{amount:.6f}"
    return False, "لم أجد تحويل TRC20 صالحًا بالمبلغ المطلوب"

def find_trc20_transfer_to_me(txid: str, min_amount: float) -> Tuple[bool, str]:
    """
    التحقق أن المعاملة:
      - ناجحة (SUCCESS)
      - تتضمن تحويل TRC20 (USDT) إلى محفظتنا
      - مبلغها >= min_amount
    ترجع (ok, info) — إذا ok=True فـ info يحتوي المبلغ الفعلي المستلم كنص.
    """
    if not RECEIVER_WALLET:
        return (False, "المحفظة غير مُهيّأة: USDT_TRC20_WALLET مفقود")
    if not TRONGRID_API_KEY:
        # ليس إجباريًا لكنه يرفع احتمال حدود المعدل/الفشل
        print("WARN: TRONGRID_API_KEY غير مضبوط — قد تواجه حدودًا أو بطئًا في TronGrid.")

    # 1) تفاصيل المعاملة العامة
    try:
        tx = get_tx(txid)
    except Exception as e:
        return (False, f"فشل الاتصال بواجهة TronGrid (TX): {e}")

    if not tx:
        return (False, "لم يتم العثور على المعاملة")

    # 2) تحقق نجاح المعاملة
    ret = tx.get("ret") or []
    if not ret or ret[0].get("contractRet") != "SUCCESS":
        return (False, "المعاملة ليست ناجحة بعد (أو فشلت)")

    # 3) حاول قراءة تحاويل TRC20 مباشرة إن توفر الحقل
    trc20_list = tx.get("trc20TransferInfo") or tx.get("tokenTransferInfo") or []
    ok, info = _scan_trc20_info_list(trc20_list, min_amount)
    if ok:
        return (True, info)

    # 4) خطة B: جلب الأحداث
    try:
        events = get_tx_events(txid)
    except Exception as e:
        events = []
        print("WARN: فشل /events:", e)

    # بعض فورمات الأحداث تأتي كـ parameter_map بداخلها
    if events:
        extracted = []
        for ev in events:
            # نحاول التوحيد إلى نفس شكل trc20TransferInfo
            # غالبًا event_name == "Transfer" و parameter: {from, to, value}
            param = ev.get("parameter", {}) or ev.get("parameter_map", {})
            extracted.append({
                "to_address": param.get("to") or param.get("_to"),
                "contract_address": ev.get("contract_address") or ev.get("contract"),
                "symbol": (ev.get("tokenInfo", {}) or {}).get("symbol") or ev.get("token_name"),
                "amount_str": param.get("value") or param.get("_value") or ev.get("amount"),
                "decimals": (ev.get("tokenInfo", {}) or {}).get("decimals", 6)
            })
        ok, info = _scan_trc20_info_list(extracted, min_amount)
        if ok:
            return (True, info)

    # 5) خطة C: إن توفرت /trc20
    trc20_alt = get_tx_trc20(txid)
    if trc20_alt:
        ok, info = _scan_trc20_info_list(trc20_alt, min_amount)
        if ok:
            return (True, info)

    return (False, "لم أجد تحويل USDT TRC20 صالحًا لهذه المحفظة بهذا txid وبالمبلغ المطلوب")

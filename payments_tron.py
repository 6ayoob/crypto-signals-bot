# payments_tron.py — تحقق تلقائي من دفعات USDT على TRON (TRC20)
import os
import requests

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")  # مفتاح TronGrid (مستحسن/مطلوب)
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "")  # عقد USDT (اختياري للتقييد)
RECEIVER_WALLET = os.getenv("USDT_TRC20_WALLET")      # محفظتك (مطلوب!)
TIMEOUT = 15

def _headers():
    hdr = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def get_tx(txid: str):
    """
    يجلب تفاصيل معاملة بالـ txid.
    يرفع استثناء لو فشل الاتصال.
    """
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}"
    r = requests.get(url, timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        arr = data.get("data") or []
        return arr[0] if arr else None
    return None

def find_trc20_transfer_to_me(txid: str, min_amount: float) -> tuple[bool, str]:
    """
    التحقق أن المعاملة:
      - ناجحة (SUCCESS)
      - تتضمن تحويل TRC20 إلى محفظتنا
      - من نوع USDT (بالرمز أو بالعقد إذا وضعته)
      - مبلغها >= min_amount
    ترجع (ok, info) — إذا ok=True فـ info يحتوي المبلغ الفعلي المستلم.
    """
    if not RECEIVER_WALLET:
        return False, "المحفظة غير مهيأة (USDT_TRC20_WALLET مفقود)"

    tx = None
    try:
        tx = get_tx(txid)
    except Exception as e:
        return False, f"فشل الاتصال بواجهة TRON: {e}"

    if not tx:
        return False, "لم يتم العثور على المعاملة"

    ret = tx.get("ret", [])
    if not ret or ret[0].get("contractRet") != "SUCCESS":
        return False, "المعاملة ليست ناجحة بعد (أو ما زالت قيد التأكيد)"

    trc20_list = tx.get("trc20TransferInfo") or tx.get("tokenTransferInfo") or []
    if not trc20_list:
        return False, "لا توجد سجلات تحويل TRC20 في هذه المعاملة"

    for ev in trc20_list:
        to_addr = ev.get("to_address")
        contract = ev.get("contract_address")
        symbol = (ev.get("symbol") or "").upper()
        decimals = int(ev.get("decimals") or 6)
        raw_val = ev.get("amount_str") or ev.get("amount") or "0"

        try:
            amount = float(raw_val) / (10 ** decimals)
        except Exception:
            continue

        if USDT_CONTRACT:
            if contract != USDT_CONTRACT:
                continue
        else:
            if symbol and symbol != "USDT":
                continue

        if to_addr != RECEIVER_WALLET:
            continue

        if amount + 1e-9 >= float(min_amount):
            return True, f"{amount:.6f}"

    return False, "لم أجد تحويل USDT صالحًا لهذه المحفظة بهذا txid وبالمبلغ المطلوب"

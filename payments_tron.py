# payments_tron.py
import os, requests

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")  # ضع مفتاحك من TronGrid
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "")  # اختياري: عقد USDT على TRON إن رغبت بالتقييد
RECEIVER_WALLET = os.getenv("USDT_TRC20_WALLET")      # محفظتك (من config/env)
TIMEOUT = 15

def _headers():
    hdr = {"Accept":"application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def get_tx(txid: str):
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}"
    r = requests.get(url, timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    data = r.json()
    return data.get("data", [None])[0]  # قد تكون قائمة

def find_trc20_transfer_to_me(txid: str, min_amount: float) -> tuple[bool, str]:
    """
    يتحقق أن الـ txid:
      - معاملة مؤكدة وناجحة
      - تحويل TRC20 USDT إلى محفظتنا
      - المبلغ >= min_amount
    يعيد (ok, reason_or_amount)
    """
    tx = get_tx(txid)
    if not tx:
        return False, "لم يتم العثور على المعاملة"

    # حالة عامة
    ret = tx.get("ret", [])
    if not ret or ret[0].get("contractRet") != "SUCCESS":
        return False, "المعاملة ليست ناجحة بعد"

    # سجلات الأحداث (logs) غالبًا تحت 'log' أو 'trc20TransferInfo' في بعض الاستجابات
    trc20_list = tx.get("trc20TransferInfo") or tx.get("tokenTransferInfo") or []

    if not trc20_list:
        # إذا لم توجد قائمة جاهزة، قد تحتاج نداءً لواجهة ثانية لسجلات العقد (تبسيطًا نكتفي بهذا)
        return False, "لا توجد بيانات تحويل TRC20 في هذه المعاملة"

    found = False
    for ev in trc20_list:
        to_addr = ev.get("to_address")
        contract = ev.get("contract_address")
        symbol = ev.get("symbol", "").upper()
        decimals = ev.get("decimals", 6)
        raw_val = ev.get("amount_str") or ev.get("amount") or "0"
        try:
            amount = float(raw_val) / (10 ** int(decimals))
        except Exception:
            continue

        # تحقق أنه USDT (أو قارن عقد USDT لو وضعته في env)
        if symbol != "USDT":
            if USDT_CONTRACT and contract != USDT_CONTRACT:
                continue  # ليس عقد USDT الذي نريده
            # لو ما عندك symbol نستخدم العقد فقط

        # تحقق أنه التحويل إلى محفظتنا
        if to_addr != RECEIVER_WALLET:
            continue

        # تحقق من الحد الأدنى
        if amount + 1e-9 >= float(min_amount):
            found = True
            return True, f"{amount:.6f}"

    if not found:
        return False, "لم أجد تحويل USDT صالح لهذه المحفظة بهذا txid"

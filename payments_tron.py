# payments_tron.py — فحص تلقائي لمدفوعات USDT TRC20
import os
import time
import requests

TRONGRID_BASE = os.getenv("TRONGRID_BASE", "https://api.trongrid.io")
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY")
USDT_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "")  # اختياري للتقييد بدقة
RECEIVER_WALLET = os.getenv("USDT_TRC20_WALLET")
TIMEOUT = 15

def _headers():
    hdr = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        hdr["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return hdr

def get_tx(txid: str):
    url = f"{TRONGRID_BASE}/v1/transactions/{txid}"
    r = requests.get(url, timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        arr = data.get("data") or []
        return arr[0] if arr else None
    return None

def find_trc20_transfer_to_me(txid: str, min_amount: float) -> tuple[bool, str]:
    if not RECEIVER_WALLET:
        return False, "المحفظة غير مهيأة (USDT_TRC20_WALLET مفقود)"
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

# -------- فحص التحويلات الواردة للمحفظة (لمطابقة الفواتير) --------
def list_recent_incoming_usdt(limit: int = 100) -> list[dict]:
    """
    يعيد قائمة تحويلات TRC20 (USDT) الواردة لمحفظتنا (الأحدث).
    يعتمد على TronGrid v1. لو تغيّر السلوك، حدّث المسار حسب وثائق TronGrid.
    """
    if not RECEIVER_WALLET:
        raise RuntimeError("USDT_TRC20_WALLET is not set")
    # only_to=true لتصفيه التحويلات الداخلة
    # ملاحظة: بعض تنصيبات TronGrid تُعيد الحقل data أو token_transfers
    url = f"{TRONGRID_BASE}/v1/accounts/{RECEIVER_WALLET}/transactions/trc20"
    params = {"limit": limit, "only_to": "true"}
    if USDT_CONTRACT:
        params["contract_address"] = USDT_CONTRACT

    r = requests.get(url, params=params, timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    js = r.json()
    rows = js.get("data") or js.get("token_transfers") or []
    out = []
    for ev in rows:
        try:
            to_addr = ev.get("to")
            contract = ev.get("contract_address") or ev.get("token_info", {}).get("address")
            symbol = (ev.get("symbol") or ev.get("token_info", {}).get("symbol") or "").upper()
            decimals = int(ev.get("decimals") or ev.get("token_info", {}).get("decimals") or 6)
            raw = ev.get("value") or ev.get("amount") or ev.get("amount_str") or "0"
            txid = ev.get("transaction_id") or ev.get("transaction_id_str") or ev.get("transaction_id_hex")
            ts = ev.get("block_timestamp") or ev.get("trigger_info", {}).get("block_timestamp")
            amount = float(raw) / (10 ** decimals)
            if USDT_CONTRACT:
                if contract != USDT_CONTRACT:
                    continue
            else:
                if symbol and symbol != "USDT":
                    continue
            if to_addr != RECEIVER_WALLET:
                continue
            out.append({
                "txid": txid,
                "amount": round(amount, 6),
                "timestamp": ts
            })
        except Exception:
            continue
    return out

def match_invoices_by_amount(invoices: list[dict], incoming: list[dict], tol: float = 0.000001):
    """
    يطابق كل فاتورة (expected_amount) مع تحويل مطابق نفس المبلغ (± tol).
    يعيد dict: invoice_id -> {"txid":..., "amount":...}
    """
    res = {}
    used_tx = set()
    for inv in invoices:
        exp_amt = round(float(inv["expected_amount"]), 6)
        for tr in incoming:
            if tr["txid"] in used_tx:
                continue
            if abs(tr["amount"] - exp_amt) <= tol:
                res[inv["id"]] = {"txid": tr["txid"], "amount": tr["amount"]}
                used_tx.add(tr["txid"])
                break
    return res

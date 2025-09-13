# -*- coding: utf-8 -*-
"""
symbols.py
بناء قائمة الرموز التي يتعامل معها البوت.

ملاحظات مهمة:
- نعيد دائمًا رموزًا بشكل "BASE/USDT" (صيغة سبوت). يقوم bot.py بتكييفها تلقائيًا إلى عقود سواب بإضافة ":USDT" إذا لزم.
- لا نعتمد على أي API خارجي بشكل حتمي؛ إن تعذّر الجلب من OKX نرجع لقائمة ثابتة كبيرة.
- نوفر _prepare_symbols() التي تُرجع (list, meta) حيث meta تحمل {'source': 'SPOT' أو 'SWAP'} للاسترشاد فقط.
"""

from __future__ import annotations
import os
import re
from typing import List, Tuple, Dict

# ============== إعدادات عامة قابلة للتهيئة عبر البيئة ==============

# نوع الأداة المستهدف (إرشادي للـ meta فقط)
INST_TYPE: str = os.getenv("INST_TYPE", "SWAP").strip().upper()  # "SPOT" أو "SWAP"
# العدد المستهدف للرموز (سنقصّ القائمة إلى هذا العدد)
TARGET_SYMBOLS_COUNT: int = int(os.getenv("TARGET_SYMBOLS_COUNT", "120"))
# حد أدنى لحجم 24 ساعة بالدولار (حاليًا إرشادي فقط إن لم تتوفر بيانات فوليوم)
MIN_24H_USD_VOL: float = float(os.getenv("MIN_24H_USD_VOL", "0"))

# استثناء رموز الرافعة (Spot Leveraged Tokens) باللاحقة التالية
_DEF_LEV = {"3L", "3S", "5L", "5S", "UP", "DOWN"}

def _env_set(name: str, default: List[str] | set[str]) -> set[str]:
    raw = os.getenv(name, "")
    if not raw:
        return set(default)
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return set(parts) if parts else set(default)

LEVERAGED_SUFFIXES: set[str] = _env_set("EXCLUDE_LEVERAGED_SUFFIXES", _DEF_LEV)

# قوائم الإدراج/الاستبعاد اليدوية
# أمثلة القيم:
#   INCLUDE_BASES="RNDR,TAO,ENA"
#   EXCLUDE_BASES="PEPE,WIF"
_INCLUDE_BASES: set[str] = _env_set("INCLUDE_BASES", [])
_EXCLUDE_BASES: set[str] = _env_set("EXCLUDE_BASES", [])
# استبعاد أزواج كاملة بصيغة BASE/QUOTE لو لزم (تفيد عند وجود كتابة خاصة في السوق)
_EXCLUDE_SYMBOLS: set[str] = {s for s in (os.getenv("EXCLUDE_SYMBOLS") or "").split(",") if s.strip()}
# يمكنك إجبار القائمة بالكامل عبر MANUAL_SYMBOLS (تفصل بفواصل). إن وُجدت تُستخدم كما هي بعد التنظيف.
_MANUAL_SYMBOLS_RAW = [s.strip().upper() for s in (os.getenv("MANUAL_SYMBOLS") or "").split(",") if s.strip()]

# ============== أدوات مساعدة ==============

def _is_leveraged_base(base: str) -> bool:
    """
    True إذا كان BASE ينتهي بلاحقة رافعة (3L/3S/5L/5S/UP/DOWN)
    """
    base = base.upper()
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf):
            return True
    return False

def _mk(base: str, quote: str = "USDT") -> str:
    return f"{base.upper()}/{quote.upper()}"

def _clean_and_dedupe(symbols: List[str]) -> List[str]:
    """
    - توحيد الصيغة إلى BASE/USDT
    - إسقاط الرموز غير الصالحة
    - إزالة المكررات
    - تطبيق قوائم الاستبعاد
    - استبعاد رموز الرافعة
    """
    out = []
    seen = set()
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s:
            continue

        # السماح بصيغ: BASE/USDT أو BASE فقط
        if "/" in s:
            base, _, quote = s.partition("/")
            base = base.strip().upper()
            quote = (quote or "USDT").strip().upper()
        else:
            base, quote = s, "USDT"

        if not re.fullmatch(r"[A-Z0-9\-]+", base):
            continue

        if _is_leveraged_base(base):
            continue

        sym = _mk(base, quote)
        if sym in _EXCLUDE_SYMBOLS:
            continue
        if base in _EXCLUDE_BASES:
            continue

        if sym not in seen:
            seen.add(sym)
            out.append(sym)

    return out

# ============== قائمة افتراضية كبيرة (Fallback) ==============
# قائمة واسعة لأشهر الرموز (سبوت) — تُختصر لاحقًا حسب TARGET_SYMBOLS_COUNT
# ملاحظة: وجود رمز هنا لا يضمن توفره على OKX؛ bot.py سيتحقق ويكيّف.
_FALLBACK_BASES = [
    # Top majors
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "DOT", "LINK", "MATIC", "NEAR", "APT", "ARB", "OP", "ATOM", "ETC", "XLM",
    "FIL", "ICP", "LTC", "BCH", "INJ", "SUI", "HBAR", "TIA", "SEI", "AAVE",
    "IMX", "RNDR", "PEPE", "WIF", "JUP", "TURBO", "PYTH", "TAO", "ENA", "SAGA",
    "GALA", "FTM", "ALGO", "EGLD", "KAS", "QNT", "RON", "CSPR", "CFX", "MINA",
    "FTT", "RUNE", "DYDX", "GMX", "SAND", "MANA", "AXS", "APE", "FLOW", "GRT",
    "CHZ", "XEC", "KAVA", "KLAY", "WLD", "ZRO", "JST", "BLUR", "TIA", "STRK",
    "W", "NEO", "ONT", "AR", "WAXP", "NOT", "AEVO", "PORTAL", "ORDI", "TONCOIN",
    "1000SATS", "MEW", "ETHFI", "BOME", "DOGS", "AERO", "OMNI", "BANANA", "IO",
    "ALT", "BTT", "FET", "AGIX", "OCEAN", "TRB", "MOVR", "SFP", "SSV", "LDO",
    "YFI", "UNI", "SUSHI", "CRV", "BAL", "COMP", "SNX", "1INCH", "ZRX", "NMR",
    "SKL", "STX", "ENS", "FLUX", "ZIL", "ANKR", "CTSI", "RVN", "ARPA", "BEL",
    "DODO", "HOOK", "ID", "BRETT", "TNSR", "ZK", "ZETA", "PRIME", "NULS",
    "BEAM", "ARKM", "COTI", "BAND", "GAL", "HIGH", "LPT", "AUDIO", "ROSE",
    "CELO", "GMT", "WOO", "HOT", "ILV", "KNC", "MASK", "POLYX", "RIF", "RSR",
    "SXP", "TOMO", "VET", "XEM",
]

# إدراج يدوي إن وُجد
if _INCLUDE_BASES:
    for b in sorted(_INCLUDE_BASES):
        if b not in _FALLBACK_BASES:
            _FALLBACK_BASES.append(b)

# ============== محاولة الجلب التلقائي من OKX (اختياري) ==============
# إذا أردت تعطيله نهائيًا: اضبط AUTO_FETCH_OKX=0
_AUTO_FETCH_OKX = os.getenv("AUTO_FETCH_OKX", "1") == "1"

def _try_fetch_okx_universe() -> List[str]:
    """
    يحاول استخراج قائمة قواعد BASE من أسواق OKX.
    - في وضع SWAP: نأخذ قواعد العقود الدائمة USDT-M (linear).
    - في وضع SPOT: نأخذ أزواج BASE/USDT.
    إن فشل الاتصال أو حدث خطأ، نرجع قائمة فارغة ليتم استخدامها الـ fallback.
    """
    try:
        import ccxt  # import داخلي كي لا يصبح هذا الاعتماد ضروريًا للبيئات التي لا تحتاجه
        ex = ccxt.okx({"enableRateLimit": True})
        markets = ex.load_markets()

        bases = []
        if INST_TYPE == "SWAP":
            for m in markets.values():
                if m.get("swap") and m.get("linear") and (m.get("settle") or "").upper() == "USDT":
                    base = (m.get("base") or "").upper()
                    quote = "USDT"
                    if base and not _is_leveraged_base(base):
                        sym = _mk(base, quote)
                        if sym not in _EXCLUDE_SYMBOLS and base not in _EXCLUDE_BASES:
                            bases.append(base)
        else:  # SPOT
            for m in markets.values():
                if m.get("spot"):
                    base = (m.get("base") or "").upper()
                    quote = (m.get("quote") or "").upper()
                    if quote == "USDT" and base and not _is_leveraged_base(base):
                        sym = _mk(base, quote)
                        if sym not in _EXCLUDE_SYMBOLS and base not in _EXCLUDE_BASES:
                            bases.append(base)

        # dedupe while preserving order
        seen = set()
        uniq = [b for b in bases if not (b in seen or seen.add(b))]
        return [_mk(b) for b in uniq]
    except Exception:
        return []

# ============== API العامة التي يستخدمها البوت ==============

def _prepare_symbols() -> Tuple[List[str], Dict[str, dict]]:
    """
    • تُرجع (symbols_list, meta)
    • symbols_list: قائمة "BASE/USDT"
    • meta: dict لكل symbol → {"source": "SPOT" أو "SWAP"}
    """
    # 1) إن كانت MANUAL_SYMBOLS محددة، استخدمها مباشرة
    if _MANUAL_SYMBOLS_RAW:
        manual = _clean_and_dedupe(_MANUAL_SYMBOLS_RAW)
        manual = manual[:TARGET_SYMBOLS_COUNT] if TARGET_SYMBOLS_COUNT > 0 else manual
        meta = {sym: {"source": INST_TYPE} for sym in manual}
        return manual, meta

    # 2) محاولة تلقائية من OKX (اختياري)
    auto_syms: List[str] = []
    if _AUTO_FETCH_OKX:
        auto_syms = _try_fetch_okx_universe()

    # 3) Fallback إلى القائمة الثابتة الكبيرة عند الحاجة
    if not auto_syms:
        auto_syms = [_mk(b) for b in _FALLBACK_BASES]

    # تنظيف وتطبيق الاستثناءات
    cleaned = _clean_and_dedupe(auto_syms)

    # تقليم للعدد المستهدف
    if TARGET_SYMBOLS_COUNT > 0 and len(cleaned) > TARGET_SYMBOLS_COUNT:
        cleaned = cleaned[:TARGET_SYMBOLS_COUNT]

    meta = {sym: {"source": INST_TYPE} for sym in cleaned}
    return cleaned, meta

def list_symbols(inst_type: str = INST_TYPE,
                 target_count: int = TARGET_SYMBOLS_COUNT,
                 min_24h_usd_vol: float = MIN_24H_USD_VOL) -> List[str]:
    """
    دالة التوافق التي يستدعيها bot.py عند الإقلاع.
    نعيد فقط القائمة (بدون meta). التحديثات الدورية تستخدم _prepare_symbols().
    """
    syms, _meta = _prepare_symbols()
    # مبدئيًا لا نطبّق min_24h_usd_vol لأننا لا نجمع فوْليوم هنا بشكل موثوق،
    # ويقوم bot.py بالتحقق من قابلية الرمز عند التحميل من OKX والتكييف للسواب.
    return syms

# ============== قيم جاهزة عند الاستيراد ==============

SYMBOLS, SYMBOLS_META = _prepare_symbols()

# طباعة تشخيصية مفيدة في اللوج
try:
    first = ", ".join(SYMBOLS[:10])
    print(f"[symbols] ready({INST_TYPE}): {len(SYMBOLS)} | first 10: {first}")
    # إظهار لواحق الرافعة المُستبعدة لمزيد من الشفافية
    print(f"OK, LEVERAGED_SUFFIXES: {sorted(list(LEVERAGED_SUFFIXES))}")
except Exception:
    pass

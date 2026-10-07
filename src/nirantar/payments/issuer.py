"""Which institution a payment depended on — the unit of payment health (P10 payment-degradation front).

An incident is "this issuer, on this rail, is failing", e.g. HDFC net banking or the PhonePe UPI handle. Derived from
what the provider reports, never from customer data: netbanking/e-mandate bank code, the UPI handle's PSP bank, the
wallet, the card issuer. Unknown → None (the payment still counts, just not towards any issuer's health).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# UPI handle → the bank behind the PSP (public NPCI handle list; the most common ones)
UPI_HANDLES: dict[str, str] = {
    "okhdfcbank": "HDFC", "hdfcbank": "HDFC", "payzapp": "HDFC",
    "oksbi": "SBI", "sbi": "SBI",
    "okicici": "ICICI", "icici": "ICICI", "ibl": "ICICI",          # ibl: PhonePe on ICICI
    "okaxis": "AXIS", "axisbank": "AXIS", "axl": "AXIS",             # axl: PhonePe on Axis
    "ybl": "YES", "yesbank": "YES",                                  # ybl: PhonePe on Yes Bank
    "paytm": "PAYTM", "ptyes": "YES", "ptaxis": "AXIS", "pthdfc": "HDFC", "ptsbi": "SBI",
    "apl": "AXIS", "yapl": "YES",                                   # Amazon Pay
    "kotak": "KOTAK", "kbl": "KARNATAKA", "upi": "BHIM", "freecharge": "AXIS", "jupiteraxis": "AXIS",
    "idfcbank": "IDFC", "indus": "INDUSIND", "fbl": "FEDERAL", "pnb": "PNB", "barodampay": "BOB",
}


def issuer_of(p: Mapping[str, Any]) -> str | None:
    """Issuer from a Razorpay-shaped payment entity."""
    method = str(p.get("method") or "")
    if method in ("netbanking", "emandate", "nach") and p.get("bank"):
        return str(p["bank"]).upper()
    if method == "upi":
        vpa = str(p.get("vpa") or (p.get("upi") or {}).get("vpa") or "")
        handle = vpa.rsplit("@", 1)[1].lower() if "@" in vpa else ""
        if handle:
            return UPI_HANDLES.get(handle) or f"UPI:{handle}"
        return str(p["bank"]).upper() if p.get("bank") else None
    if method == "wallet" and p.get("wallet"):
        return f"WALLET:{str(p['wallet']).upper()}"
    if method == "card":
        card = p.get("card") or {}
        issuer = card.get("issuer") if isinstance(card, Mapping) else None
        return f"CARD:{str(issuer).upper()}" if issuer else None
    return str(p["bank"]).upper() if p.get("bank") else None

"""PII minimisation at the lake boundary (DPDP data minimisation; ADR-0012).

Provider payloads contain names, phone numbers, emails, VPAs, addresses and free-text notes. None of that is
needed for analytics or model training, so it never lands in the lake in clear text: values are replaced by a
tenant-scoped keyed hash (`core.crypto.lookup_hash`) — the same function used for `billing.customers.contact_hash`,
so joins still work — and nested address/card/bank objects are replaced wholesale.
"""

from __future__ import annotations

import json
from typing import Any

from nirantar.core.crypto import lookup_hash

PII_KEYS = frozenset({
    "email", "contact", "phone", "name", "customer_name", "vpa", "gstin", "customer_email", "customer_contact",
    "account_number", "beneficiary_name", "payer_name", "upi_id", "description",
})
PII_OBJECTS = frozenset({"shipping_address", "billing_address", "address", "card", "bank_account", "customer",
                         "customer_details"})
KEEP_NOTE_PREFIXES = ("nirantar", "subscription_id", "reference_id")


def _h(value: Any, tenant_id: str) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return "h:" + lookup_hash(raw, tenant_id)[:32]


def redact(payload: Any, tenant_id: str, *, _key: str | None = None) -> Any:
    """Return a copy of `payload` with personal data replaced by keyed hashes. Structure is preserved."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if v is None:
                out[k] = None
            elif k in PII_OBJECTS:
                out[k] = _h(v, tenant_id)
            elif k == "notes" and isinstance(v, dict):
                out[k] = {nk: (nv if nk.startswith(KEEP_NOTE_PREFIXES) else _h(nv, tenant_id))
                          for nk, nv in v.items()}
            elif k in PII_KEYS and not isinstance(v, (dict, list)):
                out[k] = _h(v, tenant_id)
            else:
                out[k] = redact(v, tenant_id, _key=k)
        return out
    if isinstance(payload, list):
        return [redact(x, tenant_id, _key=_key) for x in payload]
    return payload

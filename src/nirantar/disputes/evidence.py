"""Dispute evidence service (domain layer): builds and stores the Unified Evidence objects for a dispute
from system-of-record facts. Agents call this through MCP tools; they never query the database themselves."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.core.canonical import sha256_hex
from nirantar.core.ids import new_id
from nirantar.payments.domain import PaymentProvider, ProviderError


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    supports_merchant: bool
    facts: dict[str, Any]
    verified_against: str


def build_evidence(conn: Connection, tenant_id: str, dispute_id: str, provider: PaymentProvider) -> list[EvidenceItem]:
    d = conn.execute(text("SELECT * FROM billing.disputes WHERE tenant_id=:t AND dispute_id=:d"),
                     {"t": tenant_id, "d": dispute_id}).one()
    items: list[EvidenceItem] = []
    debit = conn.execute(text("SELECT d.*, s.mandate_id FROM billing.debits d JOIN billing.subscriptions s ON "
                              "s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id "
                              "WHERE d.tenant_id=:t AND d.debit_id=:id"), {"t": tenant_id, "id": d.debit_id}
                         ).one_or_none() if d.debit_id else None
    if debit is None:   # disputed payment not created by a Nirantar debit: only provider evidence is available
        items.append(EvidenceItem("mandate_record", False, {"note": "payment not linked to a Nirantar debit"},
                                  "system_of_record"))
    else:
        mandate = conn.execute(text("SELECT status, rail, created_at, valid_until FROM billing.mandates WHERE "
                                    "tenant_id=:t AND mandate_id=:m"), {"t": tenant_id, "m": debit.mandate_id}
                               ).one_or_none()
        items.append(EvidenceItem("mandate_record", mandate is not None and mandate.status in ("active", "paused"),
                                  {"mandate_id": debit.mandate_id, "rail": mandate.rail if mandate else None,
                                   "status": mandate.status if mandate else "missing",
                                   "registered_at": mandate.created_at.isoformat() if mandate else None},
                                  "system_of_record"))
        notice_at = debit.predebit_notified_at
        # Conservative: treat the debit as executing at 00:00 on its date, so the lead time is a lower bound.
        debit_at = (datetime.combine(debit.scheduled_for, datetime.min.time(), tzinfo=notice_at.tzinfo)
                    if notice_at else None)
        lead_h = (debit_at - notice_at).total_seconds() / 3600 if notice_at and debit_at else None
        items.append(EvidenceItem("predebit_notice", lead_h is not None and lead_h >= 24,
                                  {"sent_at": notice_at.isoformat() if notice_at else None, "lead_hours": lead_h,
                                   "policy_id": "IN-RBI-EMANDATE-PREDEBIT-001"}, "contact_log"))
    try:
        p = provider.fetch_payment(d.provider_payment_id)
        items.append(EvidenceItem("payment_verification", p.amount.minor == d.amount_minor,
                                  {"provider_payment_id": p.provider_payment_id, "status": p.status.value,
                                   "amount_minor": p.amount.minor, "method": p.method}, "provider"))
    except ProviderError as exc:
        items.append(EvidenceItem("payment_verification", False, {"error": str(exc)[:200]}, "provider"))
    if debit is None:
        return items
    raw_cons: Any = conn.execute(
        text("SELECT consents FROM billing.customers WHERE tenant_id=:t AND customer_id=:c"),
        {"t": tenant_id, "c": debit.customer_id}).scalar_one()
    cons: dict[str, Any] = raw_cons if isinstance(raw_cons, dict) else json.loads(raw_cons or "{}")
    items.append(EvidenceItem("no_prior_opt_out", "debit" not in cons.get("opted_out", []),
                              {"opted_out": cons.get("opted_out", [])}, "system_of_record"))
    return items


def store_evidence(conn: Connection, tenant_id: str, case_id: str, customer_id: str, items: list[EvidenceItem],
                   now: datetime) -> list[str]:
    ids = []
    for it in items:
        eid = new_id("evd")
        conn.execute(text("INSERT INTO ai.evidence (tenant_id, evidence_id, case_id, customer_id, modality, "
                          "source_uri, extracted_fields, language, confidence, verified_against, hash, created_at) "
                          "VALUES "
                          "(:t, :e, :c, :cu, 'document', :s, CAST(:f AS jsonb), 'en', 1.0, :v, :h, :n)"),
                     {"t": tenant_id, "e": eid, "c": case_id, "cu": customer_id, "s": f"nirantar://evidence/{it.kind}",
                      "f": json.dumps({"kind": it.kind, "supports_merchant": it.supports_merchant, **it.facts}),
                      "v": it.verified_against, "h": sha256_hex(it.facts), "n": now})
        ids.append(eid)
    return ids

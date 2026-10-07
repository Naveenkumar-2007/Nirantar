"""The recovery queue: every rupee at risk right now, why it is stuck, and the next best action.

Three sources, all from the system of record:
  failed_debit   a debit the provider declined and nobody has collected yet
  at_risk_debit  an upcoming debit whose M1 decision score says it will probably fail
  mandate        an open mandate-repair case (the mandate will break the next debit)

Expected recoverable ₹ = amount × probability. For failed debits the probability is the business's OWN historical
recovery rate for that decline code, shrunk towards its overall rate (empirical Bayes, so one lucky recovery does
not make a code look certain). With no history yet the probability is None and the queue says so — never a guess.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

SHRINK = 10                      # pseudo-observations of the overall rate added to each decline code
STALLED_AFTER = timedelta(hours=72)
SETTLED_AFTER = timedelta(days=14)        # a failure this old with no payment counts as lost
AT_RISK_HORIZON_DAYS = 7


def _recovery_rates(conn: Connection, now: datetime) -> tuple[float | None, dict[str, float], int]:
    """History = failures whose outcome is known: recovered (verified, with money), or failed more than SETTLED_AFTER
    ago by the PROVIDER's clock (ingest time can be much later, e.g. a history import). Today's open failures are
    not losses yet; counting them would drag every rate towards zero."""
    rows = conn.execute(text(
        "WITH f AS (SELECT d.debit_id, coalesce(d.last_error_code, 'UNKNOWN') AS code, "
        "  EXISTS (SELECT 1 FROM experiments.outcomes o WHERE o.tenant_id=d.tenant_id AND o.debit_id=d.debit_id "
        "    AND o.verified AND o.outcome='recovered' AND o.value_minor > 0) AS won, "
        "  (SELECT min(coalesce(p.provider_created_at, p.created_at)) FROM billing.payments p "
        "    WHERE p.tenant_id=d.tenant_id AND p.debit_id=d.debit_id "
        "    AND p.status='failed') AS failed_at FROM billing.debits d) "
        "SELECT code, count(*) AS n, count(*) FILTER (WHERE won) AS won FROM f "
        "WHERE failed_at IS NOT NULL AND (won OR failed_at < :settled) GROUP BY code"),
        {"settled": now - SETTLED_AFTER}).all()
    n = sum(r.n for r in rows)
    if n == 0:
        return None, {}, 0
    overall = sum(r.won for r in rows) / n
    return overall, {r.code: (r.won + SHRINK * overall) / (r.n + SHRINK) for r in rows}, n


def _in_running_batch(conn: Connection) -> dict[str, str]:
    return {r.subject_id: r.batch_id for r in conn.execute(text(
        "SELECT i.subject_id, i.batch_id FROM ops.recovery_batch_items i JOIN ops.recovery_batches b ON "
        "b.tenant_id=i.tenant_id AND b.batch_id=i.batch_id WHERE b.status IN ('running','stopping','measuring')"))}


def build(conn: Connection, now: datetime, *, min_risk: float = 0.3) -> dict[str, Any]:
    overall, by_code, history_n = _recovery_rates(conn, now)
    busy = _in_running_batch(conn)
    items: list[dict[str, Any]] = []

    # ---- failed debits
    failed = conn.execute(text(
        "SELECT d.debit_id, d.customer_id, d.amount_minor, d.scheduled_for, d.attempt_count, d.last_error_code, "
        "c.display_name, c.preferred_language, k.case_id, k.status AS case_status "
        "FROM billing.debits d JOIN billing.customers c ON c.tenant_id=d.tenant_id AND c.customer_id=d.customer_id "
        "LEFT JOIN LATERAL (SELECT case_id, status FROM ops.cases k WHERE k.tenant_id=d.tenant_id AND "
        "k.subject_id=d.debit_id AND k.kind='debit_cycle' ORDER BY opened_at DESC LIMIT 1) k ON true "
        "WHERE d.status='failed' ORDER BY d.amount_minor DESC LIMIT 1000")).all()
    case_ids = [r.case_id for r in failed if r.case_id]
    acts: dict[str, list[Any]] = defaultdict(list)
    if case_ids:
        for a in conn.execute(text(
                "SELECT x.case_id, x.tool_name, x.status, x.created_at, x.result, ap.status AS approval_status "
                "FROM ops.actions x LEFT JOIN ops.approvals ap ON ap.tenant_id=x.tenant_id AND "
                "ap.approval_id=x.approval_id WHERE x.case_id = ANY(:c) ORDER BY x.created_at"), {"c": case_ids}):
            acts[a.case_id].append(a)
    promises = {r.debit_id: r.promised_date.isoformat() for r in conn.execute(text(
        "SELECT debit_id, promised_date FROM ops.promises WHERE status IN ('open','broken') AND promised_date IS "
        "NOT NULL ORDER BY created_at"))}                                  # the latest promise per debit wins
    for r in failed:
        code = r.last_error_code or "UNKNOWN"
        p = by_code.get(code, overall)
        history = acts.get(r.case_id or "", [])
        contacts = [a for a in history if a.status in ("executed", "verified") and a.tool_name.startswith("comms.")]
        last_contact = contacts[-1].created_at if contacts else None
        pending = any(a.status == "pending_approval" and a.approval_status == "pending" for a in history)
        denied = [a for a in history if a.status == "denied"]
        promise = promises.get(r.debit_id)
        if r.debit_id in busy:
            state, why = "in_batch", f"in recovery batch {busy[r.debit_id]}"
        elif pending:
            state, why = "needs_approval", "an agent action is waiting for a person's approval"
        elif promise and promise[:10] < now.date().isoformat():
            state, why = "promise_broken", f"customer promised to pay by {promise[:10]}; no payment yet"
        elif promise:
            state, why = "promised", f"customer promised to pay by {promise[:10]}"
        elif last_contact is not None and now - last_contact < STALLED_AFTER:
            state, why = "agent_working", "the recovery agent contacted the customer recently"
        elif denied and not contacts:
            state, why = "blocked", "every contact so far was refused by policy (consent, window or fatigue)"
        else:
            state, why = "stalled", ("no contact yet" if last_contact is None else
                                     f"no contact for {(now - last_contact).days} days")
        items.append({
            "item_id": f"failed_debit:{r.debit_id}", "kind": "failed_debit", "subject_id": r.debit_id,
            "customer_id": r.customer_id, "display_name": r.display_name, "language": r.preferred_language,
            "amount_minor": int(r.amount_minor), "due": r.scheduled_for.isoformat(), "reason": code,
            "attempts": int(r.attempt_count), "contacts": len(contacts),
            "last_contact_at": last_contact.isoformat() if last_contact else None,
            "probability": p, "probability_source": (f"your history: {code}" if code in by_code else
                                                     "your overall recovery rate" if p is not None else None),
            "expected_minor": round(int(r.amount_minor) * p) if p is not None else None,
            "state": state, "why": why, "case_id": r.case_id,
            "action": None if state == "in_batch" else "payment_link_whatsapp"})

    # ---- upcoming debits the model expects to fail
    horizon = now.date() + timedelta(days=AT_RISK_HORIZON_DAYS)
    for r in conn.execute(text(
            "SELECT d.debit_id, d.customer_id, d.amount_minor, d.scheduled_for, d.status, d.predebit_notified_at, "
            "c.display_name, c.preferred_language, p.score, p.model_version FROM billing.debits d "
            "JOIN billing.customers c ON c.tenant_id=d.tenant_id AND c.customer_id=d.customer_id "
            "JOIN LATERAL (SELECT score, model_version FROM ai.predictions p WHERE p.tenant_id=d.tenant_id AND "
            "p.subject_id=d.debit_id AND p.model_name='m1_debit_failure' AND p.output->>'role'='decision' "
            "ORDER BY predicted_at DESC LIMIT 1) p ON true "
            "WHERE d.status IN ('scheduled','notified') AND d.scheduled_for BETWEEN :today AND :h AND p.score >= :m "
            "ORDER BY p.score * d.amount_minor DESC LIMIT 500"),
            {"today": now.date(), "h": horizon, "m": min_risk}):
        notice = r.predebit_notified_at is None and r.status == "scheduled"
        items.append({
            "item_id": f"at_risk_debit:{r.debit_id}", "kind": "at_risk_debit", "subject_id": r.debit_id,
            "customer_id": r.customer_id, "display_name": r.display_name, "language": r.preferred_language,
            "amount_minor": int(r.amount_minor), "due": r.scheduled_for.isoformat(), "reason": None,
            "probability": float(r.score), "probability_source": f"M1 {r.model_version}",
            "expected_minor": round(int(r.amount_minor) * float(r.score)),
            "state": "in_batch" if r.debit_id in busy else "at_risk",
            "why": f"{float(r.score):.0%} predicted chance this debit fails",
            "action": None if r.debit_id in busy or not notice else "predebit_notice"})

    # ---- mandates that will break the next debit
    for r in conn.execute(text(
            "SELECT k.case_id, k.subject_id, k.customer_id, k.summary, k.opened_at, c.display_name, "
            "c.preferred_language FROM ops.cases k JOIN billing.customers c ON c.tenant_id=k.tenant_id AND "
            "c.customer_id=k.customer_id WHERE k.kind='mandate' AND k.status <> 'closed' LIMIT 500")):
        s = r.summary if isinstance(r.summary, dict) else {}
        items.append({
            "item_id": f"mandate:{r.case_id}", "kind": "mandate", "subject_id": r.case_id,
            "customer_id": r.customer_id, "display_name": r.display_name, "language": r.preferred_language,
            "amount_minor": int(s.get("next_amount_minor") or 0), "due": s.get("next_debit_on"),
            "reason": s.get("repair"), "probability": None, "probability_source": None,
            "expected_minor": None, "state": "in_batch" if r.case_id in busy else "mandate_problem",
            "why": s.get("rationale") or "the mandate will not cover the next debit", "case_id": r.case_id,
            "action": None if r.case_id in busy else "mandate_repair"})

    items.sort(key=lambda x: (x["expected_minor"] if x["expected_minor"] is not None else x["amount_minor"]),
               reverse=True)
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "amount_minor": 0, "expected_minor": 0})
    for x in items:
        k = by_kind[x["kind"]]
        k["n"] += 1
        k["amount_minor"] += x["amount_minor"]
        k["expected_minor"] += x["expected_minor"] or 0
    return {"generated_at": now.isoformat(), "items": items, "totals": dict(by_kind),
            "history": {"failed_debits": history_n, "overall_recovery_rate": overall}}

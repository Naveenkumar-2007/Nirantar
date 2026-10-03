"""Seed a demo tenant by running the REAL pipeline for a month of debits.

Every row the dashboard shows is produced by the same code paths as production: billing service → M1
prediction → pre-debit notice via MCP → mock provider debit → signed webhook → ingress → processing →
Conductor (triage, compliance, holdout, arbiter, MCP actions) → payment link paid → webhook → Verifier →
ledger → outcome/labels/experiment. Customer behaviour (who fails, who pays the link) is simulated and the
whole tenant is labelled demo. Run: uv run python -m nirantar.demo.seed --customers 120
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from nirantar.agents.conductor import ConductorDeps, FailedDebit, handle_failure
from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.comms.sink import MockCommsSink
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import get_engine, tenant_tx
from nirantar.experiments import service as experiments
from nirantar.features.online import OnlineStore
from nirantar.llm.gateway import LLMGateway
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.ml.router import ModelRouter
from nirantar.outcomes.service import close_cycle
from nirantar.payments.domain import LinkRequest
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.security.keys import create_principal, issue_api_key
from nirantar.settings import learning
from nirantar.settings import runtime as tenant_runtime

NAMES = ["Priya Sharma", "Ravi Kumar", "Ananya Reddy", "Arjun Rao", "Meera Iyer", "Kiran Naidu", "Sneha Patel",
         "Vikram Singh", "Lakshmi Devi", "Rahul Verma", "Divya Menon", "Suresh Babu", "Pooja Gupta", "Aditya Joshi"]
LANGS = ["te", "te", "hi", "hi", "en", "en", "en"]
PLANS = [("Chai Club Monthly", "499"), ("Chai Club Plus", "999"), ("Chai Club Family", "1499")]


def _ist(d: date, hour: int, minute: int = 0) -> datetime:
    """A time of day in IST, as UTC (IST = UTC+05:30)."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def seed(engine: Engine, n_customers: int = 120, seed_value: int = 11, llm: LLMGateway | None = None,
         today: date | None = None) -> dict[str, Any]:
    rng = random.Random(seed_value)
    today = today or datetime.now(UTC).date()
    tenant = new_id("ten")
    mock, sink = MockProvider(webhook_secret="whsec_demo"), MockCommsSink()
    with tenant_tx(tenant, engine) as c:
        create_tenant(c, tenant, "Chai Club (demo)", {"segments": ["subscription"], "demo": True})
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_demo")
        # holdout size comes from the tenant's experiments settings (platform default 8%)
        holdout = experiments.default_holdout_bp(c, tenant)
        exp = experiments.create_experiment(c, tenant, "recovery-holdout-v1", {"treatment": 10_000 - holdout},
                                            holdout_bp=holdout)
    keys = {}
    for role in ("owner", "finance_approver", "viewer"):
        pid = create_principal(engine, tenant, f"demo-{role}", "user", [role])
        keys[role] = issue_api_key(engine, tenant, pid).plaintext
    store: OnlineStore | None = OnlineStore()
    try:
        store.r.ping()  # type: ignore[union-attr]
    except Exception:  # Redis not running → the seeder still runs; predictions are simply absent
        store = None
    router = ModelRouter()

    clock = FixedClock(_ist(today - timedelta(days=35), 10))
    gw = ToolGateway(engine, TOOLS, AGENT_SCOPES,
                     {"engine": engine, "provider": mock, "comms": sink, "experiment_id": exp}, clock=clock)
    with tenant_tx(tenant, engine) as c:
        rt = tenant_runtime.load(c, tenant)          # channels, effects, costs, policy from tenant settings

    def deps() -> ConductorDeps:
        return ConductorDeps(gw, llm, rt.capacity, rt.priors, config_sources=rt.sources)

    stats = {"debits": 0, "paid_on_time": 0, "failed": 0, "recovered": 0, "unrecovered": 0, "night_denials": 0}
    for i in range(n_customers):
        name = NAMES[i % len(NAMES)]
        plan_name, price = PLANS[rng.randrange(len(PLANS))]
        debit_day = today - timedelta(days=rng.randint(3, 30))
        created = _ist(debit_day - timedelta(days=4), 10)
        clock._at = created
        stress = rng.random()
        with tenant_tx(tenant, engine) as c:
            cust = create_customer(c, tenant, NewCustomer(
                f"chai-{i:04d}", name, f"+9198{rng.randint(10_000_000, 99_999_999)}", None, LANGS[i % len(LANGS)],
                consents={"whatsapp": rng.random() > 0.08, "sms": True}))
            psub = mock.add_subscription(cust, Money.of(price))
            sub = create_subscription(c, tenant, cust, "mock", psub, Money.of(price))
            debit = schedule_debit(c, tenant, sub, debit_day, created)
            case_id = f"cas_{debit.split('_', 1)[1]}"
            c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, "
                           "opened_at) VALUES (:t, :c, 'debit_cycle', :d, :cu, 'open', :n)"),
                      {"t": tenant, "c": case_id, "d": debit, "cu": cust, "n": created})
            pred_id: str | None = None
            if store is not None:   # the same feature store + router as production (cold start for a new tenant)
                due = datetime.combine(debit_day, datetime.min.time(), UTC)
                of = store.features_for(tenant, sub, as_of=created, due_at=due, amount_minor=Money.of(price).minor,
                                        method=None, bank=None)
                pred_id = router.score(c, tenant, subject_id=debit, entity_id=sub, features=of.row, now=created,
                                       context={"cold_start": of.cold_start}).prediction_id
        clock._at = _ist(debit_day - timedelta(days=2), 9, 30)
        gw.call(tenant_id=tenant, agent_id="debit_strategist", tool_name="comms.send_predebit_notice",
                args={"debit_id": debit}, case_id=case_id)
        stats["debits"] += 1
        # ---- provider executes the debit (simulated customer balance)
        fails = rng.random() < (0.10 + 0.25 * stress)
        at = _ist(debit_day, 2, 15)
        pay = mock.charge(psub, succeed=not fails, error_code=None if not fails else "BAD_REQUEST_ERROR", at=at)
        headers, body = mock.webhook_for("payment.captured" if not fails else "payment.failed", "payment",
                                         MockProvider.payment_entity(pay), at=at)
        res = ingest_webhook(engine, mock, tenant, headers, body, FixedClock(at))
        process_raw_event(engine, mock, tenant, str(res.raw_event_id), FixedClock(at))
        if not fails:
            with tenant_tx(tenant, engine) as c:
                close_cycle(c, tenant, debit, cust, "paid_on_time", True, 0, pred_id, exp, case_id, at)
            stats["paid_on_time"] += 1
            continue
        stats["failed"] += 1
        # ---- Conductor round 1 happens at the failure time (night in IST) → deferred by policy
        clock._at = at
        night = handle_failure(deps(), tenant, case_id,
                               FailedDebit(debit, cust, Money.of(price).minor, "INR", "BAD_REQUEST_ERROR",
                                           "insufficient_funds", attempt=1, plan_name=plan_name))
        if night.get("retry_after"):
            stats["night_denials"] += 1
        # ---- retry at 10:30 IST, as the workflow would
        clock._at = _ist(debit_day, 10, 30)
        state = handle_failure(deps(), tenant, case_id,
                               FailedDebit(debit, cust, Money.of(price).minor, "INR", "BAD_REQUEST_ERROR",
                                           "insufficient_funds", attempt=1, plan_name=plan_name))
        contacted = state.get("chosen_arm") == "whatsapp"
        pays = rng.random() < ((0.55 if contacted else 0.38) - 0.2 * stress)
        close_at = _ist(debit_day + timedelta(days=rng.randint(1, 5)), 19)
        if pays:
            link = next((lk for lk in mock.links.values() if lk.reference_id == f"{debit}.1"), None)
            if link is None:
                link = mock.create_payment_link(LinkRequest(Money.of(price), f"{debit}.1", plan_name, None, None,
                                                            None))
            paid = mock.pay_link(link.link_id, at=close_at)
            headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(paid),
                                             at=close_at)
            r2 = ingest_webhook(engine, mock, tenant, headers, body, FixedClock(close_at))
            process_raw_event(engine, mock, tenant, str(r2.raw_event_id), FixedClock(close_at))
        with tenant_tx(tenant, engine) as c:
            status: str = c.execute(text("SELECT status FROM billing.debits WHERE debit_id=:d"),
                                    {"d": debit}).scalar_one()
            outcome = "recovered" if status == "succeeded" else "unrecovered"
            close_cycle(c, tenant, debit, cust, outcome, True, Money.of(price).minor if outcome == "recovered" else 0,
                        pred_id, exp, case_id, close_at)
        stats[outcome] += 1
    # a treasury request awaiting maker-checker, so the approval inbox has real work
    clock._at = datetime.now(UTC)
    gw.call(tenant_id=tenant, agent_id="treasury_agent", tool_name="treasury.request_credit_draw",
            args={"shortfall_day": (today + timedelta(days=6)).isoformat(),
                  "reason": "Projected balance ₹-35,000 on payout day (pessimistic M10 band); draw ₹60,000"},
            case_id="cas_treasury_demo")
    # learn this tenant's contact effects + risk threshold from the outcomes just verified (stored with evidence)
    with tenant_tx(tenant, engine) as c:
        learned = learning.refresh(c, tenant, datetime.now(UTC))
    return {"tenant_id": tenant, "experiment_id": exp, "api_keys": keys, "stats": stats,
            "learned": {k: {"version": v["version"], "value": v["value"]} for k, v in learned.items()},
            "messages_sent": len(sink.messages)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=120)
    ap.add_argument("--live-llm", action="store_true", help="draft messages with Groq/Sarvam (costs credits)")
    args = ap.parse_args()
    out = seed(get_engine(), args.customers, llm=LLMGateway.from_env() if args.live_llm else None)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

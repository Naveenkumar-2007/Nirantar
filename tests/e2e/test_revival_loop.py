"""Churn → win-back loop, end to end on real services (Postgres, Iceberg/SeaweedFS, Temporal time-skipping).

History in the provider → lifecycle labels (voluntary / involuntary churn) → sBG fit → candidate selection (consent
checked BEFORE randomisation) → stratified arms + holdout → RevivalWorkflows: offers with code-computed amounts,
maker-checker for large discounts, promotional message via the Compliance Guardian → customers pay the link →
provider-verified reactivation, ledger income, experiment outcomes → incrementality vs the holdout.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.approvals.service import decide
from nirantar.billing.service import NewCustomer, connect_provider, create_customer, create_subscription, create_tenant
from nirantar.comms.sink import MockCommsSink
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.data.pipeline import run_tenant
from nirantar.data.sources import MockSource
from nirantar.db.session import tenant_tx
from nirantar.experiments import service as experiments
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS, amounts_in_text
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.retention.candidates import select_winback
from nirantar.retention.fit import fit_retention
from nirantar.security.rbac import Principal
from nirantar.settings import service as settings
from nirantar.workflows.revival import RevivalInput, RevivalWorkflow, revival_workflow_id
from nirantar.workflows.revival_activities import RevivalActivities, RevivalDeps
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
QUEUE = "revival-test"


def _build_history(engine: Engine, tenant: str, mock: MockProvider, now: datetime) -> dict[str, str]:
    """90 subscribers at ₹499: a third active, a third left after paying (voluntary), a third after an unpaid
    failure (involuntary). Every 10th subscriber has no promotional consent."""
    kinds: dict[str, str] = {}
    with tenant_tx(tenant, engine) as c:
        create_tenant(c, tenant, "Revival merchant (synthetic)", {"segments": ["subscription"], "synthetic": True})
        connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_rv")
        for i in range(90):
            cust = create_customer(c, tenant, NewCustomer(f"rv{i}", f"Cust {i}", f"+9190000{i:05d}", None,
                                                          ("en", "hi", "te")[i % 3],
                                                          consents={"whatsapp": True, "sms": True,
                                                                    "promotional": i % 10 != 0}))
            psub = mock.add_subscription(cust, Money.of("499"))
            create_subscription(c, tenant, cust, "mock", psub, Money.of("499"))
            kind = ("active", "voluntary", "involuntary")[i % 3]
            kinds[cust] = kind
            last = {"active": 10, "voluntary": 62, "involuntary": 58}[kind]   # > 52 days silent = churned
            for k in range(6):
                at = now - timedelta(days=last + 30 * (5 - k))
                mock.charge(psub, succeed=not (kind == "involuntary" and k == 5), at=at)
    return kinds


def _pay(engine: Engine, mock: MockProvider, tenant: str, link_id: str, at: datetime) -> None:
    paid = mock.pay_link(link_id, at=at)
    headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(paid), at=at)
    res = ingest_webhook(engine, mock, tenant, headers, body, FixedClock(at))
    process_raw_event(engine, mock, tenant, str(res.raw_event_id), FixedClock(at))


async def test_winback_loop_end_to_end(app_engine: Engine, lake: Any) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    tenant, mock, sink = new_id("ten"), MockProvider(webhook_secret="whsec_rv"), MockCommsSink()
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        now = (await env.get_current_time()).astimezone(UTC)
        kinds = _build_history(app_engine, tenant, mock, now)
        run_tenant(app_engine, lake, tenant, now=now, trigger="test", sources=[MockSource(mock)],
                   backfill_since=now - timedelta(days=400))
        with tenant_tx(tenant, app_engine) as c:     # ₹125 discounts need a human; ₹50 ones don't
            settings.update(c, tenant, "policy", {"discount_approval_above_minor": 10_000}, actor="user:owner",
                            reason="discounts above ₹100 need approval", now=now, expected_version=0)

        # ---- lifecycle → fit → candidates
        fit = fit_retention(app_engine, lake, tenant, now=now)
        assert fit["churned"] == 60 and "sbg" in fit and fit["sbg"]["fit_check"]["periods_checked"] > 0
        sel = select_winback(app_engine, lake, tenant, now=now)
        cases = sel["created"]
        assert sel["excluded"] == {"no WhatsApp + promotional consent": 6}            # before randomisation
        assert len(cases) == 54
        by_stratum: dict[str, set[str]] = {}
        for x in cases:
            by_stratum.setdefault(x["stratum"], set()).add(x["arm"])
            assert kinds[x["customer_id"]] == x["stratum"]
        assert by_stratum["involuntary"] <= {"reminder", "holdout"}                  # no discounts for failed mandates
        assert by_stratum["voluntary"] <= {"reminder", "pct10", "pct25", "holdout"}
        assert "holdout" in by_stratum["voluntary"] | by_stratum["involuntary"]

        acts = RevivalActivities(RevivalDeps(app_engine, mock, sink))
        async with Worker(env.client, task_queue=QUEUE, workflows=[RevivalWorkflow], activities=acts.all(),
                          activity_executor=ThreadPoolExecutor(8)):
            handles = [await env.client.start_workflow(
                RevivalWorkflow.run, RevivalInput(tenant, x["case_id"], 10),   # short window keeps the test fast
                id=revival_workflow_id(tenant, x["case_id"]), task_queue=QUEUE) for x in cases]

            async def stages() -> list[str]:
                return [(await h.query(RevivalWorkflow.status))["stage"] for h in handles]

            # ---- maker-checker: approve every pending large discount (as the Approvals inbox would)
            for _ in range(60):
                st = await stages()
                if all(s in ("waiting", "awaiting_approval", "closed") for s in st):
                    break
                await asyncio.sleep(0.2)
            with tenant_tx(tenant, app_engine) as c:
                pending = c.execute(text("SELECT a.approval_id, x.agent_id, x.tool_name, x.params, x.case_id, "
                                         "x.idempotency_key FROM ops.approvals a JOIN ops.actions x ON "
                                         "x.tenant_id=a.tenant_id AND x.action_id=a.action_id WHERE a.tenant_id=:t "
                                         "AND a.status='pending'"), {"t": tenant}).all()
            pct25 = [x for x in cases if x["arm"] == "pct25"]
            assert {p.case_id for p in pending} == {x["case_id"] for x in pct25}      # only the ₹125 discounts
            approver = Principal(tenant, "prn_approver", "user", ("finance_approver",))
            for a in pending:
                with tenant_tx(tenant, app_engine) as c:
                    token = decide(c, approver, a.approval_id, True, now)
                gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": mock,
                                                                   "comms": sink}, clock=FixedClock(now))
                res = gw.call(tenant_id=tenant, agent_id=a.agent_id, tool_name=a.tool_name, args=dict(a.params),
                              case_id=a.case_id, approval_token=token, idempotency_key=a.idempotency_key)
                assert res.status == "executed"

            # ---- let approvals be picked up and contact windows open
            for _ in range(48):
                if all(s in ("waiting", "closed") for s in await stages()):
                    break
                await env.sleep(timedelta(hours=1))
            assert all(s == "waiting" for s in await stages())

            # ---- customers respond: every other treated customer pays their link; one holdout customer returns
            with tenant_tx(tenant, app_engine) as c:
                offers = c.execute(text("SELECT case_id, customer_id, link_ref, arm, offer_amount_minor, "
                                        "list_amount_minor, status FROM billing.offers WHERE tenant_id=:t"),
                                   {"t": tenant}).all()
            assert {o.status for o in offers} == {"sent"}
            links = {lk.url: lk.link_id for lk in mock.links.values()}
            payers = {o.case_id for o in offers[::2]}
            at = (await env.get_current_time()).astimezone(UTC)
            for o in offers:
                if o.case_id in payers:
                    _pay(app_engine, mock, tenant, links[o.link_ref], at)
            holdouts = [x for x in cases if x["arm"] == "holdout"]
            back = holdouts[0]["customer_id"]
            psub = next(s.sub_id for s in mock.subs.values() if s.customer_ref == back)
            p = mock.charge(psub, succeed=True, at=at)
            headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(p), at=at)
            process_raw_event(app_engine, mock, tenant, str(ingest_webhook(app_engine, mock, tenant, headers, body,
                                                                           FixedClock(at)).raw_event_id),
                              FixedClock(at))

            await env.sleep(timedelta(days=11))
            results = [await h.result() for h in handles]
            await capture(handles[0], "RevivalWorkflow")

        # ---- outcomes
        by_case = {x["case_id"]: x for x in cases}
        outcomes = {x["case_id"]: r["outcome"] for x, r in zip(cases, results, strict=True)}
        for case_id, outcome in outcomes.items():
            expected = case_id in payers or by_case[case_id]["customer_id"] == back
            assert outcome == ("reactivated" if expected else "not_reactivated"), case_id
        with tenant_tx(tenant, app_engine) as c:
            redeemed = c.execute(text("SELECT offer_id, offer_amount_minor, redeemed_payment_id FROM billing.offers "
                                      "WHERE tenant_id=:t AND status='redeemed'"), {"t": tenant}).all()
            income = c.execute(text("SELECT coalesce(sum(l.amount_minor), 0) FROM ledger.lines l WHERE l.side='C' AND "
                                    "l.tenant_id=:t AND l.account_code='income:recurring' AND l.entry_id IN "
                                    "(SELECT entry_id FROM ledger.entries WHERE tenant_id=:t AND memo LIKE "
                                    "'verified reactivation for %')"), {"t": tenant}).scalar_one()
            holdout_msgs = [m for m in sink.messages if m.to_ref in {x["customer_id"] for x in holdouts}]
            exps = {x["experiment_id"] for x in cases}
            analyses = [experiments.analyze(c, tenant, e, min_per_arm=1, success_outcome="reactivated")
                        for e in exps]
        assert len(redeemed) == len(payers) and income == sum(r.offer_amount_minor for r in redeemed)
        assert {o.offer_amount_minor for o in offers if o.arm == "pct25"} <= {37400}   # ₹499 − 25% by code
        assert not holdout_msgs                                                       # the holdout is untouched
        assert all(not amounts_in_text(m.text) for m in sink.messages)               # no ₹ figures in promos
        assert all("STOP" in m.text for m in sink.messages)                           # promotional opt-out line
        arms = {a for an in analyses for a in an["arms"]}
        assert "holdout" in arms and sum(an["arms"].get("holdout", {}).get("n", 0) for an in analyses) == len(holdouts)
        with tenant_tx(tenant, app_engine) as c:
            closed = c.execute(text("SELECT count(*) FROM ops.cases WHERE tenant_id=:t AND kind='revival' AND "
                                    "status='closed'"), {"t": tenant}).scalar_one()
        assert closed == len(cases)
        print(json.dumps({"cases": len(cases), "reactivated": sum(o == "reactivated" for o in outcomes.values()),
                          "analyses": analyses}, default=str)[:1500])
    finally:
        await env.shutdown()

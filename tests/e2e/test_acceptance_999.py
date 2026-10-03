"""BB-§53 final acceptance scenario, end to end, with a trace artifact.

Merchant → customer → ₹999 subscription → debit scheduled → M1 predicts risk → pre-debit notice →
Temporal sleeps to the debit → provider debit FAILS (signed webhook) → ingress → processing →
payment.failed event → workflow signal → Conductor: triage → compliance eligibility → holdout
assignment → Contact Arbiter → MCP: payment link + WhatsApp (LLM draft, amount-checked) → exposure →
Temporal waits (contact windows / replies) → customer reply → customer pays → webhook → Verifier →
ledger settled → workflow verifies via MCP → outcome + labels + experiment outcome → live evaluation.

Set NIRANTAR_E2E_LIVE_LLM=1 to use real Groq/Sarvam instead of the deterministic stub.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from nirantar.audit.chain import AuditChain
from nirantar.billing.service import (
    NewCustomer,
    connect_provider,
    create_customer,
    create_subscription,
    create_tenant,
    schedule_debit,
)
from nirantar.comms.sink import MockCommsSink
from nirantar.contracts.events import EventEnvelope, make_event
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlAuditStore, SqlLedgerStore
from nirantar.experiments import service as experiments
from nirantar.features.online import OnlineStore
from nirantar.ledger import Ledger
from nirantar.llm.gateway import LLMGateway, StubProvider
from nirantar.ml.router import ModelRouter
from nirantar.payments.ingress import ingest_webhook
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.mock import MockProvider
from nirantar.verifier.payments import RECEIVABLE, clearing_account
from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.activities import DebitActivities, Deps
from nirantar.workflows.bridge import signal_for_event, workflow_id
from nirantar.workflows.debit_cycle import DebitCycleWorkflow
from nirantar.workflows.types import CycleInput
from tests.replay.capture import capture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
TRACE_FILE = Path(__file__).resolve().parents[2] / "evals" / "results" / "acceptance_trace.json"


def _llm() -> LLMGateway:
    if os.environ.get("NIRANTAR_E2E_LIVE_LLM") == "1":
        return LLMGateway.from_env()
    draft = ('{"text":"నమస్తే Priya, Chai Club కోసం ₹999.00 చెల్లింపు పెండింగ్‌లో ఉంది. '
             'ఇక్కడ చెల్లించండి: {link}","language":"te"}')
    return LLMGateway({"sarvam": StubProvider(lambda _m: draft, "sarvam"),
                       "groq": StubProvider(lambda _m: '{"category":"UNKNOWN","rationale":"stub"}', "groq")})


def _latest_event(engine: Engine, tenant: str, event_type: str, subject: str) -> EventEnvelope:
    with tenant_tx(tenant, engine) as c:
        env = c.execute(text("SELECT envelope FROM events.outbox WHERE event_type=:e AND subject_id=:s "
                             "ORDER BY created_at DESC LIMIT 1"), {"e": event_type, "s": subject}).scalar_one()
    return EventEnvelope.model_validate(env if isinstance(env, dict) else json.loads(env))


async def _wait_for(handle: Any, predicate: Any, timeout_s: float = 60) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        st = await handle.query(DebitCycleWorkflow.status)
        if predicate(st):
            return st
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"timed out; workflow status: {st['stage']}")
        await asyncio.sleep(0.2)


async def test_acceptance_999_rupee_subscription(app_engine: Engine) -> None:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    tenant = new_id("ten")
    mock, sink = MockProvider(webhook_secret="whsec_e2e"), MockCommsSink()
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        start = await env.get_current_time()
        debit_at = start + timedelta(days=3)
        # ---------------- merchant, experiment, customer (in the treatment arm), ₹999 subscription, debit
        with tenant_tx(tenant, app_engine) as c:
            create_tenant(c, tenant, "Chai Club (D2C subscriptions)", {"segments": ["subscription"]})
            connect_provider(c, tenant, "mock", "test", "literal:unused", "literal:whsec_e2e")
            exp = experiments.create_experiment(c, tenant, "recovery-2026Q4", {"treatment": 9200}, holdout_bp=800)
            while True:  # holdout is 8%: pick a customer the hash places in treatment (scenario needs an action)
                cust = create_customer(c, tenant, NewCustomer(new_id("ext"), "Priya Sharma", "+919876543210",
                                                              "priya@example.com", "te",
                                                              consents={"whatsapp": True, "sms": True}))
                if experiments._bucket(exp, cust) >= 800:
                    break
            provider_sub = mock.add_subscription(cust, Money.of("999"))
            sub = create_subscription(c, tenant, cust, "mock", provider_sub, Money.of("999"))
            debit = schedule_debit(c, tenant, sub, date.fromisoformat(debit_at.date().isoformat()), start)

        acts = DebitActivities(Deps(app_engine, mock, sink, _llm(), OnlineStore(), ModelRouter()))
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DebitCycleWorkflow], activities=acts.all(),
                          activity_executor=ThreadPoolExecutor(8)):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    DebitCycleWorkflow.run,
                    CycleInput(tenant, debit, cust, exp, debit_at.isoformat()),
                    id=workflow_id(tenant, debit), task_queue=TASK_QUEUE)
                await _wait_for(handle, lambda s: s["stage"] == "waiting_until_debit")
                await env.sleep(timedelta(days=3, minutes=5))                     # T-3 → debit day
                await _wait_for(handle, lambda s: s["stage"] == "awaiting_payment")

                # ---------------- provider executes the mandate debit: FAILS (insufficient funds)
                failed = mock.charge(provider_sub, succeed=False, error_code="BAD_REQUEST_ERROR",
                                     at=await env.get_current_time())
                headers, body = mock.webhook_for("payment.failed", "payment", MockProvider.payment_entity(failed))
                ing = ingest_webhook(app_engine, mock, tenant, headers, body)
                assert ing.status_code == 200
                assert process_raw_event(app_engine, mock, tenant, str(ing.raw_event_id)).event_type == "payment.failed"
                assert await signal_for_event(env.client, _latest_event(app_engine, tenant, "payment.failed", debit))

                st = await _wait_for(handle, lambda s: s["stage"] in ("awaiting_recovery", "waiting_contact_window"))
                for _ in range(30):  # if it's night in IST, the workflow waits for the contact window
                    if st["stage"] == "awaiting_recovery":
                        break
                    await env.sleep(timedelta(hours=1))
                    st = await handle.query(DebitCycleWorkflow.status)
                assert st["stage"] == "awaiting_recovery", st["stage"]

                # ---------------- customer replies, then pays the link
                reply = make_event(event_type="reply.received", version=1, tenant_id=tenant, subject_id=cust,
                                   payload={"debit_id": debit, "text": "I'll pay tonight after salary"},
                                   source="whatsapp", occurred_at=await env.get_current_time())
                assert await signal_for_event(env.client, reply)
                await _wait_for(handle, lambda s: s["handled_replies"] == 1)
                link = next(lk for lk in mock.links.values() if lk.reference_id == f"{debit}.1")
                paid = mock.pay_link(link.link_id, at=await env.get_current_time())
                headers, body = mock.webhook_for("payment.captured", "payment", MockProvider.payment_entity(paid))
                ing2 = ingest_webhook(app_engine, mock, tenant, headers, body)
                out2 = process_raw_event(app_engine, mock, tenant, str(ing2.raw_event_id))
                assert out2.event_type == "payment.captured" and out2.detail == "settled"
                assert await signal_for_event(env.client, _latest_event(app_engine, tenant, "payment.captured", debit))
                result = await handle.result()
                await capture(handle, "DebitCycleWorkflow")
    finally:
        await env.shutdown()

    # ================= assertions: every stage proven from durable evidence =================
    assert result["outcome"] == "recovered" and result["verified"] is True
    stages = {t["activity"]: t for t in result["trace"]}
    risk = stages["predict_risk"]["result"]
    assert risk["p_fail"] is not None                                                   # M1 predicted
    # a brand-new business and customer: no trained model, no history → the transparent prior, flagged cold start
    assert risk["served_by"] == "prior" and risk["cold_start"] is True and risk["model_version"] == "prior-v1"
    assert stages["pre_debit"]["result"]["notice"]["status"] == "executed"            # mandatory notice
    failure = stages["handle_failure"]["result"]
    assert failure["triage"]["category"] == "INSUFFICIENT_FUNDS"
    assert failure["chosen_arm"] == "whatsapp" and failure["exposure_arm"] == "whatsapp"
    sent = next(a for a in failure["actions"] if a["tool"] == "comms.send_whatsapp")
    assert sent["status"] == "executed" and sent["provider_ref"].startswith("wamid.")
    assert ("create_payment_link", f"{debit}.1") in mock.calls                         # provider received action
    assert stages["record_reply"]["result"]["intent"] == "promise_to_pay"
    closed = stages["verify_and_close"]["result"]
    assert closed["evidence"]["verified"] and len(closed["label_ids"]) == 2
    assert closed["live_eval"]["n_labelled"] >= 1                                      # evaluation updated
    assert any("₹999.00" in m.text for m in sink.messages)                             # amount from the debit

    with tenant_tx(tenant, app_engine) as c:
        ledger = Ledger(SqlLedgerStore(c))
        assert ledger.balance(tenant, RECEIVABLE).is_zero                              # accrued then settled
        assert ledger.balance(tenant, clearing_account("mock")) == Money.of("999")
        assert ledger.trial_balance_ok(tenant)
        audit_count = AuditChain(SqlAuditStore(c)).verify(tenant)                     # tamper-evident trail
        events = c.execute(text("SELECT event_type, count(*) FROM events.outbox GROUP BY 1")).all()
        exposure = c.execute(text("SELECT arm FROM experiments.exposures")).scalars().all()
        exp_outcome = c.execute(text("SELECT outcome, verified, value_minor FROM experiments.outcomes")).one()
        actions = c.execute(text("SELECT agent_id, tool_name, status, policy_decision FROM ops.actions "
                                 "ORDER BY created_at")).all()
        case = c.execute(text("SELECT status, summary FROM ops.cases")).one()
        labels = c.execute(text("SELECT label_name, value FROM ai.labels")).all()
    event_types = {e[0] for e in events}
    assert {"subscription.debit_scheduled", "provider.webhook_received", "payment.failed", "payment.captured",
            "experiment.exposure", "outcome.recovered"} <= event_types
    assert exposure == ["whatsapp"] and exp_outcome.verified and exp_outcome.value_minor == 99900
    assert case.status == "closed" and audit_count >= 8

    TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACE_FILE.write_text(json.dumps({
        "scenario": "BB-§53 ₹999 subscription, failed debit recovered via WhatsApp",
        "generated_at": datetime.now(UTC).isoformat(), "tenant_id": tenant, "debit_id": debit,
        "llm_mode": "live" if os.environ.get("NIRANTAR_E2E_LIVE_LLM") == "1" else "stub",
        "workflow_result": result,
        "evidence": {
            "ledger": {"receivable": str(Money(0)), "clearing_mock": "INR 999.00", "trial_balance_ok": True},
            "audit_records_verified": audit_count,
            "outbox_events": {e[0]: e[1] for e in events},
            "experiment": {"exposure": exposure, "outcome": dict(exp_outcome._mapping)},
            "actions": [dict(a._mapping) for a in actions],
            "case": {"status": case.status, "summary": case.summary},
            "labels": [dict(lb._mapping) for lb in labels],
            "messages": [{"channel": m.channel, "text": m.text} for m in sink.messages],
        },
    }, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

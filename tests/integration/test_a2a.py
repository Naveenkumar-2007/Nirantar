from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from nirantar.a2a.protocol import A2AError, Identity, TaskState, check_transition
from nirantar.a2a.service import (
    accept,
    create_task,
    handle_customer_request,
    handoff_request,
    record_agency_attempt,
    revoke,
    transition,
    trust,
)
from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)   # 11:30 IST


@pytest.fixture
def world(app_engine: Engine) -> dict[str, object]:
    t = new_id("ten")
    lender = Identity.create("nirantar.lender-x", "Lender X NBFC", ["collections.agency_handoff"],
                             "https://nirantar.example/a2a/lender-x")
    agency = Identity.create("acme-collections.agent", "ACME Collections Pvt Ltd", ["collections.work_case"],
                             "https://acme.example/a2a")
    customer_agent = Identity.create("priya.personal-assistant", "Priya (consumer)", ["subscription.request"],
                                     "https://assistant.example/priya")
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Lender X", {"segments": ["lending"]})
        trust(c, t, agency.card)
        trust(c, t, customer_agent.card)
    return {"t": t, "lender": lender, "agency": agency, "customer_agent": customer_agent}


def test_lifecycle_rules() -> None:
    check_transition(TaskState.SUBMITTED, TaskState.WORKING)
    check_transition(TaskState.WORKING, TaskState.INPUT_REQUIRED)
    with pytest.raises(A2AError):
        check_transition(TaskState.COMPLETED, TaskState.WORKING)
    with pytest.raises(A2AError):
        check_transition(TaskState.CANCELED, TaskState.COMPLETED)


def test_agency_handoff_flow_with_signed_updates(app_engine: Engine, world: dict[str, object]) -> None:
    t, lender, agency = world["t"], world["lender"], world["agency"]
    assert isinstance(t, str) and isinstance(lender, Identity) and isinstance(agency, Identity)
    with tenant_tx(t, app_engine) as c:
        task = create_task(c, t, "collections.agency_handoff", agency.card.name, "outbound",
                           handoff_request("loan_42", 45, 500_000, ["extend_1_month"], []))
        transition(c, t, task, TaskState.WORKING)
        # agency reports two attempts: one inside RBI hours, one at 21:00 IST (violation)
        for at, party in ((NOW, "borrower"), (datetime(2026, 10, 5, 15, 30, tzinfo=UTC), "borrower"),
                          (NOW + timedelta(minutes=1), "relative")):
            msg = agency.sign(task, lender.card.name, "task.update",
                              {"attempt": {"at": at.isoformat(), "channel": "voice", "contacted_party": party,
                                           "outcome": "ptp"}}, NOW)
            accept(c, t, lender.card.name, msg, NOW)
            res = record_agency_attempt(c, t, task, "cus_borrower", msg.body["attempt"])
            if at.hour == 15:
                assert res["violations"] == ["IN-RBI-RBC-RECOVERY-HOURS-001"]
            if party == "relative":
                assert "IN-RBI-RBC-RECOVERY-CONDUCT-001" in res["violations"]
        final = agency.sign(task, lender.card.name, "task.result", {"status": "ptp_obtained"}, NOW)
        accept(c, t, lender.card.name, final, NOW)
        transition(c, t, task, TaskState.COMPLETED, {"agency_result": "ptp_obtained"})
        logged = c.execute(text("SELECT status FROM ops.contacts ORDER BY at")).scalars().all()
    assert logged.count("violation") == 2 and "ptp" in logged


def test_signature_replay_staleness_and_revocation(app_engine: Engine, world: dict[str, object]) -> None:
    t, lender, agency = world["t"], world["lender"], world["agency"]
    assert isinstance(t, str) and isinstance(lender, Identity) and isinstance(agency, Identity)
    with tenant_tx(t, app_engine) as c:
        msg = agency.sign("tsk_1", lender.card.name, "task.update", {"x": 1}, NOW)
        accept(c, t, lender.card.name, msg, NOW)
        with pytest.raises(A2AError, match="replayed"):
            accept(c, t, lender.card.name, msg, NOW)
        tampered = msg.model_copy(update={"body": {"x": 2}, "nonce": "fresh-nonce"})
        with pytest.raises(A2AError, match="signature"):
            accept(c, t, lender.card.name, tampered, NOW)
        with pytest.raises(A2AError, match="stale"):
            accept(c, t, lender.card.name, agency.sign("tsk_1", lender.card.name, "x", {}, NOW - timedelta(hours=1)),
                   NOW)
        impostor = Identity.create("acme-collections.agent", "Impostor", [], "https://evil.example")
        with pytest.raises(A2AError, match="signature"):
            accept(c, t, lender.card.name, impostor.sign("tsk_1", lender.card.name, "x", {}, NOW), NOW)
        with pytest.raises(A2AError, match="another agent"):
            accept(c, t, lender.card.name, agency.sign("tsk_1", "someone.else", "x", {}, NOW), NOW)
        revoke(c, t, agency.card.name, NOW)
        with pytest.raises(A2AError, match="untrusted"):
            accept(c, t, lender.card.name, agency.sign("tsk_1", lender.card.name, "x", {}, NOW), NOW)


def test_customer_agent_request_needs_consent(app_engine: Engine, world: dict[str, object]) -> None:
    state, out = handle_customer_request({"action": "pause_subscription", "months": 1})
    assert state == TaskState.INPUT_REQUIRED and out["needs"] == "consent_ref"
    state, out = handle_customer_request({"action": "pause_subscription", "months": 1, "consent_ref": "cns_1"})
    assert state == TaskState.WORKING
    state, _ = handle_customer_request({"action": "pause_subscription", "months": 9, "consent_ref": "cns_1"})
    assert state == TaskState.INPUT_REQUIRED
    state, _ = handle_customer_request({"action": "delete_my_loan", "consent_ref": "cns_1"})
    assert state == TaskState.FAILED

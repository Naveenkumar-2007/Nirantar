from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, text

from nirantar.approvals.service import decide
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
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlAuditStore
from nirantar.mcp.gateway import ScopeError, Tool, ToolContext, ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.mock import MockProvider
from nirantar.policy.engine import TenantPolicyConfig
from nirantar.security.rbac import Principal

pytestmark = pytest.mark.integration
DAY_IST = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)     # 10:30 IST
NIGHT_IST = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)  # 22:30 IST


class Empty(BaseModel):
    note: str = "x"


def _always_tool() -> Tool:
    return Tool("treasury.request_credit_draw", "test tool", Empty, lambda _c, _a: {"provider_ref": "draw_1"},
                "money", approval="always")


@pytest.fixture
def setup(app_engine: Engine) -> dict[str, Any]:
    tenant = new_id("ten")
    mock, sink = MockProvider(), MockCommsSink()
    with tenant_tx(tenant, app_engine) as c:
        create_tenant(c, tenant, "Chai Club")
        connect_provider(c, tenant, "mock", "test", "literal:x", "literal:y")
        cust = create_customer(c, tenant, NewCustomer("e1", "Priya", "+919876543210", None, "te",
                                                      consents={"whatsapp": True, "sms": True}))
        sub = create_subscription(c, tenant, cust, "mock", mock.add_subscription(cust, Money.of("999")),
                                  Money.of("999"))
        debit = schedule_debit(c, tenant, sub, date(2026, 10, 8), DAY_IST)
    clock = FixedClock(DAY_IST)
    tools = {**TOOLS, "treasury.request_credit_draw": _always_tool()}
    scopes = {**AGENT_SCOPES, "treasury_agent": frozenset({"treasury.request_credit_draw"})}
    gw = ToolGateway(app_engine, tools, scopes,
                     {"engine": app_engine, "provider": mock, "comms": sink,
                      "policy_config": lambda _c, _t: TenantPolicyConfig()}, clock=clock)
    return {"gw": gw, "tenant": tenant, "customer": cust, "debit": debit, "sink": sink, "clock": clock}


def test_allowed_message_executes_once_and_is_audited(setup: dict[str, Any], app_engine: Engine) -> None:
    gw, t = setup["gw"], setup["tenant"]
    args = {"customer_id": setup["customer"], "debit_id": setup["debit"],
            "text": "Hi Priya, your ₹999.00 renewal didn't go through. Pay here: https://mock.pay/x"}
    r1 = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.send_whatsapp", args=args, case_id="c1")
    r2 = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.send_whatsapp", args=args, case_id="c1")
    assert r1.status == "executed" and r1.output["provider_ref"].startswith("wamid.")
    assert r2.action_id == r1.action_id and len(setup["sink"].messages) == 1  # duplicate agent action deduped
    with tenant_tx(t, app_engine) as c:
        assert AuditChain(SqlAuditStore(c)).verify(t) >= 2
        assert c.execute(text("SELECT count(*) FROM ops.contacts")).scalar_one() == 1


def test_denied_at_night_but_mandatory_notice_still_goes(setup: dict[str, Any]) -> None:
    gw, t = setup["gw"], setup["tenant"]
    setup["clock"]._at = NIGHT_IST
    r = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="comms.send_whatsapp", case_id="c2",
                args={"customer_id": setup["customer"], "debit_id": setup["debit"],
                      "text": "Hi, your renewal didn't go through. Pay here: https://mock.pay/x"})
    assert r.status == "denied" and r.decision is not None
    assert "NIR-GOV-CONTACT-WINDOW-001" in {h.policy_id for h in r.decision.hits}
    notice = gw.call(tenant_id=t, agent_id="debit_strategist", tool_name="comms.send_predebit_notice",
                     args={"debit_id": setup["debit"]}, case_id="c2")
    assert notice.status == "executed" and "₹999.00" in notice.output["text"]
    # dry-run checks are compliance events: their decision is persisted on the action row
    dry = gw.call(tenant_id=t, agent_id="conductor", tool_name="policy.check_action", case_id="c2",
                  args={"customer_id": setup["customer"], "action_kind": "send_whatsapp", "purpose": "recovery"})
    with tenant_tx(t, gw.engine) as c:
        pd = c.execute(text("SELECT policy_decision FROM ops.actions WHERE action_id=:a"),
                       {"a": dry.action_id}).scalar_one()
    assert dry.output["outcome"] == "DENY" and pd == "DENY"


def test_hallucinated_amount_is_blocked(setup: dict[str, Any]) -> None:
    r = setup["gw"].call(tenant_id=setup["tenant"], agent_id="conversation_agent", tool_name="comms.send_whatsapp",
                         case_id="c3", args={"customer_id": setup["customer"], "debit_id": setup["debit"],
                                             "text": "Please pay ₹1,999 today to continue your plan."})
    assert r.status == "failed" and "debit is INR 999.00" in (r.error or "")
    assert not setup["sink"].messages


def test_scope_and_schema_enforcement(setup: dict[str, Any]) -> None:
    gw, t = setup["gw"], setup["tenant"]
    with pytest.raises(ScopeError):
        gw.call(tenant_id=t, agent_id="verifier", tool_name="comms.send_whatsapp", args={})
    bad = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="gateway.create_payment_link",
                  args={"debit_id": setup["debit"], "attempt": 1, "amount_minor": 1})  # extra field ignored
    assert bad.status == "executed" and bad.output["amount_minor"] == 99900       # amount from the debit only
    invalid = gw.call(tenant_id=t, agent_id="conversation_agent", tool_name="gateway.create_payment_link",
                      args={"debit_id": setup["debit"], "attempt": 99})
    assert invalid.status == "invalid"


def test_approval_round_trip(setup: dict[str, Any]) -> None:
    gw, t = setup["gw"], setup["tenant"]
    pending = gw.call(tenant_id=t, agent_id="treasury_agent", tool_name="treasury.request_credit_draw", args={},
                      case_id="c9")
    assert pending.status == "pending_approval" and pending.approval_id
    with tenant_tx(t, gw.engine) as c:
        token = decide(c, Principal(t, "prn_cfo", "user", ("finance_approver",)), pending.approval_id, True,
                       DAY_IST)
    done = gw.call(tenant_id=t, agent_id="treasury_agent", tool_name="treasury.request_credit_draw", args={},
                   case_id="c9", approval_token=token)
    assert done.status == "executed" and done.output["provider_ref"] == "draw_1"


def test_tool_context_is_passed(setup: dict[str, Any]) -> None:
    seen: list[ToolContext] = []
    tool = Tool("x.echo", "", Empty, lambda ctx, _a: (seen.append(ctx) or {}), "read")
    gw = ToolGateway(setup["gw"].engine, {"x.echo": tool}, {"a": frozenset({"x.echo"})}, {}, clock=setup["clock"])
    gw.call(tenant_id=setup["tenant"], agent_id="a", tool_name="x.echo", args={}, case_id="k")
    assert seen[0].agent_id == "a" and seen[0].case_id == "k"

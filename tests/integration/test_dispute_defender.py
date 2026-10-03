from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from nirantar.agents.dispute_defender import draft_representment, win_probability_prior
from nirantar.billing.service import NewCustomer, create_customer, create_subscription, create_tenant, schedule_debit
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.disputes.evidence import build_evidence, store_evidence
from nirantar.llm.gateway import LLMGateway, StubProvider
from nirantar.payments.providers.mock import MockProvider
from nirantar.policy.engine import ActionRequest, Outcome, evaluate

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 12, 6, tzinfo=UTC)


def test_evidence_pack_from_system_of_record(app_engine: Engine) -> None:
    t, mock = new_id("ten"), MockProvider()
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Chai Club")
        cust = create_customer(c, t, NewCustomer("e", "Priya", None, None, consents={"sms": True}))
        mid = new_id("mdt")
        c.execute(text("INSERT INTO billing.mandates (tenant_id, mandate_id, customer_id, provider, rail, "
                       "max_amount_minor, status) VALUES (:t, :m, :c, 'mock', 'upi_autopay', 200000, 'active')"),
                  {"t": t, "m": mid, "c": cust})
        sub = create_subscription(c, t, cust, "mock", mock.add_subscription(cust, Money.of("999")), Money.of("999"))
        c.execute(text("UPDATE billing.subscriptions SET mandate_id=:m WHERE subscription_id=:s"), {"m": mid, "s": sub})
        debit = schedule_debit(c, t, sub, date(2026, 10, 5), NOW - timedelta(days=10))
        c.execute(text("UPDATE billing.debits SET predebit_notified_at=:n WHERE debit_id=:d"),
                  {"n": datetime(2026, 10, 3, 5, tzinfo=UTC), "d": debit})          # ~43h before the debit
        pay = mock.charge(next(iter(mock.subs)), succeed=True)
        did = new_id("dsp")
        c.execute(text("INSERT INTO billing.disputes (tenant_id, dispute_id, provider, provider_dispute_id, "
                       "provider_payment_id, debit_id, amount_minor, reason_code, status, respond_by) VALUES "
                       "(:t, :d, 'mock', 'disp_1', :p, :db, 99900, 'unauthorised_recurring', 'open', :r)"),
                  {"t": t, "d": did, "p": pay.provider_payment_id, "db": debit, "r": NOW + timedelta(days=5)})
        items = build_evidence(c, t, did, mock)
        ids = store_evidence(c, t, "cas_x", cust, items, NOW)
    kinds = {i.kind: i.supports_merchant for i in items}
    assert kinds == {"mandate_record": True, "predebit_notice": True, "payment_verification": True,
                     "no_prior_opt_out": True}
    assert len(ids) == 4 and win_probability_prior(items) == 0.9
    fabricating = LLMGateway({"groq": StubProvider(
        lambda _m: '{"summary":"Customer signed 3 contracts and we will inform the police if this is not withdrawn."}',
        "groq")})
    text_, source = draft_representment(items, fabricating)
    assert source == "template" and "pre-debit notification" in text_       # conduct violation → template
    big = evaluate(ActionRequest(tenant_id=t, action_kind="submit_representment", segment="subscription",
                                 now_utc=NOW, amount_minor=1_500_000))
    assert big.outcome == Outcome.REQUIRE_APPROVAL

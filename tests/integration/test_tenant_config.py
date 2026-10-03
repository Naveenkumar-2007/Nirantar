"""Tenant configuration end-to-end on real Postgres: versioning, audit, maker-checker templates, isolation,
and — most importantly — that agents and tools actually USE the tenant's values."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

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
from nirantar.experiments import service as experiments
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.mock import MockProvider
from nirantar.settings import learning, runtime, templates
from nirantar.settings import service as settings
from nirantar.settings.schema import platform_defaults

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)          # 10:30 IST
NS = platform_defaults()["namespaces"]


@pytest.fixture
def tenant(app_engine: Engine) -> dict[str, Any]:
    t = new_id("ten")
    mock = MockProvider()
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Config Co", {"segments": ["subscription"]})
        connect_provider(c, t, "mock", "test", "literal:x", "literal:y")
        cust = create_customer(c, t, NewCustomer("e1", "Priya Rao", "+919876543210", None, "te",
                                                 consents={"whatsapp": True, "sms": True}))
        sub = create_subscription(c, t, cust, "mock", mock.add_subscription(cust, Money.of("999")), Money.of("999"))
        debit = schedule_debit(c, t, sub, date(2026, 10, 8), NOW)
    return {"tenant": t, "customer": cust, "debit": debit, "mock": mock}


def _gw(app_engine: Engine, env: dict[str, Any], sink: MockCommsSink, at: datetime = NOW) -> ToolGateway:
    return ToolGateway(app_engine, TOOLS, AGENT_SCOPES,
                       {"engine": app_engine, "provider": env["mock"], "comms": sink}, clock=FixedClock(at))


# ------------------------------------------------------------------ settings versions
def test_settings_default_then_versioned_with_audit_event_and_conflict(app_engine: Engine,
                                                                      tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    new = {"capacity_per_round": {"whatsapp": 250, "voice": 0}, "cost_minor": {"whatsapp": 85, "voice": 800}}
    with tenant_tx(t, app_engine) as c:
        assert settings.get(c, t, "channels").version == 0                # platform default until edited
        v1 = settings.update(c, t, "channels", new, actor="user:ops", reason="Gupshup rate card", now=NOW,
                             expected_version=0)
        assert v1.version == 1 and settings.get(c, t, "channels").value == new
        with pytest.raises(settings.SettingsConflict):
            settings.update(c, t, "channels", new, actor="user:other", reason="stale edit", now=NOW,
                            expected_version=0)
        hist = settings.history(c, t, "channels")
        assert [h["version"] for h in hist] == [1, 0]
        assert c.execute(text("SELECT count(*) FROM events.outbox WHERE event_type='config.settings_changed' "
                              "AND tenant_id=:t"), {"t": t}).scalar_one() == 1
        assert "settings.changed" in [r.action for r in SqlAuditStore(c).all(t)]
        AuditChain(SqlAuditStore(c)).verify(t)                           # chain still intact
        with pytest.raises(ValueError):
            settings.update(c, t, "policy", {"contact_window": ["06:00", "23:00"]}, actor="user:ops",
                            reason="loosen", now=NOW, expected_version=0)


def test_settings_rows_are_append_only_and_tenant_isolated(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    other = new_id("ten")
    with tenant_tx(other, app_engine) as c:
        create_tenant(c, other, "Other")
    with tenant_tx(t, app_engine) as c:
        settings.update(c, t, "experiments", {"holdout_bp": 500, "holdout_opt_in": False}, actor="user:a",
                        reason="smaller holdout", now=NOW, expected_version=0)
    with tenant_tx(other, app_engine) as c:
        assert settings.get(c, other, "experiments").version == 0          # can't see t's settings
        assert c.execute(text("SELECT count(*) FROM config.settings_versions")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(t, app_engine) as c:
        c.execute(text("UPDATE config.settings_versions SET reason='rewritten history' WHERE tenant_id=:t"),
                  {"t": t})
    with pytest.raises(DBAPIError), tenant_tx(other, app_engine) as c:  # cannot write rows for another tenant
        c.execute(text("INSERT INTO config.settings_versions (tenant_id, namespace, version, value, changed_by, "
                       "reason, created_at) VALUES (:t, 'channels', 9, '{}'::jsonb, 'x', 'x', now())"), {"t": t})


def test_experiment_holdout_comes_from_settings(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    with tenant_tx(t, app_engine) as c:
        assert experiments.default_holdout_bp(c, t) == NS["experiments"]["holdout_bp"]
        settings.update(c, t, "experiments", {"holdout_bp": 500, "holdout_opt_in": False}, actor="user:a",
                        reason="smaller holdout", now=NOW, expected_version=0)
        assert experiments.default_holdout_bp(c, t) == 500


# ------------------------------------------------------------------ policy settings reach the Guardian
def test_tenant_contact_window_is_enforced_by_the_gateway(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t, sink = tenant["tenant"], MockCommsSink()
    args = {"customer_id": tenant["customer"], "debit_id": tenant["debit"],
            "text": "Hi Priya, your ₹999.00 renewal didn't go through. Pay here: https://mock.pay/x"}
    # 10:30 IST is inside the platform window; the tenant narrows its window to 11:00-18:00
    with tenant_tx(t, app_engine) as c:
        settings.update(c, t, "policy", {"contact_window": ["11:00", "18:00"]}, actor="user:compliance",
                        reason="brand policy: no calls before 11", now=NOW, expected_version=0)
    r = _gw(app_engine, tenant, sink).call(tenant_id=t, agent_id="conversation_agent",
                                           tool_name="comms.send_whatsapp", args=args, case_id="c1")
    assert r.status == "denied" and not sink.messages


# ------------------------------------------------------------------ templates
def test_template_maker_checker_and_the_tools_use_the_approved_version(app_engine: Engine,
                                                                       tenant: dict[str, Any]) -> None:
    t, sink = tenant["tenant"], MockCommsSink()
    body = "Namaskaram! {amount} will be auto-debited on {date} for your {plan}. Reply STOP to manage it."
    with tenant_tx(t, app_engine) as c:
        p = templates.propose(c, t, "sms.predebit_notice", "en", body, actor="user:maker", now=NOW)
        assert p["status"] == "pending"
        # pending proposals are not used
        assert templates.resolve(c, t, "sms.predebit_notice", "te").source == "platform_default"
        with pytest.raises(templates.TemplateReviewError, match="maker-checker"):
            templates.decide(c, t, "sms.predebit_notice", "en", p["version"], approver="user:maker", approve=True,
                             now=NOW)
    with tenant_tx(t, app_engine) as c:
        templates.decide(c, t, "sms.predebit_notice", "en", p["version"], approver="user:checker", approve=True,
                         now=NOW)
        live = templates.resolve(c, t, "sms.predebit_notice", "te")      # no Telugu version → tenant English
        assert (live.source, live.version, live.language) == ("tenant", 1, "en")
    r = _gw(app_engine, tenant, sink).call(tenant_id=t, agent_id="debit_strategist",
                                           tool_name="comms.send_predebit_notice",
                                           args={"debit_id": tenant["debit"]}, case_id="c1")
    assert r.status == "executed" and r.output["template_ref"] == "sms.predebit_notice@en:v1"
    assert sink.messages[-1].text.startswith("Namaskaram! ₹999.00 will be auto-debited on 08 Oct 2026")


def test_template_versions_are_immutable_and_one_live_per_language(app_engine: Engine,
                                                                   tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    with tenant_tx(t, app_engine) as c:
        for i in range(2):
            v = templates.propose(c, t, "whatsapp.recovery", "en",
                                  f"Hello {{name}} ({i}), {{amount}} for {{plan}} is pending: {{link}}",
                                  actor="user:maker", now=NOW)["version"]
            templates.decide(c, t, "whatsapp.recovery", "en", v, approver="user:checker", approve=True, now=NOW)
        rows = c.execute(text("SELECT version, status FROM config.templates WHERE tenant_id=:t "
                              "ORDER BY version"), {"t": t}).all()
        assert [(r.version, r.status) for r in rows] == [(1, "retired"), (2, "approved")]
    with pytest.raises(DBAPIError), tenant_tx(t, app_engine) as c:
        c.execute(text("UPDATE config.templates SET body='sneaky edit {amount} {link}' WHERE tenant_id=:t"),
                  {"t": t})
    with pytest.raises(DBAPIError), tenant_tx(t, app_engine) as c:   # DB enforces maker ≠ checker too
        c.execute(text("UPDATE config.templates SET decided_by=created_by WHERE tenant_id=:t AND version=2"),
                  {"t": t})


def test_get_template_tool_serves_tenant_wording_to_agents(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    r = _gw(app_engine, tenant, MockCommsSink()).call(
        tenant_id=t, agent_id="conversation_agent", tool_name="content.get_template",
        args={"key": "whatsapp.recovery", "language": "te"}, case_id="c1")
    assert r.status == "executed" and r.output["ref"] == "whatsapp.recovery@te:v0"
    assert r.output["body"] == templates.platform_default("whatsapp.recovery", "te")


# ------------------------------------------------------------------ learned parameters + runtime
def test_runtime_uses_prior_until_learned_and_records_the_source(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    with tenant_tx(t, app_engine) as c:
        rt = runtime.load(c, t)
        assert rt.sources["effects"] == "prior" and rt.sources["risk_threshold"] == "fallback:insufficient_evidence"
        assert rt.risk_threshold == NS["strategy"]["risk_threshold"]["fixed"]
        out = learning.refresh(c, t, NOW)                                # no outcomes yet → evidence says so
        assert out["risk_threshold"]["value"]["threshold"] is None
        assert out["effects"]["evidence"]["outcomes_used"] == 0
        rt2 = runtime.load(c, t)
        assert rt2.sources["effects"] == "prior:insufficient_evidence"   # never claims "learned" without evidence
        assert dict(rt2.priors.effects["INSUFFICIENT_FUNDS"]) == NS["effects"]["prior"]["INSUFFICIENT_FUNDS"]
        settings.update(c, t, "strategy", {"risk_threshold": {**NS["strategy"]["risk_threshold"], "mode": "fixed",
                                                              "fixed": 0.5}},
                        actor="user:a", reason="manual override", now=NOW, expected_version=0)
        assert runtime.load(c, t).risk_threshold == 0.5
        with pytest.raises(DBAPIError):
            c.execute(text("DELETE FROM config.learned_params WHERE tenant_id=:t"), {"t": t})


def test_effects_are_learned_from_real_db_outcomes(app_engine: Engine) -> None:
    """Runs the production SQL (assignment + exposure + failed payment reason → category) on enough outcomes."""
    import random

    t, rng = new_id("ten"), random.Random(11)
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "Learner Co", {"segments": ["subscription"]})
        settings.update(c, t, "experiments", {"holdout_bp": 3000, "holdout_opt_in": False}, actor="user:a",
                        reason="big holdout for the test", now=NOW, expected_version=0)
        exp = experiments.create_experiment(c, t, "learn", {"treatment": 7000})
        for i in range(400):
            cust, debit = f"cus_{i}", f"deb_{i}"
            arm = experiments.assign(c, t, exp, cust, NOW)
            contacted = arm != "holdout" and rng.random() < 0.8
            if arm == "holdout":
                experiments.log_exposure(c, t, exp, cust, "holdout", None, NOW)
            else:
                experiments.log_exposure(c, t, exp, cust, "whatsapp" if contacted else "none", None, NOW)
            c.execute(text("INSERT INTO billing.payments (tenant_id, payment_id, provider, provider_payment_id, debit_id, "
                           "customer_id, amount_minor, status, error_code, error_reason) VALUES (:t, :p, 'mock', :p, :d, "
                           ":c, 99900, 'failed', 'BAD_REQUEST_ERROR', 'insufficient_funds')"),
                      {"t": t, "p": f"pay_{i}", "d": debit, "c": cust})
            recovered = rng.random() < (0.30 + (0.25 if contacted else 0.0))
            experiments.record_outcome(c, t, exp, cust, debit, "recovered" if recovered else "unrecovered",
                                       99_900 if recovered else 0, True, NOW)
        out = learning.refresh(c, t, NOW)
        ev = out["effects"]["evidence"]["categories"]["INSUFFICIENT_FUNDS"]
        assert out["effects"]["evidence"]["outcomes_used"] == 400
        assert ev["source"] == "learned" and ev["holdout"] > 60 and ev["contacted"]["whatsapp"] > 150
        assert ev["cace_ci95"][0] < 0.25 < ev["cace_ci95"][1]            # true effect of a contact = 0.25
        eff = out["effects"]["value"]["effects"]["INSUFFICIENT_FUNDS"]["whatsapp"]
        assert 0.06 < eff < 0.4                                         # moved from the 0.06 prior towards 0.25
        src = runtime.load(c, t).sources["effects"]
        assert src == f"learned:v{out['effects']['version']} (1/6 categories)"


def test_no_op_and_duplicate_proposals_are_refused(app_engine: Engine, tenant: dict[str, Any]) -> None:
    t = tenant["tenant"]
    live = templates.platform_default("whatsapp.recovery", "en") or ""
    new = "Hi {name}, {amount} for {plan} is pending. Pay here: {link}"
    with tenant_tx(t, app_engine) as c:
        with pytest.raises(templates.TemplateRejected, match="identical"):
            templates.propose(c, t, "whatsapp.recovery", "en", live, actor="user:maker", now=NOW)
        templates.propose(c, t, "whatsapp.recovery", "en", new, actor="user:maker", now=NOW)
        with pytest.raises(templates.TemplateRejected, match="already waiting"):
            templates.propose(c, t, "whatsapp.recovery", "en", new, actor="user:other", now=NOW)

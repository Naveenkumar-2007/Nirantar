from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.comms.sink import MockCommsSink
from nirantar.demo.seed import seed
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.mock import MockProvider

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def demo(app_engine: Engine, owner_engine: Engine) -> dict[str, Any]:
    out = seed(app_engine, n_customers=25, seed_value=3)
    other = seed(app_engine, n_customers=3, seed_value=4)
    gw = ToolGateway(app_engine, TOOLS, AGENT_SCOPES, {"engine": app_engine, "provider": MockProvider(),
                                                       "comms": MockCommsSink()})
    app = create_app(Services(engine=app_engine, owner_engine=owner_engine, extra={"gateway": gw}))
    return {"client": TestClient(app), **out, "other": other}


def h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_auth_is_required_and_rbac_enforced(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    assert c.get("/v1/overview").status_code == 401
    assert c.get("/v1/overview", headers=h("nk_ten_bogus.key_x.secret")).status_code == 401
    assert c.get("/v1/overview", headers=h(demo["api_keys"]["viewer"])).status_code == 200
    assert c.get("/v1/audit", headers=h(demo["api_keys"]["viewer"])).status_code == 403   # viewer lacks audit:read
    r = c.post("/v1/approvals/apr_x/decide", json={"grant": True}, headers=h(demo["api_keys"]["viewer"]))
    assert r.status_code == 403


def test_overview_reflects_the_real_pipeline(demo: dict[str, Any]) -> None:
    o = demo["client"].get("/v1/overview", headers=h(demo["api_keys"]["owner"])).json()
    s = demo["stats"]
    assert o["debits"]["total"] == s["debits"] == 25
    assert o["recovered"]["count"] == s["recovered"]
    assert o["failed_debits"] == s["failed"]
    assert o["pending_approvals"] == 1
    assert o["experiment"]["analysis"]["experiment_id"] == demo["experiment_id"]


def test_debit_timeline_and_pagination(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    key = h(demo["api_keys"]["owner"])
    first = c.get("/v1/debits?limit=10", headers=key).json()
    second = c.get(f"/v1/debits?limit=10&cursor={first['next_cursor']}", headers=key).json()
    ids1 = {d["debit_id"] for d in first["items"]}
    assert len(first["items"]) == 10 and ids1.isdisjoint({d["debit_id"] for d in second["items"]})
    assert c.get("/v1/debits?cursor=not-a-real-cursor!!", headers=key).status_code == 400
    failed = c.get("/v1/debits?limit=100", headers=key).json()["items"]
    target = next(d for d in failed if d["attempt_count"] > 0)
    detail = c.get(f"/v1/debits/{target['debit_id']}", headers=key).json()
    kinds = {t["title"] for t in detail["timeline"]}
    assert "payment.failed" in kinds and any(k.startswith("conductor") for k in kinds)
    assert detail["labels"], "verified outcome labels are attached"


def test_other_tenant_data_is_invisible(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    theirs = c.get("/v1/debits?limit=1", headers=h(demo["other"]["api_keys"]["owner"])).json()["items"][0]
    assert c.get(f"/v1/debits/{theirs['debit_id']}", headers=h(demo["api_keys"]["owner"])).status_code == 404


def test_approval_inbox_executes_through_mcp_after_grant(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    pending = c.get("/v1/approvals", headers=h(demo["api_keys"]["finance_approver"])).json()["items"]
    assert len(pending) == 1 and pending[0]["tool_name"] == "treasury.request_credit_draw"
    r = c.post(f"/v1/approvals/{pending[0]['approval_id']}/decide", json={"grant": True},
               headers=h(demo["api_keys"]["finance_approver"])).json()
    assert r["status"] == "granted" and r["executed"] and r["output"]["status"] == "requested"
    again = c.post(f"/v1/approvals/{pending[0]['approval_id']}/decide", json={"grant": True},
                   headers=h(demo["api_keys"]["finance_approver"]))
    assert again.status_code == 409


def test_audit_chain_verifies_and_platform_needs_its_key(demo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
                                                         ) -> None:
    c: TestClient = demo["client"]
    v = c.get("/v1/audit/verify", headers=h(demo["api_keys"]["owner"])).json()
    assert v["valid"] and v["records"] > 25
    assert c.get("/platform/health").status_code == 401
    monkeypatch.setenv("PLATFORM_ADMIN_KEY", "test-platform-key-123456")
    ph = c.get("/platform/health", headers={"X-Platform-Key": "test-platform-key-123456"}).json()
    assert ph["tenants"] >= 2 and "outbox_unpublished" in ph


# ------------------------------------------------------------------ tenant configuration API (ADR-0011)
def test_settings_api_versioning_rbac_and_validation(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    owner, viewer = h(demo["api_keys"]["owner"]), h(demo["api_keys"]["viewer"])
    got = c.get("/v1/settings", headers=viewer).json()
    assert got["can_edit"] is False and set(got["namespaces"]) == {"channels", "strategy", "effects",
                                                                   "experiments", "policy", "retention", "operations"}
    # the seeder ran learning.refresh; 25 customers is too little evidence, and the API says so honestly
    assert got["effective"]["sources"]["effects"] == "prior:insufficient_evidence"
    ch = got["namespaces"]["channels"]
    body = {"value": {**ch["value"], "cost_minor": {"whatsapp": 90, "voice": 700}}, "reason": "new rate card",
            "expected_version": ch["version"]}
    assert c.put("/v1/settings/channels", json=body, headers=viewer).status_code == 403
    r = c.put("/v1/settings/channels", json=body, headers=owner)
    assert r.status_code == 200 and r.json()["version"] == ch["version"] + 1
    assert c.put("/v1/settings/channels", json=body, headers=owner).status_code == 409          # stale version
    bad = {**body, "value": {**ch["value"], "capacity_per_round": {"whatsapp": -5, "voice": 0}},
           "expected_version": ch["version"] + 1}
    assert c.put("/v1/settings/channels", json=bad, headers=owner).status_code == 422
    loosen = {"value": {"contact_window": ["06:00", "23:00"]}, "reason": "x",
              "expected_version": got["namespaces"]["policy"]["version"]}
    assert c.put("/v1/settings/policy", json=loosen, headers=owner).status_code == 422
    assert c.put("/v1/settings/nope", json=body, headers=owner).status_code == 404
    hist = c.get("/v1/settings/channels/history", headers=viewer).json()["items"]
    assert hist[0]["value"]["cost_minor"] == {"whatsapp": 90, "voice": 700} and hist[-1]["version"] == 0
    assert c.get("/v1/settings", headers=viewer).json()["effective"]["cost_minor"] == {"whatsapp": 90, "voice": 700}


def test_learned_parameters_are_exposed_with_evidence(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    got = c.get("/v1/learned", headers=h(demo["api_keys"]["viewer"])).json()
    assert got["effects"]["evidence"]["outcomes_used"] == demo["stats"]["recovered"] + demo["stats"]["unrecovered"]
    assert "categories" in got["effects"]["evidence"]
    assert c.post("/v1/learned/refresh", headers=h(demo["api_keys"]["viewer"])).status_code == 403
    again = c.post("/v1/learned/refresh", headers=h(demo["api_keys"]["owner"])).json()
    assert again["effects"]["version"] == got["effects"]["version"] + 1


def test_template_api_maker_checker(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    owner, approver = h(demo["api_keys"]["owner"]), h(demo["api_keys"]["finance_approver"])
    cat = c.get("/v1/templates", headers=owner).json()
    assert cat["can_propose"] and any(i["key"] == "whatsapp.recovery" and i["language"] == "te"
                                      for i in cat["items"])
    rejected = c.post("/v1/templates", json={"key": "whatsapp.recovery", "language": "en",
                                             "body": "Pay ₹999 now or else: {link}"}, headers=owner)
    assert rejected.status_code == 422 and rejected.json()["detail"]["problems"]
    assert c.post("/v1/templates", json={"key": "whatsapp.recovery", "language": "en",
                                         "body": "Hi {name}, {amount} for {plan} is due: {link}"},
                  headers=approver).status_code == 403                              # approver can't author
    p = c.post("/v1/templates", json={"key": "whatsapp.recovery", "language": "en",
                                      "body": "Hi {name}, {amount} for {plan} is due. Pay safely: {link}"},
               headers=owner).json()
    path = f"/v1/templates/whatsapp.recovery/en/{p['version']}/decide"
    assert c.post(path, json={"grant": True}, headers=owner).status_code == 409       # author can't approve
    assert c.post(path, json={"grant": True}, headers=approver).json()["status"] == "approved"
    live = next(i for i in c.get("/v1/templates", headers=owner).json()["items"]
                if i["key"] == "whatsapp.recovery" and i["language"] == "en")["live"]
    assert live["source"] == "tenant" and live["version"] == p["version"]


# ------------------------------------------------------------------ data platform API (ADR-0012)
def test_data_sync_runs_the_pipeline_and_reports_health(demo: dict[str, Any], lake: Any) -> None:
    c: TestClient = demo["client"]
    c.app.state.services.extra["lake"] = lake  # type: ignore[attr-defined]
    owner, viewer = h(demo["api_keys"]["owner"]), h(demo["api_keys"]["viewer"])
    assert c.get("/v1/data/health", headers=viewer).json()["report"] is None      # nothing computed yet
    assert c.post("/v1/data/sync", headers=viewer).status_code == 403             # needs integrations:admin
    r = c.post("/v1/data/sync", headers=owner)
    assert r.status_code == 202                  # TestClient runs the background task before returning
    runs = c.get("/v1/data/runs", headers=viewer).json()["items"]
    assert runs[0]["run_id"] == r.json()["run_id"] and runs[0]["status"] == "succeeded"
    assert [s["step"] for s in runs[0]["steps"]][-1] == "health"
    rep = c.get("/v1/data/health", headers=viewer).json()["report"]
    assert rep["labels"]["cycles"] == demo["stats"]["debits"] == 25
    # first-attempt failures are counted on every attempted cycle (not only final ones): no censoring bias
    assert rep["labels"]["first_attempt_failures"] == demo["stats"]["failed"]
    assert rep["labels"]["recovered_cycles"] <= demo["stats"]["recovered"]
    assert rep["readiness"]["m1_debit_failure"]["ready"] is False
    other = c.get("/v1/data/health", headers=h(demo["other"]["api_keys"]["viewer"])).json()
    assert other["report"] is None                                                 # tenant-isolated


# ------------------------------------------------------------------ per-tenant ML API (ADR-0013)
def test_tenant_models_api_reports_cold_start_honestly(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    viewer, owner = h(demo["api_keys"]["viewer"]), h(demo["api_keys"]["owner"])
    r = c.get("/v1/ml/models", headers=viewer).json()
    assert r["versions"] == [] and r["can_manage"] is False          # 25 demo customers: no model, no pretending
    assert set(r["served_30d"]) <= {"prior"}                           # every decision so far came from the prior
    assert c.post("/v1/ml/train", headers=viewer).status_code == 403
    assert c.post("/v1/ml/models/1/retire", json={"reason": "x"}, headers=viewer).status_code == 403
    assert c.post("/v1/ml/models/999/retire", json={"reason": "not live"}, headers=owner).status_code == 409


# ------------------------------------------------------------------ retention API (ADR-0014)
def test_retention_overview_is_honest_for_a_small_tenant(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    viewer = h(demo["api_keys"]["viewer"])
    r = c.get("/v1/retention/overview", headers=viewer).json()
    assert r["can_manage"] is False and r["winback"] == [] and r["cases"] == {} and r["offers"] == {}
    assert r["models"] == [] and r["fit"] is None and r["at_risk"] is None      # nothing fitted: nothing claimed
    assert c.post("/v1/retention/refresh", headers=viewer).status_code == 403
    other = c.get("/v1/retention/overview", headers=h(demo["other"]["api_keys"]["viewer"])).json()
    assert other["winback"] == [] and other["fit"] is None                      # tenant-isolated


def test_dead_letter_replay_republishes_the_original_event(demo: dict[str, Any], app_engine: Engine) -> None:
    import json

    from sqlalchemy import text

    from nirantar.db.session import tenant_tx

    c: TestClient = demo["client"]
    t = demo["tenant_id"]
    with tenant_tx(t, app_engine) as conn:
        ev = conn.execute(text("SELECT event_id, event_type, envelope FROM events.outbox ORDER BY created_at "
                               "LIMIT 1")).one()
        conn.execute(text("UPDATE events.outbox SET published_at=now() WHERE event_id=:e"), {"e": ev.event_id})
        conn.execute(text("INSERT INTO events.consumer_dead_letters (tenant_id, event_id, consumer, event_type, error, "
                          "envelope, attempts) VALUES (:t, :e, 'event-bridge', :et, 'boom', CAST(:env AS jsonb), 3)"),
                     {"t": t, "e": ev.event_id, "et": ev.event_type, "env": json.dumps(ev.envelope)})
    listed = c.get("/v1/events/dead-letters", headers=h(demo["api_keys"]["viewer"])).json()["items"]
    assert [x["event_id"] for x in listed] == [ev.event_id]
    body = {"reason": "provider credentials fixed"}
    assert c.post(f"/v1/events/dead-letters/{ev.event_id}/replay", json=body,
                  headers=h(demo["api_keys"]["viewer"])).status_code == 403
    r = c.post(f"/v1/events/dead-letters/{ev.event_id}/replay", json=body, headers=h(demo["api_keys"]["owner"]))
    assert r.status_code == 200
    assert c.post(f"/v1/events/dead-letters/{ev.event_id}/replay", json=body,
                  headers=h(demo["api_keys"]["owner"])).status_code == 404          # replayed once
    assert c.get("/v1/events/dead-letters", headers=h(demo["other"]["api_keys"]["owner"])).json()["items"] == []
    with tenant_tx(t, app_engine) as conn:
        assert conn.execute(text("SELECT published_at FROM events.outbox WHERE event_id=:e"),
                            {"e": ev.event_id}).scalar_one() is None                  # the relay will re-deliver it
    audit = c.get("/v1/audit?limit=5", headers=h(demo["api_keys"]["owner"])).json()["items"]
    assert any(a["action"] == "event.replayed" for a in audit)


def test_customer_360_hides_contact_details_and_respects_tenancy(demo: dict[str, Any]) -> None:
    c: TestClient = demo["client"]
    key = h(demo["api_keys"]["viewer"])
    cust = c.get("/v1/customers?limit=1", headers=key).json()["items"][0]
    d = c.get(f"/v1/customers/{cust['customer_id']}", headers=key).json()
    assert d["customer"]["customer_id"] == cust["customer_id"]
    assert {"subscriptions", "mandates", "debits", "cases", "contacts", "actions", "replies"} <= d.keys()
    assert d["subscriptions"] and d["debits"], "the seeded customer has a plan and debits"
    assert d["customer"]["has_phone"] is True
    flat = str(d)
    assert "phone_enc" not in flat and "+91" not in flat                     # only "has a phone", never the number
    theirs = c.get("/v1/customers?limit=1", headers=h(demo["other"]["api_keys"]["owner"])).json()["items"][0]
    assert c.get(f"/v1/customers/{theirs['customer_id']}", headers=key).status_code == 404
    assert c.get("/v1/conversations", headers=key).json()["items"] == []    # the mock channel keeps no WhatsApp log

"""Activities for DebitCycleWorkflow. Short-lived, retryable, idempotent.

All customer/money side effects go through the MCP ToolGateway (policy, approvals, audit).
Every activity takes the workflow's `now` so decisions (contact windows, expiry) follow workflow time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from temporalio import activity

from nirantar.agents import debit_strategist
from nirantar.agents.conductor import ConductorDeps, FailedDebit, handle_failure
from nirantar.core.clock import FixedClock
from nirantar.db.session import tenant_tx
from nirantar.features.online import OnlineStore
from nirantar.llm.gateway import LLMGateway
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.ml.live_eval import evaluate_live
from nirantar.ml.router import ModelRouter
from nirantar.outcomes.service import close_cycle
from nirantar.payments.domain import PaymentProvider
from nirantar.payments.providers.resolver import ProviderResolver, provider_for
from nirantar.payments.reconciliation import reconcile_open_debits, reconcile_payment_requests
from nirantar.settings import runtime as tenant_runtime
from nirantar.voice.exotel import default_client as default_voice
from nirantar.workflows.types import StepInput

_PROMISE = re.compile(
    r"\b(will pay|pay (today|tonight|tomorrow)|paying|i'?ll pay|salary|kal|parso|tomorrow|tonight|"
    # Hinglish: "pay kar dunga", "de denge", "bhar dunga", "bhej dungi" …
    r"(pay|payment|paisa|paise|bhugtan)\s+(kar|de|bhar|bhej)\s*(unga|ungi|dunga|dungi|denge|enge|doonga|dena)|"
    r"(kar|de|bhar|bhej)\s*(dunga|dungi|denge|doonga|unga|ungi)|"
    # romanised Telugu: "kadatanu", "kadatha", "chellistanu", "pay chestanu"
    r"kad(a|u)t(a|h)(a|nu)?|kadatha|chellist(a|h)anu|pay\s+chest(a|h)anu|repu|ellundi)\b"
    # Devanagari / Telugu script (no \b: word boundaries are ASCII-only here)
    r"|कर दूंगा|दे दूंगा|भुगतान कर|कल|परसों|చెల్లిస్తాను|కడతాను|రేపు",
    re.IGNORECASE)
_CANCEL = re.compile(r"\b(cancel|unsubscribe|don'?t want)\b", re.IGNORECASE)
_STOP = re.compile(r"^\s*stop\b", re.IGNORECASE)
_DISTRESS = re.compile(r"\b(lost (my )?job|hospital|medical|can'?t afford|hardship|emergency)\b", re.IGNORECASE)


def classify_reply(text_: str) -> str:
    """M8 rule baseline (the fine-tuned intent model is a later milestone)."""
    if _STOP.search(text_):
        return "opt_out"
    if _DISTRESS.search(text_):
        return "hardship"
    if _CANCEL.search(text_):
        return "cancel_request"
    if _PROMISE.search(text_):
        return "promise_to_pay"
    return "other"


@dataclass
class Deps:
    engine: Engine
    provider: PaymentProvider | ProviderResolver     # services pass a resolver: each tenant's own account
    comms: Any
    llm: LLMGateway | None
    features: OnlineStore | None         # online feature store (Redis); None → no risk prediction
    router: ModelRouter | None            # per-tenant champion / canary / shadow / prior
    environment: str = "local"
    voice: Any = None                     # Exotel client (tests inject a fake); None → the deployment's, if any


class DebitActivities:
    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def _gateway(self, step: StepInput) -> ToolGateway:
        return ToolGateway(
            self.d.engine, TOOLS, AGENT_SCOPES,
            {"engine": self.d.engine, "voice": self.d.voice or default_voice(),
             "provider": provider_for(self.d.provider, step.cycle.tenant_id),
             "comms": self.d.comms,
             "experiment_id": step.cycle.experiment_id},
            clock=FixedClock(datetime.fromisoformat(step.now_iso)), environment=self.d.environment)

    @staticmethod
    def _now(step: StepInput) -> datetime:
        return datetime.fromisoformat(step.now_iso)

    @activity.defn(name="open_case")
    def open_case(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            c.execute(text("INSERT INTO ops.cases (tenant_id, case_id, kind, subject_id, customer_id, status, "
                           "workflow_id, opened_at) VALUES (:t, :c, 'debit_cycle', :d, :cu, 'open', :w, :n) "
                           "ON CONFLICT DO NOTHING"),
                      {"t": c_.tenant_id, "c": step.case_id, "d": c_.debit_id, "cu": c_.customer_id,
                       "w": activity.info().workflow_id, "n": self._now(step)})
        return {"case_id": step.case_id}

    @activity.defn(name="predict_risk")
    def predict_risk(self, step: StepInput) -> dict[str, Any]:
        """M1 at T-3 via the feature store + model router. Same feature function as training (no skew); the
        tenant's champion/canary decides, else the prior. No feature store → no prediction (never a guess)."""
        c_ = step.cycle
        if self.d.features is None or self.d.router is None:
            return {"p_fail": None, "reason": "feature store / model router not configured"}
        now = self._now(step)
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            existing = c.execute(text("SELECT prediction_id, score, model_version FROM ai.predictions WHERE "
                                      "tenant_id=:t AND subject_id=:d AND model_name='m1_debit_failure' AND "
                                      "output->>'role'='decision'"), {"t": c_.tenant_id, "d": c_.debit_id}).first()
            if existing:  # activity retry: never predict twice for the same debit
                return {"p_fail": existing.score, "prediction_id": existing.prediction_id,
                        "model_version": existing.model_version}
            d = c.execute(text("SELECT subscription_id, scheduled_for, amount_minor FROM billing.debits "
                               "WHERE tenant_id=:t AND debit_id=:d"), {"t": c_.tenant_id, "d": c_.debit_id}).one()
        due = datetime(d.scheduled_for.year, d.scheduled_for.month, d.scheduled_for.day, tzinfo=now.tzinfo)
        try:
            of = self.d.features.features_for(c_.tenant_id, d.subscription_id, as_of=now, due_at=due,
                                              amount_minor=int(d.amount_minor), method=None, bank=None)
        except Exception as exc:  # online store down: say so; the strategist then sends only the mandatory notice
            return {"p_fail": None, "reason": f"online feature store unavailable: {type(exc).__name__}"}
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            dec = self.d.router.score(c, c_.tenant_id, subject_id=c_.debit_id, entity_id=d.subscription_id,
                                      features=of.row, now=now,
                                      context={"history_cycles": of.history_cycles, "cold_start": of.cold_start,
                                               "materialized_at": of.materialized_at})
        return {"p_fail": dec.score, "prediction_id": dec.prediction_id, "model_version": dec.version,
                "served_by": dec.served_by, "cold_start": of.cold_start,
                "cash_day_distance": float(of.row["paid_day_distance"])}

    def _method(self, tenant_id: str, debit_id: str) -> str:
        with tenant_tx(tenant_id, self.d.engine) as c:
            m: str = c.execute(text("SELECT s.collection_method FROM billing.debits d JOIN billing.subscriptions s ON "
                                    "s.tenant_id=d.tenant_id AND s.subscription_id=d.subscription_id WHERE "
                                    "d.tenant_id=:t AND d.debit_id=:d"), {"t": tenant_id, "d": debit_id}).scalar_one()
        return m

    @activity.defn(name="pre_debit")
    def pre_debit(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        if self._method(c_.tenant_id, c_.debit_id) == "payment_link":
            # nothing is auto-debited: an "auto-debit on <date>" notice would be false. The due-date payment request
            # (collect) is the customer's notice.
            return {"plan": None, "notice": {"status": "skipped", "reason": "pay-by-link: no auto-debit"}}
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            rt = tenant_runtime.load(c, c_.tenant_id)
        plan = debit_strategist.plan(debit_strategist.StrategyIn(
            debit_id=c_.debit_id, p_fail=step.payload.get("p_fail"), risk_threshold=rt.risk_threshold,
            cash_day_distance=step.payload.get("cash_day_distance")))
        r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="debit_strategist",
                                     tool_name="comms.send_predebit_notice", args={"debit_id": c_.debit_id},
                                     case_id=step.case_id)
        return {"plan": plan.model_dump(), "risk_threshold_source": rt.sources["risk_threshold"],
                "notice": {"status": r.status, "action_id": r.action_id,
                           "provider_ref": r.output.get("provider_ref"), "template_ref": r.output.get("template_ref")}}

    @activity.defn(name="mark_attempting")
    def mark_attempting(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            c.execute(text("UPDATE billing.debits SET status='attempting', updated_at=:n WHERE tenant_id=:t "
                           "AND debit_id=:d AND status IN ('scheduled','notified')"),
                      {"n": self._now(step), "t": c_.tenant_id, "d": c_.debit_id})
        return {"status": "attempting"}

    @activity.defn(name="reconcile")
    def reconcile(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        provider = provider_for(self.d.provider, c_.tenant_id)
        report = reconcile_open_debits(self.d.engine, provider, c_.tenant_id, stale_after=timedelta(0),
                                       clock=FixedClock(self._now(step)))
        links = reconcile_payment_requests(self.d.engine, provider, c_.tenant_id, debit_id=c_.debit_id,
                                           clock=FixedClock(self._now(step)))
        report.scanned += links.scanned
        report.changed += links.changed
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            status: str = c.execute(text("SELECT status FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                               {"t": c_.tenant_id, "d": c_.debit_id}).scalar_one()
        event = {"event_type": "payment.captured"} if status == "succeeded" else None
        return {"scanned": report.scanned, "changed": report.changed, "debit_status": status, "payment_event": event}

    @activity.defn(name="collect")
    def collect(self, step: StepInput) -> dict[str, Any]:
        """Due date. Provider- and mandate-collected debits: the provider charges, nothing to do here. Pay-by-link:
        send the payment request through the gateway (policy: consent, contact window, fatigue)."""
        c_ = step.cycle
        method = self._method(c_.tenant_id, c_.debit_id)
        if method != "payment_link":
            return {"method": method, "status": "provider_charges"}
        r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="billing_agent",
                                     tool_name="billing.send_payment_request", args={"debit_id": c_.debit_id},
                                     case_id=step.case_id)
        return {"method": method, "status": r.status, "action_id": r.action_id, "url": r.output.get("url"),
                "sent": r.output.get("sent"), "channel_error": r.output.get("channel_error"),
                "error": r.error or r.output.get("error"),
                "retry_after": r.decision.retry_after.isoformat() if r.decision and r.decision.retry_after else None}

    @activity.defn(name="poll_link")
    def poll_link(self, step: StepInput) -> dict[str, Any]:
        """No webhook yet (or a missed one): ask the provider whether this debit's payment link was paid."""
        c_ = step.cycle
        report = reconcile_payment_requests(self.d.engine, provider_for(self.d.provider, c_.tenant_id), c_.tenant_id,
                                            debit_id=c_.debit_id, clock=FixedClock(self._now(step)))
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            status: str = c.execute(text("SELECT status FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                                    {"t": c_.tenant_id, "d": c_.debit_id}).scalar_one()
        event = {"event_type": "payment.captured"} if status == "succeeded" else None
        return {"scanned": report.scanned, "errors": report.errors[:3], "debit_status": status,
                "payment_event": event}

    @activity.defn(name="handle_failure")
    def handle_failure(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            d = c.execute(text("SELECT amount_minor, currency FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                          {"t": c_.tenant_id, "d": c_.debit_id}).one()
            rt = tenant_runtime.load(c, c_.tenant_id)
            last = c.execute(text("SELECT method, issuer, coalesce(provider_created_at, created_at) AS at FROM "
                                  "billing.payments WHERE tenant_id=:t AND debit_id=:d AND status='failed' ORDER BY "
                                  "coalesce(provider_created_at, created_at) DESC LIMIT 1"),
                             {"t": c_.tenant_id, "d": c_.debit_id}).first()
            from nirantar.health.monitor import degraded_for

            incident = degraded_for(c, last.method, last.issuer, last.at) if last else None
        state = handle_failure(
            ConductorDeps(self._gateway(step), self.d.llm, rt.capacity, rt.priors, config_sources=rt.sources,
                          bank_degraded=incident is not None),
            c_.tenant_id, step.case_id,
            FailedDebit(c_.debit_id, c_.customer_id, d.amount_minor, d.currency, step.payload.get("error_code"),
                        step.payload.get("error_reason"), attempt=int(step.payload.get("attempt", 1))))
        return {"chosen_arm": state.get("chosen_arm"), "retry_after": state.get("retry_after"), "incident": incident,
                "triage": state.get("triage"), "exposure_arm": state.get("exposure_arm"),
                "actions": state.get("action_results", []), "trace": state.get("trace", [])}

    @activity.defn(name="record_reply")
    def record_reply(self, step: StepInput) -> dict[str, Any]:
        from nirantar.agents.promise import extract_promise_date

        c_ = step.cycle
        said = str(step.payload.get("text", ""))
        intent = classify_reply(said)
        promised = step.payload.get("promised_date")
        if intent == "promise_to_pay" and not promised:
            found = extract_promise_date(said, self._now(step).date())
            promised = found.isoformat() if found else None
        r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="conductor", tool_name="case.record_reply",
                                     args={"customer_id": c_.customer_id, "debit_id": c_.debit_id, "intent": intent,
                                           "promised_date": promised, "quote": said[:300] or None,
                                           "source": "voice_note" if step.payload.get("via") == "audio"
                                           else "whatsapp_text"},
                                     case_id=step.case_id,
                                     idempotency_key=f"reply:{c_.debit_id}:{step.payload.get('message_id')}")
        ack = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="conductor",
                                       tool_name="comms.acknowledge_reply",
                                       args={"customer_id": c_.customer_id, "intent": intent,
                                             "promised_date": promised,
                                             "via": step.payload.get("via")},
                                       case_id=step.case_id,
                                       idempotency_key=f"ack:{c_.debit_id}:{step.payload.get('message_id')}")
        return {"intent": intent, "status": r.status, "memory_id": r.output.get("memory_id"),
                "promise_id": r.output.get("promise_id"),
                "promised_date": promised if intent == "promise_to_pay" else None,
                "acknowledged": ack.status, "ack_error": ack.error or ack.output.get("error")}

    @activity.defn(name="promise_remind")
    def promise_remind(self, step: StepInput) -> dict[str, Any]:
        """The promised day: the customer's own words come back as a reminder with a payment link."""
        c_ = step.cycle
        r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="billing_agent",
                                     tool_name="billing.send_payment_request",
                                     args={"debit_id": c_.debit_id, "occasion": "promise"}, case_id=step.case_id,
                                     idempotency_key=f"promise-remind:{step.payload.get('promised_date')}:{c_.debit_id}")
        if r.status == "executed":
            with tenant_tx(c_.tenant_id, self.d.engine) as c:
                c.execute(text("UPDATE ops.promises SET reminder_sent_at=:n WHERE tenant_id=:t AND debit_id=:d AND "
                               "status='open'"), {"n": self._now(step), "t": c_.tenant_id, "d": c_.debit_id})
        return {"status": r.status, "sent": r.output.get("sent"), "url": r.output.get("url"),
                "error": r.error or r.output.get("error")}

    @activity.defn(name="promise_resolve")
    def promise_resolve(self, step: StepInput) -> dict[str, Any]:
        """Kept = the provider-verified payment arrived by the end of the promised day; otherwise broken."""
        c_ = step.cycle
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            paid: bool = c.execute(text("SELECT status FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
                             {"t": c_.tenant_id, "d": c_.debit_id}).scalar_one() == "succeeded"
            n = c.execute(text("UPDATE ops.promises SET status=:s, resolved_at=:n WHERE tenant_id=:t AND "
                               "debit_id=:d AND status='open'"),
                          {"s": "kept" if paid else "broken", "n": self._now(step), "t": c_.tenant_id,
                           "d": c_.debit_id}).rowcount
        return {"kept": paid, "resolved": n}

    @activity.defn(name="verify_and_close")
    def verify_and_close(self, step: StepInput) -> dict[str, Any]:
        c_ = step.cycle
        expect = str(step.payload["expect"])
        verified, value = True, 0
        evidence: dict[str, Any] = {}
        if expect in ("paid_on_time", "recovered"):
            r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="verifier",
                                         tool_name="ledger.verify_credit", args={"debit_id": c_.debit_id},
                                         case_id=step.case_id)
            evidence = r.output
            verified = bool(r.output.get("verified"))
        with tenant_tx(c_.tenant_id, self.d.engine) as c:
            debit = c.execute(text("SELECT status, amount_minor FROM billing.debits "
                                   "WHERE tenant_id=:t AND debit_id=:d"),
                              {"t": c_.tenant_id, "d": c_.debit_id}).one()
            if expect == "unrecovered":
                verified = debit.status != "succeeded"      # provider/ledger agree nothing arrived
            outcome = expect if verified else "unverified"
            value = debit.amount_minor if outcome == "recovered" else 0
            closed = close_cycle(c, c_.tenant_id, c_.debit_id, c_.customer_id, outcome, verified, value,
                                 step.payload.get("prediction_id"), c_.experiment_id, step.case_id, self._now(step))
        receipt = None
        if closed.verified and closed.outcome in ("paid_on_time", "recovered") and \
                self._method(c_.tenant_id, c_.debit_id) == "payment_link":
            from nirantar.comms.sink import ChannelNotConnected

            try:
                r = self._gateway(step).call(tenant_id=c_.tenant_id, agent_id="billing_agent",
                                             tool_name="comms.send_receipt", args={"debit_id": c_.debit_id},
                                             case_id=step.case_id, idempotency_key=f"receipt:{c_.debit_id}")
                receipt = {"status": r.status, "error": r.error}
            except ChannelNotConnected as exc:          # the payment stands; the receipt can be re-sent later
                receipt = {"status": "not_sent", "error": str(exc)[:200]}
        live = evaluate_live(self.d.engine, c_.tenant_id)
        return {"outcome": closed.outcome, "verified": closed.verified, "label_ids": closed.label_ids,
                "receipt": receipt,
                "experiment_outcome_id": closed.outcome_id, "outcome_event_id": closed.event_id,
                "evidence": evidence, "live_eval": live}

    def all(self) -> list[Any]:
        return [self.open_case, self.predict_risk, self.pre_debit, self.mark_attempting, self.reconcile, self.collect,
                self.poll_link, self.handle_failure, self.record_reply, self.promise_remind, self.promise_resolve,
                self.verify_and_close]

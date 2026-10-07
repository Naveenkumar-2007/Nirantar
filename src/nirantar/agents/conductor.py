"""Conductor (supervisor) — LangGraph state machine for one failed debit (BB-§15).

Nodes: triage → context → eligibility → assignment → arbitrate → act → exposure.
The Conductor holds no business logic of its own: each node delegates to a specialist
(Failure Triage, Compliance Guardian via policy.check_action, experiment service, Contact Arbiter,
Conversation Agent). Every side effect goes through the MCP ToolGateway. Every node appends to
`trace`, which becomes the audit/acceptance evidence.
Durability across days (waiting for the customer to pay) is Temporal's job, not LangGraph's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from nirantar.agents import conversation, failure_triage
from nirantar.agents.contact_arbiter import Candidate, EffectPriors, arbitrate
from nirantar.core.money import Money
from nirantar.llm.gateway import LLMGateway
from nirantar.mcp.gateway import ToolGateway


@dataclass(frozen=True)
class FailedDebit:
    debit_id: str
    customer_id: str
    amount_minor: int
    currency: str
    error_code: str | None
    error_reason: str | None
    attempt: int = 1
    plan_name: str = "your subscription"


@dataclass
class ConductorDeps:
    gateway: ToolGateway
    llm: LLMGateway | None
    capacity: dict[str, int]                 # tenant channel capacity for this round (settings: channels)
    priors: EffectPriors                     # tenant effects (learned or configured) + channel costs
    bank_degraded: bool = False
    config_sources: dict[str, str] = field(default_factory=dict)   # where capacity/effects came from (traced)


class State(TypedDict, total=False):
    tenant_id: str
    case_id: str
    debit: FailedDebit
    triage: dict[str, Any]
    profile: dict[str, Any]
    allowed_arms: list[str]
    denials: dict[str, Any]
    retry_after: str | None
    assigned_arm: str
    chosen_arm: str | None
    action_results: list[dict[str, Any]]
    exposure_arm: str
    trace: list[dict[str, Any]]


def _log(state: State, step: str, **data: Any) -> list[dict[str, Any]]:
    return [*state.get("trace", []), {"step": step, **data}]


def build(deps: ConductorDeps) -> Any:
    gw = deps.gateway

    def call(state: State, agent: str, tool: str, args: dict[str, Any]) -> Any:
        return gw.call(tenant_id=state["tenant_id"], agent_id=agent, tool_name=tool, args=args,
                       case_id=state["case_id"])

    def triage_node(state: State) -> State:
        d = state["debit"]
        out = failure_triage.triage(failure_triage.TriageIn(error_code=d.error_code, error_reason=d.error_reason,
                                                            bank_degraded=deps.bank_degraded), deps.llm)
        return {"triage": out.model_dump(), "trace": _log(state, "triage", **out.model_dump())}

    def context_node(state: State) -> State:
        r = call(state, "conductor", "customer.get_profile", {"customer_id": state["debit"].customer_id})
        return {"profile": r.output, "trace": _log(state, "context", action_id=r.action_id,
                                                   language=r.output.get("language"))}

    def eligibility_node(state: State) -> State:
        allowed, denials, retry = [], {}, []
        if state["triage"]["customer_contact_recommended"]:
            for arm, kind in (("whatsapp", "send_whatsapp"), ("voice", "voice_call")):
                r = call(state, "conductor", "policy.check_action",
                         {"customer_id": state["debit"].customer_id, "action_kind": kind, "purpose": "recovery"})
                if r.output.get("outcome") == "ALLOW":
                    allowed.append(arm)
                else:
                    denials[arm] = r.output.get("policy_ids")
                    if r.output.get("retry_after"):
                        retry.append(r.output["retry_after"])
        # If nothing is allowed *now* but a window opens later, tell the workflow when to try again.
        retry_after = min(retry) if (retry and not allowed) else None
        return {"allowed_arms": allowed, "denials": denials, "retry_after": retry_after,
                "trace": _log(state, "eligibility", allowed=allowed, denied=denials, retry_after=retry_after)}

    def assignment_node(state: State) -> State:
        r = call(state, "conductor", "experiment.assign_treatment", {"customer_id": state["debit"].customer_id})
        return {"assigned_arm": r.output["arm"], "trace": _log(state, "assignment", arm=r.output["arm"],
                                                               experiment_id=r.output["experiment_id"])}

    def arbitrate_node(state: State) -> State:
        d = state["debit"]
        cand = Candidate(state["case_id"], d.customer_id, d.amount_minor, state["triage"]["category"],
                         frozenset(state.get("allowed_arms", [])), in_holdout=state["assigned_arm"] == "holdout")
        a = arbitrate([cand], deps.capacity, deps.priors)[0]
        return {"chosen_arm": a.arm, "trace": _log(state, "arbitrate", arm=a.arm, value_minor=a.expected_value_minor,
                                                   reason=a.reason, config=deps.config_sources)}

    def act_node(state: State) -> State:
        d = state["debit"]
        results: list[dict[str, Any]] = []
        if state.get("chosen_arm") == "whatsapp":
            link = call(state, "conversation_agent", "gateway.create_payment_link",
                        {"debit_id": d.debit_id, "attempt": d.attempt})
            results.append({"tool": "gateway.create_payment_link", "status": link.status,
                            "action_id": link.action_id, "provider_ref": link.output.get("provider_ref")})
            profile = state["profile"]
            tpl = call(state, "conversation_agent", "content.get_template",
                       {"key": "whatsapp.recovery", "language": profile.get("language", "en")})
            if link.status == "executed" and tpl.status == "executed":
                draft = conversation.draft(conversation.DraftIn(
                    customer_name=profile.get("first_name", "there"), language=profile.get("language", "en"),
                    amount=Money(d.amount_minor, d.currency), plan_name=d.plan_name,
                    failure_category=state["triage"]["category"], template=tpl.output["body"],
                    template_ref=tpl.output["ref"], template_language=tpl.output["language"]), deps.llm)
                text = draft.text_with_placeholder.replace("{link}", link.output["url"])
                msg = call(state, "conversation_agent", "comms.send_whatsapp",
                           {"customer_id": d.customer_id, "debit_id": d.debit_id, "text": text})
                results.append({"tool": "comms.send_whatsapp", "status": msg.status, "action_id": msg.action_id,
                                "provider_ref": msg.output.get("provider_ref"), "draft_source": draft.source,
                                "template_ref": draft.template_ref,
                                "language": draft.language,
                                "denied_by": [h.policy_id for h in msg.decision.hits] if msg.decision else []})
        elif state.get("chosen_arm") == "voice":
            placed = call(state, "conversation_agent", "comms.place_call",
                          {"customer_id": d.customer_id, "debit_id": d.debit_id})
            results.append({"tool": "comms.place_call", "status": placed.status, "action_id": placed.action_id,
                            "provider_ref": placed.output.get("provider_ref"), "error": placed.error,
                            "denied_by": [h.policy_id for h in placed.decision.hits] if placed.decision else []})
        return {"action_results": results, "trace": _log(state, "act", results=results)}

    def exposure_node(state: State) -> State:
        executed = [r for r in state.get("action_results", []) if r["status"] == "executed"]
        if state.get("retry_after") and not executed and state["assigned_arm"] != "holdout":
            # Deferred round (every channel closed right now): not a treatment decision, so no exposure.
            # Logging "none" here would count this customer as untreated AND treated after the retry.
            return {"exposure_arm": "deferred", "trace": _log(state, "exposure", arm="deferred",
                                                              retry_after=state["retry_after"])}
        arm = "holdout" if state["assigned_arm"] == "holdout" else (state.get("chosen_arm") if executed else "none")
        ref = executed[-1]["action_id"] if executed else None
        r = call(state, "conductor", "experiment.log_exposure",
                 {"customer_id": state["debit"].customer_id, "arm": arm or "none", "action_ref": ref})
        return {"exposure_arm": arm or "none", "trace": _log(state, "exposure", arm=arm, exposure=r.output)}

    g = StateGraph(State)
    for name, fn in (("triage", triage_node), ("context", context_node), ("eligibility", eligibility_node),
                     ("assignment", assignment_node), ("arbitrate", arbitrate_node), ("act", act_node),
                     ("exposure", exposure_node)):
        g.add_node(name, fn)
    g.set_entry_point("triage")
    for a, b in (("triage", "context"), ("context", "eligibility"), ("eligibility", "assignment"),
                 ("assignment", "arbitrate"), ("arbitrate", "act"), ("act", "exposure")):
        g.add_edge(a, b)
    g.add_edge("exposure", END)
    return g.compile()


def handle_failure(deps: ConductorDeps, tenant_id: str, case_id: str, debit: FailedDebit) -> State:
    graph = build(deps)
    result: State = graph.invoke({"tenant_id": tenant_id, "case_id": case_id, "debit": debit, "trace": []})
    return result

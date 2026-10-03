"""Agent specifications (BB-§14). One record per agent: contract, tools, model tier, limits, fallback.

Status is explicit: `implemented` agents have code + tests; `planned` agents are specified here so
their contracts are reviewed before code exists (no fake completion, BB-§49).
"""

from __future__ import annotations

from dataclasses import dataclass

from nirantar.mcp.tools import AGENT_SCOPES


@dataclass(frozen=True)
class AgentSpec:
    name: str
    job: str
    input_schema: str
    output_schema: str
    allowed_tools: frozenset[str]
    forbidden: tuple[str, ...]
    llm_tier: str | None             # None = deterministic
    timeout_s: float
    retries: int
    fallback: str
    escalates_when: str
    status: str                      # implemented | planned


SPECS: dict[str, AgentSpec] = {s.name: s for s in (
    AgentSpec("conductor", "Owns the per-debit plan; sequences specialists; never does specialist reasoning",
              "ConductorState", "FailureStepResult", AGENT_SCOPES["conductor"],
              ("money movement", "direct DB/provider access"), None, 30, 0,
              "no optional contact; mandatory notices only", "any specialist fails twice", "implemented"),
    AgentSpec("failure_triage", "Classify a decline and decide retry vs contact", "TriageIn", "TriageOut",
              frozenset(), ("any write tool",), "fast", 10, 1, "rule table; UNKNOWN → no contact, human review",
              "unknown decline code with high amount", "implemented"),
    AgentSpec("debit_strategist", "Pre-debit plan from M1 risk (notice content, optional nudge, debit shift)",
              "StrategyIn", "StrategyOut", AGENT_SCOPES["debit_strategist"], ("choosing dates or amounts",),
              None, 5, 0, "mandatory notice only", "M1 unavailable", "implemented"),
    AgentSpec("contact_arbiter", "Deterministic OR-Tools optimiser over eligible arms and capacity",
              "Candidate[]", "Assignment[]", frozenset(), ("LLM use",), None, 5, 0, "no optional contact",
              "solver infeasible", "implemented"),
    AgentSpec("revival_agent", "Win back churned subscribers: randomised offer arm vs holdout, offer + link, "
              "promotional message; outcome verified by the provider", "RevivalInput", "RevivalOutcome",
              AGENT_SCOPES["revival_agent"], ("amounts from the caller", "offers outside the tenant's grid",
                                               "messages without promotional consent"), None, 20, 2,
              "no message (case closed as withheld)", "discount above threshold -> human approval", "implemented"),
    AgentSpec("conversation_agent", "Draft and send a compliant message in the customer's language",
              "DraftIn", "DraftOut", AGENT_SCOPES["conversation_agent"],
              ("offers outside grid", "amounts not from the debit"), "indic", 15, 1,
              "approved static template per language", "distress/complaint detected", "implemented"),
    AgentSpec("compliance_guardian", "Pre-flight gate on every outbound action (policy engine)", "ActionRequest",
              "Decision", frozenset(), ("being bypassed",), None, 1, 0, "DENY", "every DENY is logged",
              "implemented"),
    AgentSpec("verifier", "Deterministic verification against provider/ledger; writes labels", "DebitRef",
              "Verification", AGENT_SCOPES["verifier"], ("trusting agent output",), None, 10, 3,
              "mark unverified, schedule reconciliation", "mismatch", "implemented"),
    AgentSpec("mandate_doctor", "Mandate health and repair; repairs via MandateRepairWorkflow", "MandateIn",
              "MandatePlan", AGENT_SCOPES["mandate_doctor"], ("re-registering a mandate itself",), "fast", 10, 1,
              "re-auth link template", "fraud revocation", "implemented"),
    AgentSpec("voice_agent", "Sarvam voice calls: reminders, PTP, grid-bound negotiation", "CallTask",
              "CallOutcome", frozenset(), ("collecting OTP/PIN",), "fast", 1.5, 0, "transfer to human",
              "distress, hardship, complaint", "planned"),
    AgentSpec("collections_strategist", "DPD plans, legal-notice drafts, agency handoff", "LoanCase",
              "CollectionsPlan", frozenset(), ("third-party contact",), "mid", 20, 1, "human queue",
              "any legal step", "planned"),
    AgentSpec("dispute_defender", "Evidence packs and representment drafts; contests via DisputeWorkflow",
              "DisputeIn", "DisputeOut", AGENT_SCOPES["dispute_defender"],
              ("submitting above threshold without approval", "inventing evidence"), "strong", 60, 1, "human queue",
              "low win probability or deadline too close", "implemented"),
    AgentSpec("treasury_agent", "Forecast → cash plan (approval for any money movement)", "TreasuryIn",
              "TreasuryPlan", frozenset(), ("moving money without approval",), "mid", 30, 1, "alert only",
              "always for money movement", "planned"),
)}

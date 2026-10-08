# ADR-0030: Agent architecture (supervisor) and evaluation gates

Status: accepted (2026-10-08)

## Context

The brief asks for a multi-agent system with a supervisor, plus evaluations that fail a deployment when quality drops.
Nirantar already had specialist agents with scoped tools. Two things were missing: a written answer to "who
supervises", and a measured, enforced quality bar.

## Decision

### 1. The supervisor is the durable workflow, not another LLM

- **Routing.** For each subject (a debit, an invoice, a checkout, a mandate repair, a dispute, a recovery batch), the
  event bridge routes the event to exactly one durable Temporal workflow.
  - That workflow is the supervisor for its subject.
  - It decides which specialist acts next, and it keeps the state.
  - It enforces loop bounds: contact rounds, the retry cap, follow-up limits and expiry.
  - It survives restarts and replays deterministically.
- **The per-debit Conductor** (LangGraph) sequences the specialists that need reasoning: triage, context,
  eligibility, holdout assignment, the contact arbiter, and the conversation draft.
- **Specialists:**

  | Area | Agents |
  |---|---|
  | Diagnosis | `failure_triage`, checkout diagnosis, the mandate retry planner |
  | Strategy | `debit_strategist`, `contact_arbiter`, the `retry_sequencer` planner |
  | Channels | `conversation_agent` (WhatsApp), `voice_agent` |
  | Domains | `receivables_agent`, promise handling, `checkout_agent`, `revival_agent`, `mandate_doctor`, `dispute_defender`, `treasury_agent` |
  | Measurement | experiments and verified outcomes |
  | Compliance | `compliance_guardian` (the policy engine) |

- **Each agent acts only through the MCP ToolGateway with its own scope** (`AGENT_SCOPES`):
  - scope → schema → dedupe/lease → policy → approval → execute → audit;
  - a reviewed `AgentSpec` (job, tools, forbidden actions, timeout, fallback, escalation) is required for every
    scoped agent;
  - human approval is an approval state of the action, not a separate path.
- **Decision traces stored** are operational only: agent, tool, parameters hash, policy decision and hits, approval,
  result, timing (`ops.actions`, the audit chain, workflow traces). No private chain-of-thought is stored or shown.
- **Rejected:** a free-form LLM "supervisor" that picks tools. It would add a component that can loop, invent steps
  or be prompt-injected, in front of decisions that are already deterministic and auditable.

### 2. Evaluations are datasets with gates, run on every change

- `nirantar/evals/agents.py` scores held-out, labelled datasets (`evals/agents/*.yaml`) and live registries:

  | Area | What is measured |
  |---|---|
  | Routing | decline triage accuracy; checkout cause diagnosis accuracy |
  | Money decisions | retry or not, for every case at every hour; the ≥24h notice; the morning window |
  | Policy | consent, opt-out, window, fatigue, promotional, voice header, lending hours, mandatory notices |
  | Tools | scopes reference real tools; specs cover every scoped agent; out-of-scope calls are refused |
  | Security | prompt-injection recall and false-positive rate on new phrasings (EN, Hinglish, HI, TE, hidden characters); PII redaction recall |
  | Conduct | threatening wording blocked, courteous wording passes |
  | Multilingual | every customer template exists in en/hi/te, with identical placeholders, and passes the conduct and script checks |
  | Attribution | the uplift estimator recovers a known +10pp effect (CI coverage, error) |

- `evals/agents/thresholds.yaml` sets the gates. Critical gates — money, contact, data protection, conduct and
  tools — are 1.0 (or 0.0 for error rates).
- `tests/evals` runs the gates in CI, so a regression fails the build and blocks the deploy. The report is written
  to `evals/results/agents_latest.json`.

## Consequences

- The first run found real gaps, all fixed:
  - an OTP inside a sentence was not redacted;
  - a "defaulter / everyone will know" shaming message was not blocked;
  - six injection phrasings were missed;
  - six agents had no spec;
  - the SMS pre-debit notice existed only in English.
- Duplicate-action resistance, stopping rules, voice outcomes and provider-verified recovery are already proven
  end to end against real Postgres and Temporal (`tests/e2e`). Those run in the same CI job.
- LLM drafting quality on live models is measured separately (`--live-llm`, sandbox); it never gates money paths.

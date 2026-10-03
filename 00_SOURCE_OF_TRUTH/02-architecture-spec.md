# Nirantar — Architecture Specification (baseline v1.0, 2026-09-28)

Model IDs, provider capabilities and regulatory numbers below are **intent**. Verified values live in `docs/integrations/` and `docs/compliance/`.

## 1. System shape

```
Provider webhooks / mandate events / LMS / comms replies / call events / AA statements (consented)
   → Webhook ingress (verify → persist raw → idempotency → normalise)
   → Redpanda topics (payment.*, mandate.*, subscription.*, dispute.*, reply.*, call.*, bank.health, outcome.*, experiment.*, compliance.*)
   → Stream features → Feature store (online Redis / offline Parquet+ClickHouse)
   → Model serving (M1–M12)
   → Temporal workflows (durable) → LangGraph agent steps
   → Compliance Guardian → Approval (when required) → MCP Gateway → MCP servers → providers
   → Verifier → Ledger → outcome events → labels → retraining
Stores: Postgres (+pgvector) · Redis · ClickHouse · MinIO (S3)
```

## 2. Temporal workflows

- `CustomerLifecycleWorkflow` — long-lived entity workflow (continue-as-new). Children:
  - `DebitCycleWorkflow` — T-3 … T+15 around each scheduled debit
  - `CollectionsWorkflow` — DPD 1 → 90, contact-window timers
  - `DisputeWorkflow` — deadline timers
  - `RevivalWorkflow`
- `TreasuryWorkflow` — hourly
- `RetrainSchedule` — nightly
- Signals: `payment.success`, `payment.failed`, `reply.received`, `call.ended`, `approval.granted`, `approval.denied`.
- Short tasks (decline triage, message drafting, a single call turn) run as activities, never as long workflows.

## 3. Agents (12)

| Agent | Job | Main tools | LLM tier | Escalates when |
|---|---|---|---|---|
| Conductor | Owns the account plan; sequences specialists | customer, policy, experiment | mid | Conflicting goals |
| Mandate Doctor | Mandate health and fixes | mandate, gateway | fast | Mandate revoked for fraud |
| Debit Strategist | Timing, instrument, routing | gateway, mandate + M1/M3 | fast | Rule conflict |
| Failure Triage | Decline codes, bank/gateway health | gateway, bank-health + M4 | fast | Unknown pattern |
| Contact Arbiter | Who/when/channel under fatigue budget — **deterministic OR-Tools optimiser**, LLM only explains | experiment, customer, policy | none/fast | — |
| Conversation Agent | WhatsApp/SMS/email | comms, gateway | mid | Distress, wrong person, complaint |
| Voice Agent | Calls: reminders, objections, promise-to-pay, grid-bound negotiation | voice, lending, gateway | fast + Sarvam STT/TTS | Hardship/anger → human |
| Collections Strategist | DPD plans, legal-notice drafts, agency handoff | lending, a2a | mid | Any legal step |
| Dispute Defender | Evidence and representment drafts | dispute, ledger | strong | Above ₹X or low win probability |
| Treasury Agent | Forecast → cash plan | treasury, ledger + M10 | mid | Always for money movement |
| Compliance Guardian | Pre-flight gate on every outbound action; can veto | policy | rules + fast judge | Every veto logged |
| Verifier | Confirms real-world state; writes labels | ledger, gateway | deterministic | Mismatch → reopen |

Model tiers (intent, configurable): strong = Claude Opus 5.5, mid = Claude Sonnet 5, fast = Claude Haiku 4.5; Indic/sovereign fallback = Sarvam chat model; low-latency alternative = Groq-hosted open model.

## 4. ML model zoo (12)

| # | Model | Label | Baseline → Advanced |
|---|---|---|---|
| M1 | Debit Failure Predictor (T-3) | debit success | LightGBM → LightGBM + Temporal Fusion Transformer |
| M2 | Cash-Window Estimator | payment-success day | circular stats → Bayesian periodicity |
| M3 | Retry Timing | success by hour after failure | DeepHit survival + constrained Thompson bandit |
| M4 | Bank/Gateway Health | technical-decline incident | z-score → Bayesian online changepoint detection |
| M5 | Multi-treatment Uplift (none/WhatsApp/voice/grid offer) | incremental payment | T-learner → DragonNet + causal forest (EconML) |
| M6 | Churn/Lapse Hazard | time to cancel | Cox → DeepSurv |
| M7 | Promise-to-Pay Reliability | PTP kept | LR → GBDT |
| M8 | Intent & Objection (Telugu/Hindi/English code-mixed) | intent | TF-IDF → fine-tuned MuRIL/IndicBERT |
| M9 | Roll-Rate (lending) | DPD transition | Markov → GBDT hazard |
| M10 | Cash Forecaster | daily collections | Σ M1 probabilities → + N-BEATS residual + conformal intervals |
| M11 | Dispute Win Probability | won/lost | LR → GBDT |
| M12 | Conduct Judge | policy violation | rules → small classifier + LLM judge |

Contact Arbiter objective: maximise Σ uplift × value subject to consent, contact windows, fatigue budget, tenant policy. OR-Tools.

Randomized holdout: 5–10 % per tenant, configurable.

## 5. Data ladder

1. Public research data (research-only licences): KKBox churn, Home Credit installments, Criteo Uplift (CC BY-NC-SA), Hillstrom; NPCI public aggregates as priors.
2. Sandbox: Razorpay / Cashfree / Stripe test modes; Setu AA sandbox; telephony trial.
3. RecurSim: agent-based simulator with salary cycles, bank outages, decline codes, heterogeneous treatment effects and **true counterfactuals**.
4. Design partner: read-only keys, shadow mode.
5. Proprietary intervention → outcome logs.

Synthetic results are never reported as production results.

## 6. GenAI, RAG, memory

- LLM gateway with routing by task, latency, cost, language, capability, reliability.
- RAG corpus: RBI e-mandate framework, Digital Lending Directions, recovery conduct rules, NPCI AutoPay/NACH guidelines, card dispute reason codes, tenant T&Cs/refund policy, loan agreements, DPDP.
- Pipeline: layout-aware parse → section-aware chunk → embed → pgvector + BM25 → hybrid → rerank → metadata filter (tenant, rail, document_type, regulation, effective_date, jurisdiction, version) → citations → citation checker (fails closed for compliance answers).
- Memory types: customer, merchant, procedural, episodic case. Writes only through `customer-mcp.update_memory`. Each record carries source, timestamp, confidence, provenance, tenant, consent applicability.

## 7. MCP servers (12) — only action boundary

✋ = requires approval token above tenant threshold.

| Server | Tools |
|---|---|
| gateway-mcp | fetch_payment, list_subscriptions, create_payment_link, charge_subscription ✋, pause_subscription ✋, create_refund ✋ |
| mandate-mcp | get_mandate_status, send_reauth_link, schedule_execution, get_predebit_status, switch_instrument ✋ |
| bank-health-mcp | get_bank_success_rate, get_incidents, is_bank_degraded, forecast_recovery |
| customer-mcp | get_profile, get_memory, update_memory, get_consents, set_opt_out |
| comms-mcp | send_whatsapp_template, send_dlt_sms, send_email, get_replies, schedule_message |
| voice-mcp | start_call, transfer_to_human, get_transcript, get_call_outcome, send_link_during_call |
| lending-mcp | get_loan, get_dpd, get_restructure_grid, record_ptp, draft_legal_notice ✋ |
| dispute-mcp | list_disputes, build_evidence_bundle, submit_representment ✋, accept_dispute ✋ |
| treasury-mcp | get_balances, get_forecast, list_payouts, reschedule_payout ✋, request_credit_draw ✋ |
| policy-mcp | search_policy, check_action, get_contact_window, get_offer_grid |
| experiment-mcp | assign_treatment, log_exposure, get_incremental_revenue |
| ledger-mcp | verify_credit, mark_outcome |

MCP Gateway: tenant auth, per-agent scopes, OPA-style policy check, rate limits, approval-token check, audit.

## 8. A2A

- Agent Card: name, organisation, skills, endpoint, authentication, signing key.
- Lifecycle: submitted → working → input-required → completed / failed / canceled. All messages signed.
- Flow A: Lender ↔ external collection agency (task out with DPD, allowed hours, grid; contact attempts streamed back → RBI contact log).
- Flow B: Merchant ↔ customer's personal AI agent (pause / date change requests, verified card + consent token, policy check).
- Flow C: Internal Conductor ↔ Voice Agent service.
- MCP for tools; A2A for agent-to-agent tasks.

## 9. Multimodal

Inputs: call audio, WhatsApp text and voice notes, payment screenshots, loan PDFs, KYC/mandate documents, AA JSON, emails.
Processors: Sarvam STT (code-mix), language ID, OCR + layout parser, vision extraction, screenshot forgery detector + UTR/amount cross-check via provider, deterministic AA parser, M8.
Unified Evidence object: case_id, customer_id, modality, source_uri, extracted_fields, language, confidence, verified_against, hash, created_at. Artefacts in MinIO with SHA-256.

## 10. Voice (Sarvam)

Exotel → media stream (WebSocket) → VAD + barge-in → language ID → Sarvam streaming STT (code-mix) → turn manager → Voice Agent (fast LLM + MCP tools) → Compliance check → Sarvam TTS → caller. Design target: < 1.5 s turn latency (to be measured). Human transfer on distress/hardship/complaint/wrong person. Post-call: transcript → M8 → M7 → case update → `call.ended` signal → follow-up → Verifier → label. Recording only with consent; never collect OTP/PIN.

## 11. Governance

| AI automatic | AI prepares, human approves | Never |
|---|---|---|
| Retries within rail caps; scheduling inside permitted window; mandatory pre-debit notices; templated messages inside consent and contact hours; payment/re-auth links; evidence packs; routing away from degraded banks | Representment above ₹X; restructuring outside band; refunds; payout reschedule; credit draw; legal notices | Discounts outside grid; third-party contact; lending contact outside permitted hours; contacting opted-out customers; false urgency/threats; debit without valid mandate and notice; changing amounts |

Kill switch per tenant and per agent. Hash-chained audit of prompt, model version, retrieved documents, policy decision, tool request, approval, provider response, verification, outcome.

## 12. The 8 diagrams (to be placed in `diagrams/` after approval)

01-overview · 02-aiml-models · 03-genai-rag-memory · 04-agentic-multiagent-orchestration · 05-multimodal · 06-mcp-servers · 07-a2a · 08-sarvam-voice-ai. Their full rendering spec is the "master prompt" in the product-discovery conversation; this document is the textual equivalent and wins if they differ.

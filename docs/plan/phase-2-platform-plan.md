# Nirantar Phase 2 — from engineering prototype to a real, open-source B2B platform

Date: 2026-09-28 · Status: in progress — P1 done (ADR-0011, 2026-09-29), P2 done (ADR-0012, 2026-09-29), P3 done for M1 (ADR-0013, 2026-09-29), P4 done (ADR-0014, 2026-10-01) · Deployment is explicitly out of scope until P8.

---

## 1. Honest status audit (what exists today vs an enterprise target)

Percentages are engineering judgement against the target in §2, not a measurement.

| Area | What really exists | What is missing | ~% |
|---|---|---|---|
| Core platform (DB, tenancy, ledger, audit, auth) | Postgres + RLS on every table, encrypted PII, double-entry ledger, hash-chained audit, API keys + RBAC, outbox → Redpanda | End-user login (OIDC), org/team management, secret manager, tenant settings stored in DB | 65 |
| Payment integrations | Razorpay adapter (tested live in test mode), Cashfree (fixtures only), mock provider, webhook ingress, reconciliation | OAuth "connect your account", webhook auto-registration, historical backfill, Stripe, provider health dashboards | 35 |
| **ML data pipelines** | **None for real data.** Models train on RecurSim generated in memory at run time. Feature code is pandas in-process; the live feature adapter approximates 4 features (training/serving skew) | Ingestion → bronze/silver/gold, data validation, feature store, point-in-time joins over real tenant history, scheduled per-tenant training, drift monitoring, shadow/canary, model serving service | 20 |
| ML models | M1 (debit failure), M2 (cash window estimator), M4 (bank health), M5 (uplift, shadow), M10 (cash forecast) | M3 retry timing, **M6 churn/lapse**, M7 promise-to-pay, M8 intent (regex only today), M9 roll-rate, M11 (prior only), M12 conduct (regex only), plus new models in §6. Public datasets are listed but never downloaded or used | 30 |
| GenAI | Provider-neutral LLM gateway (Groq, Sarvam), schema-validated outputs, fallbacks; hybrid RAG with citation checking | Prompt registry + versioning, LLM evals in CI, tracing (Langfuse), cost budgets, PDF document ingestion UI, guard model | 50 |
| Agents / multi-agent | Conductor (LangGraph) + triage, strategist, conversation, verifier wired into a workflow; mandate/dispute/treasury/collections exist as planners only | Those planners are not wired to workflows; no agent eval harness; no per-tenant automation settings | 40 |
| **MCP** | A policy-enforcing tool gateway in MCP *style* | **Not exposed over the MCP protocol.** The official SDK is installed but unused; no external client can connect today | 30 |
| A2A | Signed messages, agent cards, lifecycle, replay protection (library + tests) | No HTTP endpoints, not aligned to A2A **v1.0** (Mar 2026) or its `/.well-known/agent-card.json` | 30 |
| Orchestration | Temporal DebitCycleWorkflow (tested with time skipping), outbox relay entrypoint | No always-on worker/relay/bridge services; Collections, Dispute, Revival, MandateHealth, Treasury, Onboarding, Retrain workflows not built | 30 |
| Multimodal | Screenshot OCR + tamper rules, voice-note STT, AA salary parsing, S3 evidence store | Inbound WhatsApp media intake, PDF parsing (loan agreements, T&Cs), trained forgery model, vision model | 35 |
| Voice | Sarvam STT/TTS (live-tested), scripted turn engine, Exotel protocol gateway (simulator only) | Real telephony, streaming STT/TTS, outbound dialer, call analytics, human-agent desk; not triggered by workflows | 35 |
| Product / UX | Internal ops dashboard (10 pages) on real API data | Not a sellable B2B product: no website, sign-up, onboarding wizard, integrations, automation settings, customer 360, conversations, billing, developer portal, end-customer pages | 20 |
| Evaluation | ML gates, RAG recall/MRR, voice CER/intent, E2E acceptance trace | Agent trajectory evals, LLM evals, load/chaos tests, CI pipeline | 40 |

### Hard-coded values that must go (inventory)

| Where | Hard-coded today | Becomes |
|---|---|---|
| `agents/contact_arbiter.py` | Effect priors per failure code, costs ₹1/₹8 | Learned per tenant from holdout outcomes (Bayesian update from global prior); costs from tenant billing settings |
| `agents/debit_strategist.py` | `HIGH_RISK = 0.35` | Threshold chosen per tenant by cost-sensitive optimisation on validation data |
| `workflows/activities.py`, `demo/seed.py` | Capacity `{"voice": 0, "whatsapp": 1000}`, `TenantPolicyConfig()` default for every tenant | `tenant_settings` table (versioned, audited), edited in the Automations UI |
| `mcp/tools.py`, `voice/turn.py`, `agents/conversation.py` | Message templates, voice scripts | Template registry per tenant/language with compliance pre-checks and approval |
| `workflows/activities.py`, `voice/turn.py` | Reply intent regexes | M8 trained intent model (regex kept only as a fallback) |
| `policy/engine.py` | Conduct regex list | M12 conduct classifier + regex floor |
| `experiments/service.py` | Default holdout 8% | Tenant setting (within brief limits), power analysis recommends size |
| Everywhere | Model tier → model id map in code | Routing config in DB, per tenant overrides |

---

## 2. Definition of "complete working product" (acceptance criteria)

1. A business signs up on the website, creates an organisation, invites teammates with roles.
2. It connects Razorpay via **OAuth** (or pastes test keys), and Nirantar registers webhooks and **backfills 12 months** of subscriptions/payments automatically.
3. A data-health report appears; per-tenant models train on **its own history**; results are shown in **shadow mode** first (predictions with no actions) with measured accuracy.
4. The business turns on automations loop by loop, sets windows, capacity, templates, offer grid, and approval thresholds in the UI.
5. Everything then runs 24/7 as services: webhook ingress, relay, event→workflow bridge, Temporal workers, model server, voice gateway, retraining schedules.
6. Customers receive real WhatsApp messages (test number) and real phone calls (telephony trial), can reply, pay via a hosted page, and manage preferences.
7. Outcomes are verified, labelled, fed back; models retrain on schedule and on drift; the Experiments page shows incremental ₹ with confidence intervals.
8. External agents can connect over **MCP** (OAuth-scoped) and **A2A v1.0**.
9. Everything is open-source (Apache-2.0) and self-hostable with Docker Compose / Helm on free components.

"Real" means real integrations in their free sandboxes/trials. Real *merchant money* requires a design partner's live accounts — no code change, only credentials.

---

## 3. Product & platform design (a real B2B SaaS, not an ops console)

### Personas
Merchant admin (owner) · finance approver · ops/collections analyst · compliance officer · developer · end customer · Nirantar operator.

### Public website (marketing + trust)
Home (problem → how it works → proof), Solutions (subscriptions, lending EMIs, SIPs, insurance, B2B), How it works (the loop), Security & compliance (RLS, encryption, audit, DPDP, not-a-PA positioning), Pricing (open-source self-host free; managed tiers), Docs, Open-source repo, Changelog, Book a demo. Built with Next.js, statically rendered, fast, accessible, en/hi/te.

### App information architecture
| Section | Purpose |
|---|---|
| **Onboarding wizard** | Org → connect provider (OAuth) → verify webhooks → backfill progress → data health → models training → shadow report → enable automations |
| Home | Today: at-risk debits, recovered ₹, incremental ₹ vs holdout, approvals, alerts |
| Customers 360 | Per customer: subscriptions, mandates, payments, predictions (risk, churn), conversations, consent, memory |
| Debits & Cases | Lifecycle timelines, case inbox with filters, assignment |
| Conversations | WhatsApp threads + call recordings/transcripts, human takeover |
| Automations | Each loop on/off/shadow, contact windows, capacity, fatigue, templates (with compliance check), offer grid, approval thresholds — all versioned |
| Approvals | Maker-checker inbox (exists) |
| Experiments | Holdouts, power analysis, incremental ₹ |
| Models | Per-tenant model cards, live metrics, drift, shadow vs champion, retrain history |
| Compliance & Audit | Decisions, contact log, policy KB, audit export (CSV/PDF), DPDP requests |
| Integrations | Razorpay/Cashfree/Stripe, WhatsApp, telephony, email, AA (regulated tenants), CSV import |
| Developers | API keys, outgoing webhooks, MCP connection (OAuth), A2A agent card, API reference |
| Settings | Team & roles (OIDC), billing/usage, data retention, languages |
| **End-customer pages** | Hosted pay/update-mandate page in the customer's language; preference centre (channels, times, opt-out) — DPDP-aligned |

Design system: Tailwind + shadcn/ui components, Recharts/Tremor for charts, WCAG AA, dark/light, i18n. Every page: real data, loading/error/empty states, permissions, audit trail.

---

## 4. Architecture v2 (all free / open-source)

| Concern | Choice | Why |
|---|---|---|
| Identity | **Zitadel** or **Keycloak** (OIDC, orgs, roles) | Open-source, multi-tenant orgs |
| Secrets | **OpenBao** (open-source Vault fork) | Provider tokens, OAuth refresh tokens |
| OLTP | Postgres 16 + pgvector (exists) | |
| Streaming | Redpanda (exists) + **Quix Streams / Bytewax** consumers | Real-time features, bank health |
| Lakehouse | **Apache Iceberg** tables on S3 (SeaweedFS) via **PyIceberg**, queried with **DuckDB** | Bronze/silver/gold, time travel for point-in-time training |
| CDC | Transaction-id change capture (Debezium later, see ADR-0012) | Every OLTP change into the lakehouse without dual writes or missed late commits |
| Data/ML orchestration | **Dagster** (assets, schedules, sensors, per-tenant partitions) | Lineage + backfills + retrain schedules |
| Business orchestration | **Temporal** (exists) | Long-running customer workflows |
| Data validation | **Pandera** + Dagster asset checks | Contracts on every table |
| Feature store | **Feast** (offline: Iceberg/DuckDB, online: Redis) | Same features in training and serving |
| Experiment tracking/registry | MLflow (exists) | Per-tenant model names |
| Model serving | **BentoML** service | Versioned, batched, observable |
| Drift/monitoring | **Evidently** + Prometheus/Grafana | Data + prediction drift, calibration |
| LLM tracing/evals | **Langfuse** (self-hosted) + **promptfoo** in CI | |
| PDF/document parsing | **Docling** | Loan agreements, T&Cs, statements |
| OCR | RapidOCR (exists) | |
| Voice | **LiveKit Agents** + official **Sarvam plugin**, SIP trunk / Exotel | Streaming, barge-in, telephony |
| MCP | Official **MCP Python SDK** server (streamable HTTP) behind the ToolGateway | Real protocol |
| A2A | **A2A v1.0** server/client (official SDK if available for Python, else spec-conformant) | |
| Observability | OpenTelemetry → Prometheus, Grafana, Loki/Tempo | |
| Frontend | Next.js 16 (exists), shadcn/ui | |

Services that run 24/7: `api`, `webhook-ingress`, `outbox-relay`, `event-bridge` (Kafka → Temporal signals), `temporal-workers`, `feature-streamer`, `model-server`, `voice-agent`, `mcp-server`, `a2a-server`, `dagster-daemon`, `web`.

---

## 5. Data platform & ML pipelines (the enterprise part)

```
Sources: provider APIs (backfill) · provider webhooks · Postgres CDC (Debezium) · WhatsApp/voice events · AA (consented)
   → BRONZE (Iceberg, immutable raw, per tenant partition)
   → validation (Pandera contracts; failures quarantine + alert)
   → SILVER (normalised customers, subscriptions, mandates, debits, payments, contacts, replies, outcomes)
   → GOLD: feature views (Feast), labels (verified outcomes), experiment tables
   → training (Dagster per-tenant partitions; temporal splits; point-in-time joins via Feast)
   → evaluation gates (ml/gates.yaml per model) → MLflow registry (m1.<tenant>)
   → shadow (predict + log, no actions) → canary (x% of decisions) → champion
   → serving (BentoML; online features from Feast/Redis)
   → monitoring (Evidently drift, calibration on labelled outcomes, business KPIs) → retrain trigger
```

### Where training data comes from (honest)
| Tier | Source | Use |
|---|---|---|
| Tenant history (real) | 12-month backfill from the tenant's provider + ongoing webhooks | **Primary** training data for that tenant's models |
| Verified outcomes (real) | Nirantar's own labels after each cycle | Retraining; uplift from holdout randomisation |
| RecurSim (synthetic) | Simulator with counterfactuals | Cold start for new tenants; evaluation of uplift/policies; tests |
| Public research datasets | KKBox (churn), Home Credit (installments), Criteo (uplift) | Method development and benchmarks only (licences forbid commercial training) — downloaded via a registered, checksum-verified ingestion job |

### Cold-start policy
New tenant → rules + global synthetic-trained priors (flagged "cold start") → shadow → per-tenant model when ≥ N verified labels (default 2,000 debits / 200 failures) and gates pass. Tenant data is never pooled (ADR-0004 f).

---

## 6. Model zoo v2 (all trained, gated, served, monitored)

| # | Model | Question it answers | Label (verified) | Approach |
|---|---|---|---|---|
| M1 | Debit failure (T-3) | Will this debit fail? | debit outcome | LightGBM + calibration → sequence model when history is long |
| M2 | Cash window | When does this customer have money? | payment/recovery days | Circular stats → Bayesian periodicity; AA salary days when consented |
| M3 | Retry timing | When should a failed debit be retried? | retry success by hour | Survival (DeepHit) + constrained bandit |
| M4 | Bank/gateway health | Is a bank degrading now? | incidents (provider downtime events + our own failure stream) | EWMA / BOCPD streaming |
| M5 | Uplift | Which contact changes the outcome? | holdout-randomised outcomes | DR-learner / causal forest (promoted only if it beats heuristics) |
| **M6** | **Churn / lapse hazard** | Will this subscriber cancel or lapse in 30/60/90 days? | cancellation, mandate revoke, non-renewal | Survival (Cox → gradient-boosted survival) |
| **M13** | **Involuntary vs voluntary churn** | Is this churn caused by payment failure or by choice? | cancel reason / failure-then-lapse | Classifier → routes to recovery vs win-back |
| **M14** | **Win-back propensity + uplift** | Will a win-back offer bring a lapsed subscriber back, and does it cause it? | reactivation within 30 days | Uplift model on randomised win-back arm |
| **M15** | **Customer lifetime value** | What is this subscriber worth over 12 months? | realised revenue | sBG (shifted beta-geometric) for contractual churn + spend model |
| **M16** | **Mandate revocation risk** | Will the mandate be revoked/paused soon? | revoke/pause events | GBDT |
| M7 | Promise-to-pay reliability | Will they pay as promised? | PTP kept | GBDT |
| M8 | Intent & objection (te/hi/en, code-mixed) | What did the customer mean? | human-labelled replies (+ reviewed synthetic) | Fine-tuned multilingual encoder (MuRIL/IndicBERT) |
| M9 | Roll-rate (lending) | Will DPD worsen? | bucket transitions | Markov → GBDT hazard |
| M10 | Cash forecast | How much will we collect per day? | realised collections | Σ M1 + residual model + conformal intervals |
| M11 | Dispute win probability | Will a representment win? | dispute outcomes | LR → GBDT (replaces the prior) |
| M12 | Conduct classifier | Is this message harassing/manipulative? | labelled messages | Small classifier + regex floor |
| **M17** | **Best contact time** | When does this customer respond? | reply/pickup timestamps | Per-customer Bayesian hour model |
| **M18** | **Tenant KPI anomaly** | Did something break (plan, bank, template)? | incident labels | Seasonal decomposition + robust z |

Churn loop (new): M6 flags at-risk subscribers → M13 splits involuntary vs voluntary → involuntary goes to recovery/mandate repair; voluntary goes to a `RevivalWorkflow` with M14-targeted win-back offers from the tenant's offer grid → outcomes feed M6/M14.

---

## 7. GenAI v2
- Prompt registry (versioned YAML per task/language) + promptfoo eval suites in CI (format validity, amount fidelity, script/language, conduct, groundedness).
- Langfuse tracing: every LLM call with tenant, prompt version, model, tokens, cost, latency; per-tenant budgets.
- Guard layer: M12 + optional guard model; untrusted-content fencing (exists).
- RAG: tenant document upload (Docling parsing) → per-tenant corpus; citations required (exists); RAG eval set written by a compliance reviewer.
- LLM tasks stay narrow: drafting, explanation, representment prose, summarisation, triage of unknowns. No money math (unchanged rule).

## 8. Agents & multi-agent v2
- All 12 agents wired into workflows (mandate doctor → MandateHealthWorkflow, dispute defender → DisputeWorkflow, collections → CollectionsWorkflow with the launch gate, treasury → TreasuryWorkflow, revival → RevivalWorkflow).
- Per-tenant automation settings drive every agent (no constants).
- Agent evaluation harness: scripted RecurSim scenarios → task success, tool-call correctness, invalid action rate, policy violation rate (must be 0), recovery after tool failure; runs in CI.
- Human takeover everywhere (conversation and case level).

## 9. Orchestration v2
Temporal workflows: CustomerLifecycle (entity), DebitCycle (exists), MandateHealth, Collections, Dispute, Revival, Treasury, Onboarding (connect → backfill → validate → train → shadow → go-live), Retrain (or Dagster schedule), Reconciliation (periodic). Always-on services from §4; worker versioning; dead-letter handling; runbooks.

## 10. MCP & A2A (real protocols)
- **MCP server** using the official Python SDK (streamable HTTP), OAuth 2.1 per tenant, tools = the existing ToolGateway (policy, approvals, audit unchanged). Merchants can connect Claude or any MCP client to their tenant.
- **A2A v1.0**: publish `/.well-known/agent-card.json` per tenant; implement task send/stream/status per spec; collection-agency handoff and customer-agent requests over HTTP with signed messages (exists as library).

## 11. Multimodal v2
WhatsApp Cloud API inbound (text, voice notes, images, documents) → evidence pipeline (exists) → case; Docling for PDFs; vision-language model for document fields where OCR fails; trained screenshot-forgery classifier on a generated + labelled dataset.

## 12. Voice v2
LiveKit Agents + official Sarvam plugin (streaming STT/TTS) + SIP trunk (Exotel or another provider) → sub-second turns target; outbound calls scheduled by the Contact Arbiter within windows/capacity; recording with disclosure; post-call transcript → M8 → case; human transfer to an agent desk page; voice registration gate (TRAI) enforced (exists).

## 13. Quality gates (CI)
GitHub Actions: lint/type/tests (unit, integration with services, e2e), import boundaries, migrations up/down, ML gates on RecurSim, RAG/LLM/agent evals, security scans (pip-audit, npm audit, Trivy), Playwright UI tests, load tests (k6/Locust) and chaos scenarios (worker kill, provider outage, Redis down) on a schedule.

---

## 14. Roadmap (no deployment until P8)

| Phase | Weeks | Delivers | Done when |
|---|---|---|---|
| **P1 Settings & de-hardcoding** ✅ | 1–2 | `tenant_settings` (versioned), template registry, prompt registry, routing config; all constants removed; Automations UI v1 | grep finds no business constants; settings changes are audited |
| **P2 Data platform** ✅ | 3–5 | Provider backfill jobs, Debezium CDC, Iceberg bronze/silver/gold, Pandera contracts, Dagster assets + checks | A tenant's 12-month history lands in gold tables with passing contracts |
| **P3 Feature store & ML pipelines** ✅ (M1; other models in P4+) | 5–7 | Feast (offline/online), per-tenant training for M1/M2/M3/M6/M13/M16/M17, gates, registry, BentoML serving, Evidently drift, shadow → canary → promote | Training/serving skew test passes; models retrain on schedule and on drift |
| **P4 Churn & revival loop** ✅ | 7–8 | M6/M13/M14/M15, RevivalWorkflow, win-back holdout | At-risk subscribers get correct routing; win-back uplift measured |
| **P5 Always-on services & workflows** ◐ built (ADR-0015: services, Dispute/Onboarding/Reconciliation/MandateHealth workflows, DLQ replay, replay-tested versioning, runbook; Retrain = Dagster; CustomerLifecycle deliberately not built; open: 72 h soak; Collections/Treasury gated) | 8–10 | Relay, event bridge, workers, all workflows in §9, reconciliation schedule | 72-hour soak test with RecurSim traffic, zero lost events |
| **P6 Real channels** ◐ (ADR-0017: WhatsApp + Sarvam voice notes done; SMS on hold (DLT); calls deferred; inbound live needs public URL) | 10–12 | WhatsApp Cloud API (test number), LiveKit + Sarvam streaming voice + SIP, inbound multimodal, hosted customer pages | Real messages/calls end to end in sandbox |
| **P7 Real protocols** ✅ (ADR-0016; real-client interop pending a public URL in P8) | 12–13 | MCP server (OAuth), A2A v1.0 endpoints + agent cards | External MCP/A2A clients complete tasks through policy |
| **P8 Platform & website** ◐ (sign-in — ADR-0018; self-serve onboarding — ADR-0019; next: design system, public URL, billing, CI) | 13–16 | OIDC (Zitadel), orgs/teams, onboarding wizard, Customers 360, Conversations, Developers, Billing, public website, docs; CI with all gates; Helm/Compose self-host | A new business self-onboards in < 30 minutes |

## 15. What is needed from the product owner
1. Razorpay **technology-partner OAuth app** (development client) — or keep test keys for now.
2. **WhatsApp Cloud API** test number and token (free tier).
3. Telephony: an Exotel trial or any SIP trunk for LiveKit.
4. A **design partner** (even a small subscription business) for real history — the single biggest step from "working" to "proven".
5. A compliance reviewer to write the RAG evaluation questions and review templates.

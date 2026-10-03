# Nirantar — Engineering Build Brief (baseline v1.0, supplied by product owner 2026-09-28)

Condensed faithfully from the product owner's brief. Section numbers match the original so requirements can be traced (`BB-§n`).

**§1 Mission.** Autonomous recurring-revenue OS for subscriptions, SaaS/OTT/Edtech, EMIs, SIPs, insurance premiums, B2B retainers. Loop: TRIGGER → CONTEXT → PREDICT → PLAN → ARBITRATE → COMPLIANCE CHECK → ACT → WAIT → VERIFY → RECOVER/ESCALATE → MEASURE VS HOLDOUT → LABEL → LEARN. Must: (1) predict failure before failure (2) diagnose why (3) choose best intervention (4) execute via controlled tools (5) communicate with customers (6) recover failed payments (7) lending collections (8) disputes/chargebacks (9) revive lapsed customers (10) forecast cash (11) verify money movement against authoritative ledgers (12) measure incremental revenue with randomized holdouts (13) retrain models and improve policies.

**§2 Source of truth.** Read all of `00_SOURCE_OF_TRUTH/` first. Don't silently remove capabilities. Conflicts → ADR, prefer safer/deterministic/production-correct, keep backward compatibility where practical. Verify time-sensitive facts against primary sources.

**§3 Research first.** Primary sources: Razorpay, Cashfree, Stripe, RBI, NPCI, MeitY/GoI, Sarvam, Groq, Anthropic, Exotel, WhatsApp Business, dataset licence pages. Per dependency: `docs/integrations/<name>.md` with source URL, retrieval date, API version, auth, endpoints, webhook events, rate limits, idempotency, retries, sandbox, errors, limitations, security, implementation notes.

**§4 Regulatory safety.** Versioned regulatory KB covering e-mandate, pre-debit notification, UPI AutoPay, NACH/eNACH, card recurring, digital lending, recovery conduct, outsourcing/LSP, payment security, DPDP Act + Rules, consent, minimisation, retention, grievance, auditability. Each policy: policy_id, source, effective_date, jurisdiction, applicability, rule, exceptions, implementation, test_case. Never hard-code a regulation without source + date. Conflict with old requirement → stop, ADR.

**§5 Positioning.** Orchestration/recovery platform integrating licensed providers; never represented as an RBI-authorised PA without authorisation. Provider-neutral.

**§6 Architecture.** Modular monorepo: apps/ services/ packages/ ml/ agents/ mcp/ data/ infra/ docs/ evals/ tests/. Domains: identity/auth, tenant, customer, merchant, subscriptions, mandates, payments, payment events, reconciliation, communication, lending, collections, disputes, revival, treasury, experiments, model serving, agent orchestration, policy/compliance, memory, evidence, audit, notifications, analytics. Clear boundaries, no giant monolith.

**§7 Multi-tenancy.** Isolation at DB, cache, vector retrieval, object storage, events, MCP authorisation, agent memory, analytics. No cross-tenant retrieval. Tenant id on every important record. Automated cross-tenant isolation tests.

**§8 Money safety.** LLMs never do financial calculations, amounts, money date arithmetic, retry scheduling, authorisation, ledger verification, reconciliation, idempotency. Path: Agent → policy/compliance → authorisation → MCP → provider → verification → ledger.

**§9 Core tech.** Next.js/TS/Tailwind/shadcn; Python/FastAPI; Postgres, pgvector, Redis; Redpanda; Temporal; LangGraph where appropriate; scikit-learn, LightGBM, XGBoost, PyTorch where justified, EconML, MLflow; Feast if justified; Parquet, ClickHouse; MinIO; OpenTelemetry, Prometheus, Grafana; custom eval harness. Every major dependency needs an ADR.

**§10 Providers.** Adapters under payments/{domain,providers/{razorpay,cashfree,stripe},webhooks,reconciliation,idempotency}. Common interface: fetch_payment, create_payment_link, list_subscriptions, charge_subscription, pause_subscription, create_refund, verify_signature, parse_webhook, reconcile. Capabilities declared explicitly; never assume identical behaviour.

**§11 Webhooks.** Signature verification, raw-body validation, idempotency, replay protection, event versioning, durable persistence, DLQ, retry, reconciliation. Flow: authenticate → persist raw → idempotency → normalise → publish → workflow → ledger/state → audit. Never lose raw events.

**§12 Events.** Topics payment.*, mandate.*, subscription.*, dispute.*, reply.*, call.*, bank.health, outcome.*, experiment.*, compliance.*. Versioned, typed, immutable, traceable, tenant-aware, replayable.

**§13 Temporal.** CustomerLifecycleWorkflow ⊃ DebitCycle, Collections, Dispute, Revival; plus TreasuryWorkflow, RetrainSchedule. Survive restarts, deploys, worker failure, duplicate signals, delayed events, provider outages. Short tasks separate from long workflows.

**§14 Agents.** Conductor, Mandate Doctor, Debit Strategist, Failure Triage, Contact Arbiter, Conversation, Voice, Collections Strategist, Dispute Defender, Treasury, Compliance Guardian, Verifier. Each: typed input/output schema, allowed + forbidden tools, instructions, policy constraints, timeout, retry, fallback, observability, eval suite. No uncontrolled autonomy.

**§15 Conductor.** Consume events, gather context, invoke specialists, maintain state, request plans, arbitrate, pass compliance, trigger MCP, wait on Temporal, invoke Verifier, record outcome, publish labels. Specialists own specialist reasoning.

**§16 Contact Arbiter.** Deterministic optimiser (OR-Tools). Inputs: uplift, customer value, fatigue budget, consent, contact hours, channel availability, language, recent contacts. Maximise expected incremental value subject to consent, windows, fatigue, tenant policy, compliance. Not an LLM.

**§17 Model zoo.** M1, M2, M6, M9 (predict); M3, M5 (decide); M4, M11 (detect); M7, M8 (understand); M10 (forecast); M12 (guard). Each: dataset version, feature version, training config, validation, baseline, advanced model, calibration analysis, model card, registry version, serving contract, drift monitor, rollback.

**§18 Real data first.** Separate research / synthetic / sandbox / customer-provided / production. Dataset record: dataset_id, source, license, collection_date, schema, PII status, allowed use, version.

**§19 RecurSim.** Simulate recurring customers, income cycles, salary dates, mandates, attempts, bank behaviour, gateway failures, decline codes, communication responses, PTPs, churn, disputes, offers, contact fatigue, heterogeneity. Must expose true counterfactual outcomes.

**§20 MLOps.** ingest → validate → features → point-in-time joins → train → evaluate → offline gate → shadow → canary → promote → monitor → rollback. Configurable gates: AUC, PR-AUC, Qini, C-index, MAPE/WAPE, calibration, business value.

**§21 Holdout.** Tenant-level randomized, default 5–10 %. Don't leak assignment into model features incorrectly. Store experiment_id, tenant_id, customer_id, treatment, assignment_timestamp, exposure, outcome, value. Compute incremental payment/revenue/recovery, confidence intervals, uplift curves.

**§22 GenAI.** LLM gateway abstraction, multi-provider; routing by task, latency, cost, language, capability, reliability. Model IDs configurable and verified.

**§23 RAG.** Ingestion → layout-aware parse → section-aware chunk → embeddings → pgvector + BM25 → hybrid → rerank → metadata filters → citations → citation checker. Metadata: tenant, rail, document_type, regulation, effective_date, jurisdiction, version. Fail closed for compliance answers without evidence.

**§24 Memory.** Customer, Merchant, Procedural, Episodic Case. Explicit schemas; tool-controlled writes; record has source, timestamp, confidence, provenance, tenant, consent applicability.

**§25 MCP.** Gateway + servers: gateway, mandate, bank-health, customer, comms, voice, lending, dispute, treasury, policy, experiment, ledger. Every tool: name, description, input/output schema, scope, approval level, idempotency, audit, timeout, retry. Agents never touch DBs or gateways directly.

**§26 Security.** OAuth/OIDC, RBAC, ABAC where justified, tenant isolation, least privilege, tool scopes, secret rotation, encryption in transit/at rest, audit logs, rate limits, replay protection, idempotency, request signing, webhook verification, prompt-injection defences, PII redaction, retention controls. Threat model: prompt injection, tool abuse, cross-tenant access, fraudulent screenshots, replay, webhook spoofing, authz bypass, privilege escalation, malicious customer text, poisoned documents, compromised MCP server.

**§27 Multimodal.** Audio, WhatsApp text/voice notes, screenshots, PDFs, KYC/mandate docs, AA JSON, emails → Unified Evidence object (case_id, customer_id, modality, source_uri, extracted_fields, language, confidence, verified_against, hash, created_at). Traceable.

**§28 Screenshots.** OCR → layout → structured extraction → consistency checks → forgery classifier → UTR verification against provider. Never authoritative; ledger/provider wins.

**§29 Voice.** Sarvam behind an abstraction. Exotel → media stream → VAD → barge-in → LID → STT → turn manager → Voice Agent → MCP → Compliance → TTS → caller. Telugu, Hindi, English, code-mixed, other Indic. Measure STT quality, language accuracy, turn latency, barge-in, tool-call correctness, PTP extraction, human-transfer correctness. Never collect OTP/PIN.

**§30 A2A.** Agent cards (name, org, skills, endpoint, auth, signing key); lifecycle submitted → working → input-required → completed/failed/canceled; signed messages; A2A for agent tasks, MCP for tools.

**§31 Compliance Guardian.** Every outbound action (message, call, payment action, refund, pause, restructure, legal notice, representment, credit draw) → ALLOW / DENY / REQUIRE_APPROVAL / REQUIRE_MORE_INFORMATION. No bypass.

**§32 Approvals.** Inbox; configurable thresholds (large refunds, high-value representment, restructuring outside policy, credit draw, payout reschedule, legal notices); signed, scoped, expiring approval tokens.

**§33 Verifier.** Deterministic. Payment claims → provider/ledger; message sent → provider acceptance; PTP recorded → durable DB event. Never trust agent output as evidence.

**§34 Ledger.** Immutable entries, reconciliation, balance checks, provider txn IDs, idempotency, compensating entries, traceability. No silent mutation.

**§35 Observability.** Per action: tenant_id, trace_id, workflow_id, agent_id, model_id, model_version, tool_name, tool_version, policy_version, input_hash, output_hash, decision, approval, provider_request_id, provider_response_code, latency, result, error. Distributed tracing. Dashboards: payment success, recovery, failed payments, uplift, agent success, tool errors, compliance blocks, voice latency, drift, RAG quality, cost, provider health.

**§36 Audit.** Hash-chained log of prompt, model version, retrieved docs, policy decision, tool request, approval, provider response, verification, outcome. No unnecessary raw PII.

**§37 Failure engineering.** Test provider timeout, gateway outage, bank degradation, duplicate/missing/stale webhooks, network partition, worker crash, Temporal restart, MCP failure, LLM timeout/unavailable, Sarvam unavailable, DB/Redis outage, event replay, duplicate agent action, approval timeout. Use backoff, idempotency, DLQs, sagas, circuit breakers, fallbacks.

**§38 Testing.** Unit, integration, contract, provider sandbox, workflow, agent, MCP, security, load, chaos, ML, RAG, voice, multimodal, E2E. No feature complete without tests.

**§39 Benchmarks.** ML: ROC-AUC, PR-AUC, Brier, calibration, C-index, MAPE, WAPE, Qini, AUUC. Agents: task success, tool correctness, invalid action rate, recovery after tool failure, policy violation rate. RAG: retrieval recall, citation precision/recall, faithfulness, unsupported claim rate. Voice: WER, code-mixed WER, latency, interruption success, PTP extraction. Platform: p50/p95, throughput, error rate, provider recovery, workflow completion. Business: incremental ₹, recovery rate, success lift, churn reduction, dispute recovery, contact cost per recovered ₹.

**§40 Merchant dashboard.** Overview, Payments, Recovery, Customers, Subscriptions, Mandates, Collections, Disputes, Revival, Treasury, Experiments, AI Agents, Automations, Compliance, Audit, Models, Integrations, Settings. Each: real API data, loading/error/empty states, filters, pagination, drill-down, audit trail, permissions.

**§41 Platform console.** Tenants, provider status, workflow status, queue health, agent health, model health, drift, cost, security alerts, audit search, failed jobs, DLQ, approval queue.

**§42 CI/CD.** Lint, format, types, unit, integration, contract, security/dependency/container scans, ML validation, migration validation, build, deploy. PRs fail on type errors, failing tests, schema mismatch, migration failure, critical security findings, benchmark regression.

**§43 Infra.** local/dev/staging/prod; Docker Compose locally; IaC for cloud; secrets outside git.

**§44 Docs.** README, architecture, local-development, deployment, security, threat-model, data-model, events, agent-design, mcp, a2a, rag, memory, ml-platform, evaluation, provider-integrations, compliance, incident-response, runbooks/, ADRs.

**§45 CLAUDE.md.** Architecture rules, commands, conventions, testing commands, service map, invariants, forbidden shortcuts, definition of done, milestones. Update only for durable rules.

**§46 Phases.** 0 audit · 1 architecture/ADRs · 2 monorepo · 3 DB + events · 4 auth + tenancy · 5 provider adapters · 6 webhooks + reconciliation · 7 ML data/features · 8 model zoo + serving · 9 RAG + memory · 10 agent framework · 11 Temporal · 12 MCP · 13 multimodal · 14 voice · 15 A2A · 16 compliance + approvals · 17 dashboards · 18 evals · 19 security/chaos/load · 20 staging · 21 E2E. Don't skip phases.

**§47 Subagents.** For parallel provider/regulatory research, schema review, ML experiments, frontend review, security review, test generation, performance. Each returns findings, files changed, tests, risks, recommendations.

**§48 Source verification.** Record URL, source, retrieval date, version, claim, implementation impact.

**§49 No fake completion.** No "implemented" without code + tests run; no "production ready" without gates; no "Razorpay compatible" without contract/integration tests; no "compliant" without documented legal review boundary; no "AI improved revenue" without controlled evaluation.

**§50 Definition of done.** Code, schema, tests, observability, error handling, security, docs, evaluation where applicable, runs locally, integration test passes, failure path works, rollback/compensation where required.

**§51 First task.** Inspect files, map requirements, research providers and regulations, identify contradictions, missing dependencies, security risks; produce gap report, dependency matrix, requirements traceability matrix, repo tree, ADR-0001, implementation roadmap, evaluation roadmap. Then implement.

**§52 Phase report.** PHASE, STATUS, Completed, Tests, Benchmarks, Files created, Files changed, Known issues, Next phase. Continue until feasible acceptance criteria are met; use deterministic mocks when credentials are missing.

**§53 Final acceptance test.** Merchant → customer → ₹999 subscription → scheduled → M1 predicts risk → Contact Arbiter → compliance → agent uses MCP → provider sandbox action → simulated reply → Temporal waits → payment succeeds → Verifier confirms → ledger → outcome → experiment exposure → label → model pipeline consumes → evaluation updates. Then functional, security, workflow, ML, agent, MCP, RAG, performance tests, with an end-to-end trace proving every stage.

**§54 Optimise for** correctness, security, auditability, determinism, observability, maintainability, performance, DX, real value — **not** agent count, file count, buzzwords, needless microservices, decorative AI, fake dashboards, unverified benchmarks, needless LLM calls.

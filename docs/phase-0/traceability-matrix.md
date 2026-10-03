# Requirements traceability matrix

Every requirement from the build brief (BB-§n) and specs maps to an implementation location, a verification and a phase. Status is updated at every phase report. Statuses: `planned` · `in-progress` · `done` (Definition of Done met, tests run) · `blocked` (reason given).

| Req | Requirement | Implementation | Verification | Phase | Status |
|---|---|---|---|---|---|
| BB-§2 | Source of truth read, conflicts via ADR | `00_SOURCE_OF_TRUTH/`, `docs/adr/` | Review | 0 | done |
| BB-§3 | Primary-source integration docs | `docs/integrations/*.md` | Doc review, citations | 0 | in-progress |
| BB-§4 | Versioned regulatory KB | `docs/compliance/policies.yaml` → `packages/policy` | Each policy has a test_case executed in `tests/compliance` | 0 → 16 | in-progress |
| BB-§5 | Provider-neutral positioning | README, adapters | Copy review | 1 | planned |
| BB-§6 | Modular monorepo, domain boundaries | repo tree, import-linter contracts | CI boundary check | 2 | planned |
| BB-§7 | Tenant isolation at 8 layers | RLS, key prefixes, vector filters, object paths, topic keys, MCP scopes, memory | `tests/tenancy/` isolation suite | 4 (+each layer's phase) | planned |
| BB-§8 | No LLM money math; safe action path | `packages/core/money`, policy → approvals → MCP → verifier → ledger | Schema tests reject LLM amounts; E2E trace | 3–16 | planned |
| BB-§10 | Provider adapters + capabilities | `packages/payments/providers/*` | Contract tests from doc fixtures; sandbox tests when keys exist | 5 | planned |
| BB-§11 | Webhook pipeline | `services/webhook-ingress` | Spoof, replay, duplicate, DLQ tests | 6 | planned |
| BB-§12 | Versioned tenant-aware events | `packages/contracts`, `packages/events` | Schema compatibility tests, replay test | 3 | planned |
| BB-§13 | Temporal workflows survive failure | `services/workers` | Time-skipping tests, worker-kill chaos test | 11 | planned |
| BB-§14–15 | 12 agents with typed contracts; Conductor | `agents/` | Agent eval suites | 10 | planned |
| BB-§16 | Deterministic Contact Arbiter | `agents/contact_arbiter` (OR-Tools) | Property tests: never violates consent/window/fatigue | 10 | planned |
| BB-§17 | 12 models with cards, gates, drift, rollback | `ml/models/*` | Offline gates in `evals/ml` | 8 | planned |
| BB-§18 | Dataset provenance | `data/registry.yaml` | Registry lint in CI | 7 | planned |
| BB-§19 | RecurSim with counterfactuals | `ml/recursim` | Counterfactual consistency tests | 7 | planned |
| BB-§20 | MLOps pipeline to rollback | `ml/pipelines`, MLflow | Pipeline integration test | 8 | planned |
| BB-§21 | Randomized holdout + incrementality | `packages/domain-experiments` | Assignment balance tests, CI coverage on synthetic truth | 8 | planned |
| BB-§22 | LLM gateway, routing | `packages/llm-gateway` | Routing tests, fallback tests | 9 | planned |
| BB-§23 | Hybrid RAG, fail-closed citations | `packages/rag` | Retrieval recall, citation precision tests | 9 | planned |
| BB-§24 | Typed memory with provenance | `packages/memory` | Write-path tests, tenant isolation | 9 | planned |
| BB-§25 | MCP gateway + 12 servers | `services/mcp-gateway`, `mcp/` | Tool contract tests, scope tests | 12 | planned |
| BB-§26 | Security controls + threat model | across; `docs/threat-model.md` | `tests/security` | 4, 19 | planned |
| BB-§27–28 | Evidence object, screenshot protection | `packages/domain-evidence` | Forged-screenshot tests | 13 | planned |
| BB-§29 | Sarvam voice pipeline | `services/voice-gateway`, `packages/voice` | WER, latency, PTP extraction evals | 14 | planned |
| BB-§30 | A2A cards, signed lifecycle | `packages/a2a` | Signature/tamper tests, lifecycle tests | 15 | planned |
| BB-§31–32 | Compliance Guardian, approvals | `packages/policy`, `packages/approvals` | Bypass attempts fail; token expiry/scope tests | 16 | planned |
| BB-§33 | Deterministic Verifier | `packages/verifier` | Mismatch tests | 6 | planned |
| BB-§34 | Double-entry ledger | `packages/ledger` | Balance invariants, compensating entries | 3 | planned |
| BB-§35–36 | Observability, hash-chained audit | `packages/core/tracing`, `packages/domain-audit` | Chain-verification test | 2–3 | planned |
| BB-§37 | Failure engineering | `tests/chaos` | Scenario list executed | 19 | planned |
| BB-§38–39 | Test types + benchmarks | `tests/`, `evals/` | CI | all, 18 | planned |
| BB-§40–41 | Dashboards + console | `apps/web` | Playwright E2E | 17 | planned |
| BB-§42 | CI/CD gates | `.github/workflows` | Pipeline runs | 2 | planned |
| BB-§43 | Environments + IaC | `infra/` | Compose up; terraform validate | 2, 20 | planned |
| BB-§44–45 | Docs + CLAUDE.md | `docs/`, `CLAUDE.md` | Review | continuous | in-progress |
| BB-§53 | Final acceptance trace | `tests/e2e/test_acceptance_999.py` | Trace artefact | 21 | planned |

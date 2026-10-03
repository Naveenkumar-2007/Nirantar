# Nirantar — engineering rules

Autonomous recurring-revenue OS. Orchestration/recovery platform that integrates licensed payment providers; **not** a payment aggregator. Spec lives in `00_SOURCE_OF_TRUTH/`; decisions in `docs/adr/`; verified provider facts in `docs/integrations/`; regulatory KB in `docs/compliance/policies.yaml`.

## Commands

```bash
docker compose up -d                 # postgres:25432 redis:6379 redpanda:19092 temporal:7233 s3:8333
uv sync --group dev --extra events
uv run alembic upgrade head
uv run pytest -q                     # unit + integration (+ sandbox when test keys are in .env)
uv run mypy && uv run ruff check src tests && uv run lint-imports
```

## Invariants (never break)

1. **Money is `nirantar.core.money.Money` (int minor units).** Floats are rejected. LLM output never produces an amount, date or retry schedule for a money action.
2. **Tenant isolation is enforced by Postgres RLS.** App code uses `nirantar.db.session.tenant_tx(tenant_id)`; the app role (`nirantar_app`) cannot bypass RLS. New tenant tables need RLS + `FORCE` + policy in the migration, and a test in `tests/tenancy/`.
3. **Ledger and audit are append-only** (DB triggers + no UPDATE/DELETE grants). Corrections are compensating entries.
4. **State changes and their events commit together** via `events.outbox`; the relay publishes to Kafka. Never publish to Kafka directly from a business transaction.
5. **Provider data is truth; webhooks are hints.** Re-fetch before acting; payment state only moves forward (`payments/state.py`); regressions become `billing.discrepancies`.
6. **Nothing settles without the Verifier.** `verifier.payments.settle_verified` refuses unverified input.
7. **Agents never import providers or the DB** (import-linter contract). MCP is the only action boundary.
8. **Secrets are references** (`env:NAME`), never stored in tables or logs. `.env` is git-ignored.
9. **Regulatory values come from `policies.yaml` with a policy_id.** Unverified values are config tenants can only tighten (ADR-0004).
10. **No business constants in code (ADR-0011).** Capacities, costs, effects, thresholds, holdout size, policy
    tightenings live in versioned tenant settings (`nirantar.settings`); customer wording lives in the template
    registry (maker-checker); starting values in `settings/defaults/*.yaml`. Agents get them via
    `settings.runtime.load` (workflows) or the `content.get_template` MCP tool.

## Forbidden shortcuts

- Claiming "implemented / compliant / production ready / improved revenue" without the tests, review or controlled evaluation that proves it.
- Retrying a non-idempotent provider POST blindly (see `payments/http.py`, ADR-0003).
- Catching broad exceptions to make a test pass; skipping a test without a documented reason.

## Definition of done

Code + schema + tests (incl. failure path) + observability hook + docs/ADR where a decision was made + runs locally + quality gate green.

## ML rules

- Train/evaluate with `uv run python -m nirantar.ml.pipelines`; results in `evals/results/ml_latest.json`, cards in `ml/model-cards/`.
- Temporal splits only; features must pass the leakage test (`tests/ml/test_recursim_and_features.py`).
- Promotion only through `ml/gates.yaml`. A simpler model that wins stays champion (ADR-0007).
- RecurSim results are labelled `source=recursim` and never presented as production performance.
- Every dataset must be in `data/registry.yaml`; research/NC data can never be `commercial_training`.

## Agent & workflow rules

- Temporal owns time; LangGraph makes one decision inside one activity (ADR-0008).
- Activities take `now` from the workflow; never use the wall clock for decisions.
- Every side effect goes through `ToolGateway.call` (scope → schema → dedupe → policy → approval → execute → audit).
- Policy hits cite a real `policy_id` from `docs/compliance/policies.yaml` or `internal-policies.yaml`
  (NIR-GOV-* = platform governance, never presented as regulation).
- Deferred rounds (all channels closed now) are not experiment exposures.
- Acceptance: `uv run pytest tests/e2e -q` (add `NIRANTAR_E2E_LIVE_LLM=1` for live Groq/Sarvam); trace in
  `evals/results/acceptance_trace.json`. In CI set `NIRANTAR_REQUIRE_SERVICES=1` so missing services fail.

## Benchmarks (re-run after relevant changes)

- ML: `uv run python -m nirantar.ml.pipelines` · RAG: `uv run python -m nirantar.rag.evaluate`
- Voice (live Sarvam, costs credits): `uv run python -m nirantar.voice.benchmark`
- Screenshots are never proof of payment; evidence code must keep `verdict=not_verified` unless a provider record matches.

## Data platform (P2, ADR-0012)

- `uv sync --extra data`; lakehouse = Iceberg on SeaweedFS (:8333) with catalog DB `nirantar_lake` (auto-created
  bucket; create the DB once: `CREATE DATABASE nirantar_lake` as owner).
- One tenant, by hand: `nirantar.data.pipeline.run_tenant(...)`; via API: `POST /v1/data/sync`.
- 24/7: `uv run dagster dev -m nirantar.data.orchestration -p 3070` (DAGSTER_HOME in .env; copy
  `infra/dagster/dagster.yaml`). Sensor/schedule auto-start only in staging/production or with NIRANTAR_AUTOMATION=on.
- Never read the lake without a tenant; never land raw PII (use `data.pii.redact`); new silver tables need a
  contract in `data/contracts.py`; labels that need time to settle must carry a `label_final` flag.

## ML platform (P3, ADR-0013)

- Model features are computed ONLY by `nirantar.features.compute.compute_features` (offline and online). A new
  feature or formula change = new feature-set VERSION. Never feed a model features built elsewhere.
- Decisions go through `nirantar.ml.router.ModelRouter` (champion/canary/shadow/prior, all scores logged).
- A tenant model exists only if it beats the prior offline (ml/gates.yaml) and then on live paired outcomes.
  Synthetic-tenant results are labelled synthetic everywhere; RecurSim global models are research only.

## Retention (P4, ADR-0014)

- Eligibility for any experiment is decided on pre-treatment facts BEFORE randomisation; outcomes are measured the
  same way in every arm (intention-to-treat). Never filter after assignment.
- Win-back is promotional: promotional consent + template registry + opt-out; offer amounts are computed by
  `mcp.tools.offer_terms`, never passed in. New models = a spec in `ml/specs.py` + gates in `ml/gates.yaml`.

## Always-on services (P5, ADR-0015)

- Provider calls always go through `ProviderResolver` / `provider_for(source, tenant)`: the TENANT's own account.
  Never a process-wide key; `mock` only by injection.
- Every event handler must be idempotent and order-independent (deterministic workflow ids, signal-with-start,
  REJECT_DUPLICATE). Outages pause the bridge; bad events dead-letter (`events.consumer_dead_letters`).
- Timings and thresholds that a merchant could reasonably change belong in the `operations` settings namespace.
- No fake delivery: the worker uses `UnconnectedSink` until a real channel exists.
- Changing a workflow: `workflow.patched` + `uv run pytest tests/replay`, then re-record histories
  (`NIRANTAR_CAPTURE_HISTORIES=1 uv run pytest tests/e2e`). Runbook: docs/runbooks/services.md.

## External agents (P7, ADR-0016)

- MCP clients and A2A partners act ONLY through the ToolGateway with their own tool sets (`mcp.server.EXTERNAL_TOOLS`,
  `a2a.server.A2A_TOOLS`); money/state-changing actions from outside always need a human approval.
- Approved actions execute via `approvals.executor.ApprovalExecutor` (proposer's tools, tenant's own provider).
- Never expose experiment assignment, mandatory notices or gated tools to external agents.

## Channels (P6, ADR-0017)

- Customer messages go through `channels.sink.ChannelSink`: free text only inside WhatsApp's 24 h window, otherwise
  the Meta-approved template for the same registry key (pass `OutboundTemplate` with the facts). Never bypass it.
- Tests never use the real WhatsApp account (conftest strips WHATSAPP_*); live checks need explicit owner approval.
- SMS is on hold (no DLT). Templates: `uv run python -m nirantar.channels.wa_templates status|submit`.

## Identity (P8, ADR-0018)

- People sign in with OIDC (Keycloak, realm in infra/keycloak). The API verifies tokens locally; ROLES come from
  core.user_memberships, never from token claims. API keys remain for services.
- Dashboard sessions are encrypted cookies; tokens never reach browser JS. Refresh happens in src/proxy.ts.

## Running the product locally

```bash
uv run python -m nirantar.demo.seed --customers 150          # real-pipeline demo tenant; prints API keys
uv run uvicorn nirantar.api.serve:app --port 18080            # API (loads .env)
uv run python -m nirantar.services                            # worker + relay + event bridge, /health :18090
uv run python -m nirantar.mcp                                 # MCP server (OAuth 2.1) on :18100
cd apps/web && npm run dev -- --port 3010                     # dashboard; needs apps/web/.env.local (git-ignored)
```
Dashboard env: NIRANTAR_API_URL, NIRANTAR_API_KEY (owner/viewer), NIRANTAR_APPROVER_KEY, NIRANTAR_PLATFORM_KEY.
Next.js 16: read `apps/web/node_modules/next/dist/docs/` before using framework APIs (params are Promises).

## Status

M-A foundation, M-B money rails, M-C learning core, M-D thin E2E (₹999 acceptance), M-E breadth (RAG, memory,
voice, mandate/dispute/treasury/collections agents, A2A, multimodal evidence) done.
M-F product surfaces done (API + dashboard + platform console, browser-verified).
Phase 2 plan: docs/plan/phase-2-platform-plan.md. P1 (tenant settings, template registry, learned parameters,
Automations UI) done — ADR-0011. P2 data platform (provider backfill, txid change capture, Iceberg
bronze/silver/gold on SeaweedFS, Pandera contracts + quarantine, gold charge_outcomes, Dagster, Data page) done —
ADR-0012. P3 feature store (shared transformation, Iceberg offline + Redis online), per-tenant M1 training with
gates vs the prior, shadow→canary→champion rollout + rollback on live labels, Evidently monitoring — ADR-0013.
P4 churn lifecycle labels, sBG lifetime value, M6/M13 on a generic model-spec framework, relative at-risk scoring,
win-back RevivalWorkflow with consent-before-randomisation, holdout and verified reactivation — ADR-0014.
P5 per-tenant provider resolution, one supervised services process (Temporal worker on one queue, outbox relay,
Kafka→Temporal event bridge with DLQ, /health), Dispute/Onboarding/ReconciliationSweep workflows, `operations`
settings, soak with invariants, mandates from provider truth + MandateHealth/Repair workflows, dead-letter
replay, replay-tested workflow versioning — ADR-0015. Open: 72 h soak.
P7 MCP server (official SDK, streamable HTTP, OAuth 2.1 + PKCE, dashboard consent) and A2A v1.0 (official
a2a-sdk, partner keys, two skills through the gateway); approvals execute with the proposer's tools — ADR-0016.
P6 WhatsApp Cloud API (24 h window + approved templates, signed webhooks, inbound routing, Sarvam voice notes both
ways, STOP, statuses) — ADR-0017; SMS on hold, phone calls deferred.
P8 step 1: sign-in (Keycloak OIDC, encrypted sessions, memberships, self-onboarding) — ADR-0018.
Next: P8 platform (OIDC, onboarding UI, billing,
self-host packaging, CI).

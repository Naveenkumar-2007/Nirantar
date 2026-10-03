# Nirantar

**An AI-native operating system for recurring revenue in India.** Nirantar keeps subscription and EMI payments
flowing, wins back customers who leave, defends chargebacks, and *proves* what worked. Every action goes through
compliance policy, human approval where money is involved, and a tamper-evident audit trail.

> Status: in active development (Phase 2, P8). Not deployed. See [`CLAUDE.md`](CLAUDE.md) → *Status* and
> [`docs/plan/phase-2-platform-plan.md`](docs/plan/phase-2-platform-plan.md).

## What it does

| | |
|---|---|
| **Payments** | Razorpay (subscriptions, payment links, disputes, tokens/mandates), Cashfree adapter; every webhook re-verified with the provider; double-entry ledger |
| **Data & ML** | Iceberg lakehouse (bronze → silver → gold) with contracts; per-merchant models (debit failure, churn, churn type, lifetime value) trained, gated against baselines, rolled out shadow → canary → champion |
| **Agents** | Debit strategist, conversation, mandate doctor, dispute defender, win-back — all acting only through one MCP-style tool gateway (scope → schema → policy → approval → execute → audit) |
| **Workflows** | Temporal: debit cycle, disputes, win-back, mandate repair, onboarding, reconciliation; event bridge from Kafka; replay-tested versioning |
| **Channels** | WhatsApp Cloud API (24 h window + approved templates, signed webhooks), Sarvam speech for voice notes in Indian languages |
| **Protocols** | MCP server (official SDK, OAuth 2.1 + PKCE) and A2A v1.0 (official `a2a-sdk`) for external agents |
| **Identity** | Keycloak OIDC sign-in; roles per business from Nirantar's own memberships; Postgres row-level security per tenant |

## Run it locally

Requirements: Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
cp .env.example .env                      # fill in the secrets you have; generate random ones for the rest
docker compose up -d                      # Postgres, Redis, Redpanda, Temporal, object store, Keycloak
uv sync --all-extras
uv run alembic upgrade head
uv run uvicorn nirantar.api.serve:app --port 18080          # API
uv run python -m nirantar.services                          # worker + outbox relay + event bridge (/health :18090)
cd apps/web && cp .env.example .env.local && npm install && npm run dev -- --port 3010
```

Open http://localhost:3010, create an account, and set up your business.

## Quality gates

```bash
uv run ruff check . && uv run mypy src && uv run lint-imports
uv run pytest                              # unit, integration, e2e (needs docker compose), replay
cd apps/web && npx tsc --noEmit && npx eslint src
```

## Documentation

- Architecture decisions: [`docs/adr/`](docs/adr) (0001–0018)
- Operations runbook: [`docs/runbooks/services.md`](docs/runbooks/services.md)
- Compliance policies (RBI, NPCI, TRAI, DPDP): [`docs/compliance/`](docs/compliance)
- Provider integration notes: [`docs/integrations/`](docs/integrations)
- Product and architecture specs: [`00_SOURCE_OF_TRUTH/`](00_SOURCE_OF_TRUTH)

## Security

Secrets live only in `.env` / `apps/web/.env.local` (git-ignored). Report vulnerabilities privately to the
maintainer; do not open public issues for them.

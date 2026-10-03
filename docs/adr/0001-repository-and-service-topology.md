# ADR-0001: Repository layout and service topology

- Status: Accepted
- Date: 2026-09-28
- Traces: BB-§6, BB-§9, BB-§54

## Context

The brief asks for clear service boundaries across ~24 domains, "no giant monolith", and also "no unnecessary microservices". The team is small (one engineer plus agents), there are no production customers yet, and every extra deployable adds network failure modes, auth hops and ops cost.

## Decision

1. **One monorepo**, Python managed as a `uv` workspace, frontend as a separate `pnpm` workspace under `apps/`.
2. **Domains are Python packages with enforced boundaries**, not separate services. Each domain under `packages/domain-*` exposes a public API module; cross-domain imports are checked in CI by `import-linter` contracts. Domains own their own tables (schema-per-domain in Postgres).
3. **A small, fixed set of deployables**, split only where the runtime profile genuinely differs:

| Deployable | Why separate |
|---|---|
| `services/api` (FastAPI) | Synchronous tenant/merchant/dashboard API |
| `services/webhook-ingress` | Must stay up and fast when everything else is degraded; persists raw events first |
| `services/workers` (Temporal workers) | Long-running workflows and activities; scale independently |
| `services/mcp-gateway` (+ MCP servers as modules) | The action boundary; separate process so agents can't bypass it and so its credentials are isolated |
| `services/model-serving` | CPU/GPU-heavy, different scaling and release cadence |
| `services/voice-gateway` | Real-time WebSocket media; strict latency budget |
| `apps/web` (Next.js) | Merchant dashboard + platform console |

4. Infrastructure dependencies for local dev run via Docker Compose: Postgres (+pgvector), Redis, Redpanda, Temporal, MinIO, MLflow, OpenTelemetry collector, Prometheus, Grafana.

## Consequences

- Positive: strong boundaries without network overhead; a domain can later become its own service because its public API already exists.
- Negative: boundary discipline depends on CI contracts; a shared database cluster means schema-per-domain rules must be enforced by migrations review.
- Deferred (separate ADRs when justified): Feast (start with a point-in-time feature module over Postgres/Parquet), ClickHouse (start with Postgres + Parquet), Kubernetes (start with Compose; IaC for a single VM, then k8s).

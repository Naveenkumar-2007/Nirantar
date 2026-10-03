# ADR-0010: Product surfaces — HTTP API, merchant dashboard, platform console (milestone M-F)

- Status: Accepted · Date: 2026-09-28 · Evidence: tests/integration/test_api.py, live browser verification

## Decisions

1. **API**: FastAPI, API-key auth (`nk_<tenant>.<key>.<secret>`), RBAC per route, every tenant route runs on an
   RLS-scoped connection (cross-tenant reads return 404). Keyset pagination with **opaque base64url cursors**.
2. **Approval inbox executes**: a grant returns a single-use token that the API immediately redeems through the
   MCP gateway to execute the stored action; a second grant is rejected (409).
3. **Dashboard**: Next.js 16 (App Router, server components). API keys live only in server env; the browser
   calls server actions / route handlers, never the API directly. `fetch` is uncached; each page has
   loading/error/empty states. Pages: Overview, Debits (+timeline), AI agents, Approvals, Compliance,
   Experiments, Models & evals, Audit, Policy assistant, Platform console.
4. **Demo data is produced by the real pipeline** (`nirantar.demo.seed`), not fixtures.
5. **Times are business times**: gateway actions and audit records take the gateway clock; timelines order
   events by `occurred_at`.
6. **Dry-run Guardian decisions are recorded** on the action row so the Compliance page shows every block.

## Bugs found by this milestone (all fixed, each with a regression test)

| Bug | Impact | Found by |
|---|---|---|
| ₹-amount guard matched "Rs" inside a URL id (`…NRS331…`) | Correct recovery message blocked | Demo tenant (1 failed action) |
| Tool failure reason not persisted | Operators couldn't see why an action failed | Same investigation |
| Pagination cursor contained `+` | Page 2 broke for real HTTP clients | API test |
| Tenant ContextVar reset across contexts | Every FastAPI request with a DB dependency failed | API test |
| Dry-run DENY decisions invisible | Compliance page showed 1 decision instead of dozens | Browser review |
| Action/audit times were wall-clock | Timelines out of order for replays/backfills | Browser review |

## Known limits (not done yet)

- **No end-user login on the dashboard**: it uses server-side service keys. Production needs OIDC login +
  per-user RBAC (the API already enforces RBAC per key). Do not expose the dashboard publicly as-is.
- Platform console queries use the owner (superuser) role; production needs a dedicated read-only BYPASSRLS role
  limited to aggregate views.
- The outbox relay must run as its own process (`python -m nirantar.events.relay`); the demo seeder leaves a
  backlog until it does.
- Local ports: Postgres 25432, API 18080, dashboard 3010 (5432/8080 unavailable on the dev machine).

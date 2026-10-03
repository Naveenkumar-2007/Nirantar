# ADR-0002: Local infrastructure and storage choices

- Status: Accepted · Date: 2026-09-28 · Traces: BB-§9, BB-§43

## Decision

| Concern | Choice | Why |
|---|---|---|
| System of record | PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`) | One transactional store for domain data, RLS isolation, and vectors; fewer moving parts than a separate vector DB |
| Isolation | Postgres RLS, `FORCE ROW LEVEL SECURITY`, app role `nirantar_app` is `NOBYPASSRLS` and not table owner | Database enforces tenant isolation even if application code is wrong (BB-§7) |
| Immutability | Triggers reject UPDATE/DELETE on ledger entries/lines and audit records; app role has no UPDATE/DELETE grant on them | BB-§34, BB-§36 |
| Cache / idempotency / rate limits | Redis 7 | Standard; tenant-prefixed keys |
| Object storage | S3 API via **SeaweedFS** locally | MinIO no longer publishes public container images (pull denied on Docker Hub and quay.io, 2026-09-28). Code depends only on the S3 API, so production can use AWS S3 / GCS / any S3-compatible store |
| Ports | Postgres on host **25432** | Host 5432 is used by a native Postgres; 55390–55898 are Windows-reserved |

## Consequences

Switching object stores requires only endpoint/credential config. RLS means platform-wide operator queries need a separate audited role (added with the platform console, Phase 17).

# Proposed repository tree

```
nirantar/
├─ 00_SOURCE_OF_TRUTH/            product, architecture, brief, research, decisions, diagrams/
├─ CLAUDE.md                      durable engineering rules
├─ pyproject.toml                 uv workspace root
├─ docker-compose.yml             local infra
├─ apps/
│  └─ web/                        Next.js + TS + Tailwind + shadcn (merchant dashboard + platform console)
├─ services/
│  ├─ api/                        FastAPI app wiring domain routers
│  ├─ webhook-ingress/            verify → persist raw → idempotency → normalise → publish
│  ├─ workers/                    Temporal workflows + activities
│  ├─ mcp-gateway/                auth, scopes, policy, approval tokens, audit; hosts MCP servers
│  ├─ model-serving/              model registry loader + prediction API
│  └─ voice-gateway/              telephony media ↔ STT/TTS ↔ Voice Agent
├─ packages/
│  ├─ core/                       config, ids, money (Decimal/paise), time, errors, tenancy context, logging, tracing
│  ├─ contracts/                  pydantic event + API schemas (versioned), JSON Schema export
│  ├─ db/                         SQLAlchemy base, tenant-scoped session, Alembic migrations
│  ├─ events/                     Kafka producer/consumer, envelope, outbox, DLQ
│  ├─ ledger/                     double-entry ledger (immutable, compensating entries)
│  ├─ payments/
│  │  ├─ domain/                  PaymentProvider protocol, capability declarations
│  │  ├─ providers/{razorpay,cashfree,stripe,mock}/
│  │  ├─ webhooks/                signature verification, normalisation
│  │  ├─ reconciliation/
│  │  └─ idempotency/
│  ├─ domain-tenants/  domain-customers/  domain-merchants/  domain-subscriptions/
│  ├─ domain-mandates/ domain-comms/ domain-lending/ domain-collections/ domain-disputes/
│  ├─ domain-revival/  domain-treasury/ domain-experiments/ domain-evidence/ domain-audit/
│  ├─ policy/                     regulatory KB loader, policy engine, Compliance Guardian
│  ├─ approvals/                  signed, scoped, expiring approval tokens
│  ├─ verifier/                   deterministic verification service
│  ├─ llm-gateway/                provider abstraction + routing + budgets
│  ├─ rag/                        ingestion, chunking, hybrid retrieval, rerank, citation checker
│  ├─ memory/                     typed memory stores
│  ├─ voice/                      STT/TTS/LID provider abstraction (Sarvam first)
│  └─ a2a/                        agent cards, signed messages, task lifecycle
├─ agents/                        12 agents: schemas, prompts, allowed tools, graphs, evals
├─ mcp/                           12 MCP servers (tool definitions + handlers)
├─ ml/
│  ├─ recursim/                   simulator with true counterfactuals
│  ├─ features/                   point-in-time feature definitions
│  ├─ models/m01_debit_failure … m12_conduct_judge/
│  ├─ pipelines/                  train → evaluate → gate → register
│  └─ model-cards/
├─ data/
│  ├─ registry.yaml               dataset records (id, source, licence, PII, allowed use, version)
│  └─ fixtures/                   provider webhook fixtures (from official docs)
├─ evals/                         benchmark harness: ml/, agents/, rag/, voice/, platform/, business/
├─ tests/                         cross-cutting: e2e/, security/, chaos/, load/, tenancy/
├─ infra/
│  ├─ compose/  otel/  grafana/  prometheus/
│  └─ terraform/                  staging/prod IaC
└─ docs/
   ├─ adr/  integrations/  compliance/  phase-0/  runbooks/
   └─ architecture.md, security.md, threat-model.md, … (BB-§44)
```

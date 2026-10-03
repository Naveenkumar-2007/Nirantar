# Dependency matrix (Phase 0)

"ADR" = the decision record that justifies the dependency (BB-§9). "Local" = how it runs in dev without credentials.

| Dependency | Purpose | Used by | Needed from phase | Local | Credential needed | ADR |
|---|---|---|---|---|---|---|
| Python 3.12 + uv | Backend, ML, agents | all Python | 2 | installed | — | 0001 |
| FastAPI + Pydantic v2 | APIs, typed schemas | api, ingress, mcp-gateway | 2 | pip | — | 0001 |
| PostgreSQL 16 + pgvector | System of record, RLS, vectors | all domains, RAG, memory | 3 | Compose | — | 0002 |
| SQLAlchemy 2 + Alembic | ORM, migrations | db | 3 | pip | — | 0002 |
| Redis 7 | Idempotency cache, rate limits, online features | ingress, gateway, serving | 3 | Compose | — | 0002 |
| Redpanda | Event backbone (Kafka API) | events | 3 | Compose | — | 0003 |
| Temporal | Durable workflows | workers | 11 (skeleton earlier) | Compose / test env | — | 0004 |
| MinIO | Evidence, recordings, datasets, model artefacts | evidence, ml | 3 | Compose | — | 0002 |
| LightGBM, scikit-learn, XGBoost | Tabular models | M1, M7, M9, M11 | 8 | pip | — | 0006 |
| PyTorch | TFT, DeepSurv, DeepHit, DragonNet, N-BEATS, fine-tuning | M1+, M3, M5, M6, M8, M10 | 8 | pip (CPU) | — | 0006 |
| EconML | Causal forest, meta-learners | M5 | 8 | pip | — | 0006 |
| OR-Tools | Contact Arbiter optimiser | arbiter | 10 | pip | — | 0007 |
| MLflow | Tracking + registry | ml pipelines, serving | 7 | Compose | — | 0006 |
| LangGraph | Agent step graphs where branching is real | agents | 10 | pip | — | 0008 |
| MCP Python SDK | Tool servers | mcp | 12 | pip | — | 0009 |
| OpenTelemetry + Prometheus + Grafana | Traces, metrics, dashboards | all | 2 | Compose | — | 0010 |
| Next.js, TS, Tailwind, shadcn/ui | Dashboards | apps/web | 17 | node 22 | — | 0011 |
| Razorpay API | Provider adapter | payments | 5 | mock adapter | key id/secret, webhook secret | 0005 |
| Cashfree API | Provider adapter | payments | 5 | mock adapter | client id/secret, webhook secret | 0005 |
| Stripe API | Provider adapter | payments | 5 | mock adapter | secret key, webhook secret | 0005 |
| Anthropic API | LLM tiers | llm-gateway | 10 | deterministic stub LLM for tests | API key | 0008 |
| Groq API | Low-latency LLM alternative | llm-gateway | 10 | stub | API key | 0008 |
| Sarvam API | STT, TTS, LID, Indic chat | voice | 14 | stub + recorded fixtures | subscription key | 0012 |
| Exotel | Telephony media stream | voice-gateway | 14 | local WebSocket simulator | account sid/token | 0012 |
| WhatsApp Cloud API | Messaging | comms | 12 | mock | token, app secret | 0009 |
| OCR (PaddleOCR or Tesseract) | Screenshots, PDFs | evidence | 13 | pip / binary | — | 0013 |
| Feast | Feature store | ml | deferred | — | — | deferred (0001) |
| ClickHouse | Analytics at scale | analytics | deferred | — | — | deferred (0001) |

ADRs 0002–0013 are written as each phase starts; this table is the checklist.

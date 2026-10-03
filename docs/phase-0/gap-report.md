# Architecture gap report (Phase 0, 2026-09-28)

Status legend: 🔴 blocks a phase · 🟠 must resolve before the dependent phase · 🟢 noted, no block.

## 1. Missing inputs

| # | Gap | Impact | Resolution |
|---|---|---|---|
| G1 🟠 | The 8 approved diagrams aren't in `00_SOURCE_OF_TRUTH/diagrams/` yet | Diagram/spec drift can't be checked | `02-architecture-spec.md` is authoritative until they arrive; review on arrival |
| G2 🟠 | No provider credentials (Razorpay, Cashfree, Stripe, Sarvam, Exotel, WhatsApp, Anthropic, Groq) | No live sandbox calls | Deterministic mock adapters + contract tests built from official doc fixtures; live sandbox tests skipped with a clear marker until keys exist (BB-§52) |
| G3 🟢 | Temporal CLI not installed | Local dev server | Use `temporalio/auto-setup` in Compose; tests use the SDK's time-skipping test environment |
| G4 🟢 | No GPU assumed | TFT, DragonNet, DeepSurv, MuRIL fine-tuning slow | Baselines first; advanced models trained at small scale on CPU; document compute limits in model cards |
| G5 🟠 | No real recurring-payment data | Every model result is synthetic until a design partner | Dataset registry marks provenance; reports label results "RecurSim" or "research data"; never "production" (BB-§18, §49) |

## 2. Spec contradictions and risky assumptions (to finalise after regulatory research)

| # | Assumption in baseline | Concern | Proposed handling |
|---|---|---|---|
| C1 | L2 may "shift the debit inside the mandate window" to after predicted salary day | The pre-debit notice fixes a debit date and amount; a change probably needs a fresh notice ≥ 24h before and must respect the mandate's frequency/validity | Treat as a policy-gated action; the policy engine computes the earliest legal new date; ADR after research |
| C2 | Randomized holdout for lending collections | Withholding legally required notices (e.g. demand/recall notices, pre-debit notices) is not acceptable | Holdout only withholds *optional* interventions; mandatory communications always go out; enforced in the experiment service |
| C3 | 8am–7pm contact window "lending only" | Subscription voice/WhatsApp outreach is governed by TRAI commercial-communication rules and WhatsApp policy | Separate policy records per channel and purpose; Contact Arbiter consumes whichever is stricter |
| C4 | Cross-tenant model training on customer payment history | DPDP purpose limitation and tenant contracts | Default: per-tenant models + global models trained only on RecurSim/public data; cross-tenant pooling needs an ADR, contractual basis and anonymisation |
| C5 | Storing call recordings | Consent, retention, DPDP | Record only with captured consent; retention per tenant policy; PII-minimised transcripts |
| C6 | Sarvam model names/versions and Anthropic model IDs | Time-sensitive | Configurable IDs, verified in `docs/integrations/` |

## 3. Security risks identified up front

| Risk | Where | Control |
|---|---|---|
| Prompt injection via customer replies, emails, PDFs | Conversation, Dispute, RAG | Untrusted-content wrapping, tool allow-lists per agent, Compliance Guardian after every plan, no tool can be triggered by retrieved text alone |
| Webhook spoofing / replay | webhook-ingress | HMAC verification on raw body, timestamp tolerance where provider supports it, event-id idempotency store |
| Cross-tenant leakage | DB, Redis, pgvector, MinIO, Kafka, memory | Tenant id in every key/row/object path; Postgres RLS; automated isolation tests |
| Forged payment screenshots | Evidence | Screenshot never authoritative; provider/ledger verification wins |
| Tool abuse / privilege escalation by agents | MCP | Per-agent scopes, approval tokens (signed, scoped, expiring), rate limits, kill switch |
| Money-math by LLM | Agents | Money values only come from deterministic services; schemas reject LLM-supplied amounts for money actions |
| Compromised MCP server | MCP gateway | Servers run behind the gateway, mutual auth, least-privilege provider credentials per server |

## 4. Scope realism

The brief's 21 phases are an enterprise programme. They will be delivered as working vertical slices with the Definition of Done enforced per capability, and every phase report will state exactly what is and isn't done. The final acceptance scenario (BB-§53) is the north star: every phase is sequenced to make that trace possible as early as possible, then deepened.

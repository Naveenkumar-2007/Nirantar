# ADR-0009: RAG, memory, voice, specialist agents, A2A and multimodal evidence (milestone M-E)

- Status: Accepted · Date: 2026-09-28 · Evidence: evals/results/{rag_latest,voice_latest}.json, tests/*

| Area | Decision | Evidence / reason |
|---|---|---|
| RAG | Local `BAAI/bge-small-en-v1.5` (384-d) + Postgres full-text, RRF fusion. **Cross-encoder rerank off by default** | 18-question set: recall@5 1.00 both; MRR 0.963 (hybrid) vs 0.944 (+ms-marco rerank). Set is author-written → optimistic; needs compliance-authored questions |
| RAG answers | Citations must be retrieved chunks AND verbatim quotes; else `insufficient_evidence` | Live Groq answer on recovery hours cited IN-RBI-RBC-RECOVERY-HOURS-001 verbatim |
| Corpus writes | Shared corpus (`ten_global`) written only by the platform role; tenants can replace their own docs (migration 0006) | Found missing DELETE grant during M-E |
| Memory | Declared keys only, allowed sources per key, supersede-never-overwrite, DPDP erasure tombstones | tests/integration/test_memory.py |
| Voice | Sarvam `saaras:v3` (codemix) + `bulbul:v3` at 8 kHz PCM16 (= Exotel format). **Scripted replies** on money calls (no free-form LLM speech in v1). OTP/PIN redaction; hardship → human | Live: intent agreement 1.00 (te/hi/en); REST turn 1.40 s vs 1.5 s target; TTS dominates → streaming TTS next |
| Voice metric | Report intent agreement alongside CER | Codemix writes English loanwords in Latin script; raw CER overstates errors |
| Specialist agents | Mandate Doctor, Treasury, Collections are deterministic planners; Dispute Defender drafts prose only from system-of-record facts; evidence building lives in the `disputes` domain (agents never import the DB) | import-linter contract |
| M11 dispute win probability | Transparent prior, explicitly not a trained model | No labelled dispute outcomes yet |
| A2A | Ed25519-signed messages, trusted-card registry per tenant, 5-min freshness, nonce replay protection, enforced task lifecycle | Agency attempts outside 08:00–19:00 IST or to third parties are flagged with RBI policy ids |
| Evidence | Raw bytes in S3 (content-addressed, tenant-prefixed); screenshots **never authoritative**; tamper signals are a baseline (editing-software metadata, malformed UTR, no provider record); image-forgery model deferred | tests/integration/test_evidence.py |

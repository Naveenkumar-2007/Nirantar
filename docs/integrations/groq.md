# Groq (GroqCloud) — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Groq page during this session.

## Source URLs (retrieved 2026-09-28)

- Models: https://console.groq.com/docs/models
- Rate limits: https://console.groq.com/docs/rate-limits
- OpenAI compatibility: https://console.groq.com/docs/openai

## API version

- OpenAI-compatible, path-versioned: **base URL `https://api.groq.com/openai/v1`**. No date-version header.

## Authentication

- `Authorization: Bearer <GROQ_API_KEY>` (OpenAI-style). Org-level keys; project/key scoping — **UNVERIFIED**.

## Relevant endpoints (Nirantar scope)

- `POST /openai/v1/chat/completions` — low-latency LLM turns (streaming SSE).
- `GET /openai/v1/models` — list active models.
- `POST /openai/v1/audio/transcriptions` (Whisper) — standard OpenAI path; not individually fetched (**UNVERIFIED** exact path). `vtt`/`srt` response formats unsupported.
- Responses API supported (per compatibility page; path `/openai/v1/responses` **UNVERIFIED**).
- TTS (`/openai/v1/audio/speech`) with Orpheus preview models — **UNVERIFIED** path; no Indic voices found.

## Models relevant for low-latency voice turns

Production:
| Model ID | Speed (t/s, per docs) | Context | Max completion |
|---|---|---|---|
| `openai/gpt-oss-20b` | ~1000 | 131,072 | 65,536 |
| `llama-3.1-8b-instant` | ~560 | 131,072 | 131,072 |
| `openai/gpt-oss-120b` | ~500 | 131,072 | 65,536 |
| `llama-3.3-70b-versatile` | ~280 | 131,072 | 32,768 |
| `whisper-large-v3`, `whisper-large-v3-turbo` | STT | — | — |

Preview (evaluation only, not for production): `canopylabs/orpheus-v1-english`, `canopylabs/orpheus-arabic-saudi` (TTS), `meta-llama/llama-prompt-guard-2-22m`, `meta-llama/llama-prompt-guard-2-86m`, `minimaxai/minimax-m2.7`, `openai/gpt-oss-safeguard-20b`, `qwen/qwen3.8-27b`.

Indic-language quality of these models for Hindi/regional code-mixed dialogue — **UNVERIFIED** (no Groq statement); Sarvam models are the Indic-native option.

## Tool use / structured output

- Function calling supported (compatibility page). JSON-schema structured outputs / `response_format` support per model — **UNVERIFIED** in this session.

## Webhook events

- None (synchronous API). Batch API exists for Developer plan (callbacks **UNVERIFIED**).

## Rate limits

- Metrics: RPM, RPD, TPM, TPD, ASH/ASD (audio seconds per hour/day), and ITPM/OTPM for some orgs; organization-wide; whichever limit is hit first applies. **Cached tokens do not count toward rate limits.**
- Free plan example (Llama models): 30 RPM, 1K RPD, 8K TPM, 200K TPD. Developer plan: higher (exact per-model values **UNVERIFIED**). Free-tier limits are far too low for production voice traffic.
- 429 with `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens`, `retry-after` (request-count headers `x-ratelimit-*-requests` likely — **UNVERIFIED**).

## Idempotency behavior

- None documented — **UNVERIFIED**. Stateless; persist outputs keyed by Nirantar turn id.

## Retry behavior

- Client-side only: honor `retry-after` on 429; backoff on 5xx. Provider-side retries N/A.

## Sandbox / test-mode behavior

- No sandbox; free plan usable for development.

## Error model

- OpenAI-style `{"error": {"message", "type", "code"}}` — **UNVERIFIED** exact fields. Unsupported params (`logprobs`, `logit_bias`, `top_logprobs`, `messages[].name`, `n != 1`) return errors. `temperature: 0` silently converted to 1e-8.

## Limitations

- Preview models may be removed without notice; only production models for Nirantar.
- Data retention / zero-data-retention and India data residency — **UNVERIFIED**; check Groq privacy/DPA before sending customer PII.
- No Indic TTS.

## Security considerations

- Server-side keys only; redact PII from prompts; treat tool calls as proposals subject to Nirantar policy checks.

## Implementation notes for Nirantar

1. Use Groq as the fast-turn LLM in the voice loop (e.g., `openai/gpt-oss-20b` or `llama-3.1-8b-instant` for intent/slot filling), with Sarvam STT/TTS on either side; escalate complex reasoning to Claude asynchronously.
2. Use a provider-agnostic OpenAI-compatible client so Groq, Sarvam `/v2/chat/completions` (beta) and others are swappable.
3. Benchmark Hindi/Hinglish accuracy before committing; keep Sarvam-105B-conversations as Indic fallback.
4. Require Developer plan for production; monitor TPM headers and shed load gracefully.

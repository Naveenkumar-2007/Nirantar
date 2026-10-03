# Anthropic (Claude API) — Integration Research

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Anthropic page during this session. (docs.anthropic.com now 301-redirects to platform.claude.com/docs.)

## Source URLs (retrieved 2026-09-28)

- Models overview: https://platform.claude.com/docs/en/models/overview (redirected from https://docs.anthropic.com/en/docs/about-claude/models/overview)
- Rate limits: https://platform.claude.com/docs/en/api/rate-limits
- Errors (incl. model-specific validation errors, request-id, anthropic-version example): https://platform.claude.com/docs/en/api/errors
- Structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Pricing (linked, not fetched): https://platform.claude.com/docs/en/about-claude/pricing
- Model deprecations (linked, not fetched): https://platform.claude.com/docs/en/about-claude/model-deprecations

## API version

- Header **`anthropic-version: 2023-06-01`** (shown in official curl example). Beta features via `anthropic-beta: <name>` headers (e.g., `output-300k-2026-03-24`, `thinking-binding-controls-2026-08-01`).

## Model IDs (verified — expectations in the brief were correct)

| Model | Claude API ID | Alias | Context | Max output | Price (in/out per MTok) | Thinking | Retirement (not sooner than) |
|---|---|---|---|---|---|---|---|
| Claude Opus 5.5 | `claude-opus-5-5` | same | 1M | 128K | $4 / $20 | Adaptive, **always on**; default effort `medium` | 2027-09-22 |
| Claude Sonnet 5 | `claude-sonnet-5` | same | 1M | 128K | $2 / $10 | Adaptive; default effort `high` | 2027-06-30 |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | `claude-haiku-4-5` | 200K | 64K | $1 / $5 | Extended (manual budget); effort not supported | **2026-10-15** |
| Claude Fable 5.1 | `claude-fable-5-1` | same | 1M | 128K | $10 / $50 | Adaptive, always on | 2027-09-01 |

- Dateless IDs (4.6 generation onward) are pinned snapshots. Batch API 50% off; cache reads 10% of base input (5% on Opus 5.5).
- **Haiku 4.5 retirement commitment is only "not sooner than October 15, 2026" — ~17 days after retrieval.** Check the deprecations page before relying on it.

## Authentication

- Header `x-api-key: <key>`; plus `anthropic-version` and `content-type: application/json`. Base `https://api.anthropic.com/v1/messages`.

## Relevant endpoints

- `POST /v1/messages` (streaming via SSE), `POST /v1/messages/count_tokens`, `POST /v1/messages/batches` (Batch API), `GET /v1/models` (Models API returns `max_input_tokens`, `max_tokens`, `capabilities`). Paths other than `/v1/messages` — standard but not individually fetched (**UNVERIFIED** exact paths for count_tokens/batches).

## Tool use & structured outputs

- Tool use supported on all current models. **Strict tool use** (`"strict": true` in tool definition) guarantees schema-valid tool names/inputs.
- **Structured outputs are GA**: `output_config.format = {"type":"json_schema","schema":{...}}` (old `output_format` param deprecated; no beta header needed). Supported on Opus 5.5, Sonnet 5, Haiku 4.5 and others.
- Schema limits: `additionalProperties` must be `false`; no recursive schemas, no external `$ref`, no numeric/length constraints (`minimum`, `maximum`, `minLength`, `maxLength`); `minItems` only 0/1. Compiled grammar cached 24 h.
- **Opus 5.5 does NOT support forced tool use** (`tool_choice` `any` / `tool` → 400); use `auto` + strict tools or structured outputs.
- **Prefill not supported** on 4.6+ models (400).
- Opus 5.5: `thinking: {"type":"disabled"}` rejected; thinking always on (use `display: "omitted"` to hide). Replayed thinking blocks must match unchanged history (new accounts since 2026-08-31 get 400 on mismatch).

## Webhook events

- None for Messages API. **UNVERIFIED** whether any webhook exists for batches (poll instead).

## Rate limits (per model, per org; token bucket)

| Tier | Opus 5.5 / Sonnet 5 / Haiku 4.5 (each separate) RPM · ITPM · OTPM | Monthly spend cap |
|---|---|---|
| Start | 1,000 · 2,000,000 · 400,000 | $500 |
| Build | 5,000 · 5,000,000 · 1,000,000 | $1,000 |
| Scale | 10,000 · 10,000,000 · 2,000,000 | $200,000 |

- New orgs may start in a lower "Evaluation" tier. Cached reads don't count toward ITPM (most models). Acceleration limits can 429 on sudden ramps.
- Headers: `retry-after`, `anthropic-ratelimit-{requests|tokens|input-tokens|output-tokens}-{limit|remaining|reset}`.
- Spend-cap 429: `rate_limit_error` with `error.details.error_code = "enforced_spend_limit_reached"` and **no `retry-after`** — retries fail until next month. Self-set spend limit → 400 `invalid_request_error`.

## Idempotency behavior

- No idempotency-key header documented for Messages — **UNVERIFIED** (none found). LLM calls are non-deterministic; Nirantar must persist outputs keyed by its own request id rather than re-calling.

## Retry behavior

- Client-side: official SDKs auto-retry connection errors, 429, 5xx **twice by default** with exponential backoff honoring `retry-after` (`max_retries` configurable). Errors can also occur mid-stream after a 200 (SSE error events).

## Sandbox / test-mode behavior

- No sandbox; use separate workspace with low spend/rate limits for dev (workspace limits configurable).

## Error model

- `{"type":"error","error":{"type","message"},"request_id"}`; `request-id` response header.
- 400 `invalid_request_error`, 401 `authentication_error`, 402 `billing_error`, 403 `permission_error`, 404 `not_found_error`, 409 `conflict_error`, 413 `request_too_large` (Messages 32 MB), 429 `rate_limit_error`, 500 `api_error`, 504 `timeout_error`, 529 `overloaded_error`.

## Limitations

- Non-streaming requests validated against a 10-minute SDK timeout; use streaming for long outputs.
- Latency: Opus "Moderate", Sonnet "Fast", Haiku "Fastest" — for real-time voice turns Haiku/Sonnet (or Groq) are preferable to always-thinking Opus 5.5. Time-to-first-token figures — **UNVERIFIED**.
- Data residency: `inference_geo` exists (us/global); India region — **UNVERIFIED**.

## Security considerations

- Keep API key server-side; separate workspaces per environment with spend limits.
- Never pass raw card/bank numbers or full PII into prompts; tokenize references (DPDP minimisation).
- Treat model tool calls as untrusted proposals: Nirantar's policy engine must authorize every money-moving action (refund, retry charge, cancel mandate).

## Implementation notes for Nirantar

1. Pin model IDs in config: `claude-opus-5-5` (agentic back-office, dispute summaries), `claude-sonnet-5` (default copilot), `claude-haiku-4-5-20251001` only with a migration plan given its Oct-2026 retirement floor.
2. Use `output_config.format` JSON schema for all extraction/classification outputs; strict tools for actions. Don't use `tool_choice: any` with Opus 5.5.
3. Handle `enforced_spend_limit_reached` as a non-retryable outage → fail over to secondary LLM (Groq/Sarvam) and alert.
4. Use prompt caching for system prompts/tool definitions to stretch ITPM.

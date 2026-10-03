# ADR-0005: LLM and voice providers

- Status: Accepted · Date: 2026-09-28 · Traces: BB-§22, BB-§29 · Evidence: docs/integrations/{anthropic,groq,sarvam,exotel}.md

## Decision

- **LLM gateway is provider-neutral.** Agents request a *tier* (`strong`, `mid`, `fast`, `indic`) and a task; routing config maps tiers to model IDs. No agent hard-codes a model.
- Credentials available now: **Groq** and **Sarvam**. Anthropic is supported by the gateway but not configured until a key is provided.
- Default tier mapping (configurable, verified 2026-09-28):
  - `fast` → Groq `openai/gpt-oss-20b` (fallback `llama-3.1-8b-instant`)
  - `mid` → Groq `openai/gpt-oss-120b` (Claude Sonnet 5 when configured)
  - `strong` → Groq `openai/gpt-oss-120b` (Claude Opus 5.5 when configured)
  - `indic` → Sarvam `sarvam-105b`
- Claude Haiku 4.5 is only guaranteed until at least 2026-10-15, so it is **not** a default.
- Sarvam-M and Sarvam-30B are deprecated; the baseline's "Sarvam-30B" fallback is replaced by `sarvam-105b`.
- Voice: Exotel Voicebot stream (PCM16, 8 kHz) → Sarvam `saaras:v3` STT (`codemix`) → fast LLM turn → Sarvam `bulbul:v3` TTS. Starter-plan limit of 20 concurrent STT streams caps concurrent calls; the voice gateway enforces it.
- **Amendment (2026-09-28, measured):** `sarvam-105b` returned empty content with `finish_reason=length` even
  at `reasoning_effort=low` and 1,500 max tokens (19 s). `sarvam-105b-conversations` returned valid Telugu JSON
  in 1.1 s / 48 tokens. The `indic` tier therefore routes to `sarvam-105b-conversations` first; `sarvam-105b` is
  reserved for offline reasoning tasks with large budgets. The gateway reports "token budget exhausted" explicitly.
- Every LLM call has a deterministic fallback path (rules / templates) so money workflows never block on model availability.

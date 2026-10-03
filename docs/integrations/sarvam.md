# Sarvam AI — Integration Research (STT / TTS / Translate / LID / LLM)

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Sarvam page during this session.

## Source URLs (retrieved 2026-09-28)

- Docs index: https://docs.sarvam.ai/llms.txt
- API intro: https://docs.sarvam.ai/api-reference-docs/introduction
- Authentication: https://docs.sarvam.ai/api-reference/authentication.md
- Models catalogue: https://docs.sarvam.ai/api/getting-started/models
- Saaras (STT): https://docs.sarvam.ai/api/getting-started/models/saaras.md
- Bulbul (TTS): https://docs.sarvam.ai/api/getting-started/models/bulbul.md
- Streaming STT (WebSocket): https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/streaming-api
- Realtime streaming STT: https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming
- Streaming TTS (WebSocket): https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/streaming-api/web-socket
- Translate: https://docs.sarvam.ai/api-reference/text/translate-text
- Language identification: https://docs.sarvam.ai/api-reference-docs/text/identify-language (via official search snippet)
- Chat completion: https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview
- Rate limits: https://docs.sarvam.ai/api/getting-started/ratelimits
- **Pricing:** https://docs.sarvam.ai/api/getting-started/pricing
- Changelog: https://docs.sarvam.ai/changelog (not fetched)

## API version

- No global version header. Versioning is by **model id** (e.g., `saaras:v3`) and, for chat, by path (`/v1/chat/completions`, `/v2/chat/completions` beta).

## Authentication

- Header **`api-subscription-key: <KEY>`**; alternatively `Authorization: Bearer <KEY>` for OpenAI-compatible tooling.
- Base URL `https://api.sarvam.ai`.
- Auth failures return **HTTP 403** (not 401) with `error.code` e.g. `invalid_api_key_error`.
- WebSocket auth: docs say key is passed via the SDK's `api_subscription_key` parameter; the raw-WebSocket header/query mechanism is **UNVERIFIED**.

## Models (verified names)

| Capability | Model id(s) | Status / notes |
|---|---|---|
| STT | `saaras:v3` (default, recommended), `saaras:v4` (latest; Global English, keyterm prompting up to 50 terms), `saaras:v2.5` (deprecating) | Modes: `transcribe`, `translate`, `verbatim`, `translit`, **`codemix`** |
| Realtime STT | `saaras:v3-realtime`, `saaras:v4` on realtime endpoint | Streaming page calls it **beta**; realtime page doesn't — treat as beta |
| TTS | `bulbul:v3` (current), `bulbul:v2` (legacy) | 11 languages |
| Translate | `mayura:v1` (modes, `auto` source), `sarvam-translate:v1` (22 langs, formal only) | |
| LLM | `sarvam-105b` (reasoning, 128K ctx), `sarvam-105b-conversations` (dialogue/voice agents, 32K ctx) | **`sarvam-m` deprecated (removed from API); `sarvam-30b` deprecated → use 105B** |
| LLM (beta, open-weight via `/v2`) | GLM-5.3, Gemma 4 31B, DeepSeek V4 Flash (`glm5.3`, `gemma4`, `deepseekv4-flash`) | Beta |
| Saarika | Saarika model page still exists; current status (superseded by Saaras v3) — **UNVERIFIED** |

## Relevant endpoints (Nirantar scope)

- `POST /speech-to-text` (REST, ≤ 30 s audio; WAV, MP3, AAC, AIFF, OGG, OPUS, FLAC, MP4, AMR, WMA, WebM)
- `POST /speech-to-text-translate`
- Batch STT (≤ 2 h/file) — path **UNVERIFIED**
- **WebSocket `/speech-to-text/ws`** (full URL presumably `wss://api.sarvam.ai/speech-to-text/ws` — scheme/host **UNVERIFIED**): query params `model` (`saaras:v3`|`saaras:v4`), `mode`, `language_code` (BCP-47 e.g. `hi-IN`), `sample_rate` (`8000`|`16000`), `high_vad_sensitivity`, `vad_signals`. Audio: **WAV or raw PCM** (`pcm_s16le`, `pcm_l16`, `pcm_raw`) as base64. Responses with `vad_signals=true`: `speech_start`, `speech_end`, `transcript`.
- `/speech-to-text-translate/ws`
- **WebSocket `GET /speech-to-text-realtime/ws`** (recommended for voice agents): encodings `linear16`, `linear32`, **`mulaw`**, **`alaw`**; 8000 or 16000 Hz, mono; message `{"event":"audio_input","audio":"<base64>"}`; true partial transcripts; mid-stream reconfiguration; millisecond VAD; stream types `fast` (lowest latency) vs `balanced`.
- `POST /text-to-speech` (≤ 2,500 chars; sample rates 8/16/22.05/24 kHz, REST also 32/44.1/48 kHz; pace 0.5–2.0).
- TTS WebSocket (path **UNVERIFIED**): client messages `config`, `text` (1–2500 chars), `flush`, `ping`; output codecs mp3, wav, aac, opus, flac, **linear16, mulaw, alaw**; ≤ 24 kHz.
- `POST /translate` (mayura ≤ 1000 chars; sarvam-translate ≤ 2000 chars; `mode` formal|modern-colloquial|classic-colloquial|code-mixed; `output_script`; `numerals_format`).
- `POST /transliterate`
- `POST /text-lid` — returns `language_code` (e.g. `en-IN`) and script (e.g. `Latn`); exact script field name **UNVERIFIED**.
- `POST /v1/chat/completions` (sarvam-105b*), `POST /v2/chat/completions` (beta, OpenAI-compatible). Tool calling, JSON-Schema `response_format`, `reasoning_effort` (`low|medium|high`), SSE streaming.

Bulbul v3 voices (as listed): Shubh (default), Aditya, Ritu, Priya, Neha, Rahul, Pooja, Rohan, Simran, Kavya, Amit, Dev, Ishita, Shreya, Ratan, Varun, Manan, Sumit, Roopa, Kabir, Aayan, Ashutosh, Advait, Anand, Tanya, Tarun, Sunny, Mani, Gokul, Vijay, Shruti, Suhani, Mohit, Kavitha, Rehan, Soham, Rupali. (Speaker id casing in API — lowercase? — **UNVERIFIED**.)

## Supported languages

- STT (Saaras v3/v4): 23 — Hindi, Bengali, Kannada, Malayalam, Marathi, Odia, Punjabi, Tamil, Telugu, English, Gujarati, Assamese, Urdu, Nepali, Konkani, Kashmiri, Sindhi, Sanskrit, Santali, Manipuri, Bodo, Maithili, Dogri. Optimized for **8 kHz telephony** and multi-speaker audio.
- TTS (Bulbul v3), Chat, Mayura: 11 (10 Indian + English).
- Sarvam-Translate: 22 Indian + English.

## Webhook events

- None for synchronous/streaming APIs. Batch job callbacks — **UNVERIFIED**.

## Rate limits (per plan: Starter / Pro / Business)

- STT REST: 60 / 100 / 4,000 req/min; STT WebSocket: **20 / 100 / 100 concurrent**; Batch: 20 / 100 / 500 req/min.
- TTS REST: 60 / 200 / 1,000 req/min (bulbul:v3 Starter 30); TTS WebSocket: 60 / 200 / 1,000 concurrent (bulbul:v3 Starter 30).
- Translation & text: 60 / 200 / 1,000 req/min.
- Chat: default 60 / 200 / 1,000; **sarvam-105b 40 / 60 / 120 req/min**.
- 429 body: `{"error":{"message":"Rate limit exceeded","code":"rate_limit_exceeded_error"}}`; backoff on 429 and 503.

## Pricing (from pricing page, INR)

STT ₹30/hour (₹45 with diarization); TTS Bulbul v3 ₹30 per 10K chars; Translation ₹20 per 10K chars; LID ₹3.5 per 10K chars; Sarvam-105B ₹29.28 in / ₹73.2 out per 1M tokens. ₹100 free credits for new users. Streaming-specific pricing — **UNVERIFIED** (assumed same as STT).

## Idempotency behavior

- No idempotency keys documented — **UNVERIFIED**. Calls are stateless; safe to retry at the cost of re-billing.

## Retry behavior

- No provider-side retries (no webhooks). Client: exponential backoff on 429/503. WebSocket close 1000 = normal; 1006/1011 → reconnect with backoff; 4xxx (auth/quota) → do not auto-retry.

## Sandbox / test-mode behavior

- No sandbox; ₹100 free credits usable against production — **no separate test mode found (UNVERIFIED that none exists)**.

## Error model

- `{"error": {"message", "code"}}` with codes: `invalid_request_error`, `internal_server_error`, `unprocessable_entity_error`, `insufficient_quota_error`, `invalid_api_key_error`, `authentication_error`, `not_found_error`, `rate_limit_exceeded_error`, `model_call_error`, `gateway_timeout_error`, `billing_service_unavailable_error`. Auth errors use 403.

## Limitations

- Starter plan allows only 20 concurrent STT WebSocket streams → caps concurrent voice calls; Pro/Business both cap at 100 concurrent STT streams per published table.
- REST STT max 30 s; TTS REST 2,500 chars.
- TTS only 11 languages vs STT 23 → for 12 of the STT languages Nirantar cannot speak back in the same language.
- Model churn: sarvam-m / sarvam-30b already deprecated; saaras:v2.5 deprecating. Pin model ids and monitor changelog.

## Security considerations

- Keep the key server-side only (never in browser/mobile); keys shown once at creation.
- Audio of debtors/customers is personal data (DPDP Act) — data retention/processing location terms of Sarvam — **UNVERIFIED**; review DPA before sending call audio.

## Implementation notes for Nirantar

1. Voice pipeline over Exotel (8 kHz PCM16) → Sarvam realtime STT (`/speech-to-text-realtime/ws`, `linear16` @ 8000, mode `codemix` for Hinglish) → LLM → Bulbul v3 TTS WebSocket with `linear16` 8 kHz output to feed Exotel without resampling.
2. Use `/text-lid` or STT language detection to pick TTS language; fall back to Hindi/English when the detected language lacks Bulbul support.
3. Budget concurrency by plan; implement a call admission controller keyed to STT/TTS concurrent limits.
4. Pin `saaras:v3` (or v4 after evaluation) and `bulbul:v3`; do not reference `sarvam-m`.

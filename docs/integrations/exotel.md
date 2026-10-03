# Exotel — Integration Research (Voice, AgentStream/Voicebot, SMS)

Retrieval date for every source below: **2026-09-28**. Facts marked **UNVERIFIED** could not be confirmed from an official Exotel page during this session.

## Source URLs (retrieved 2026-09-28)

- AgentStream WebSocket protocol: https://developer.exotel.com/docs/agentstream/websocket-protocol
- Stream & Voicebot applet: https://developer.exotel.com/docs/agentstream/stream-voicebot-applet
- Stream/Voicebot extension guide (beta; via official search snippet): https://developer.exotel.com/docs/agentstream/stream-voicebot-extension and https://support.exotel.com/support/solutions/articles/3000132302
- Connect Two Numbers API: https://developer.exotel.com/api/make-a-call-api
- Outgoing call to a call flow (search snippet; page 404 on fetch): https://developer.exotel.com/api/outgoing-call-to-connect-number-to-a-call-flow
- Subdomains / credentials (search snippet): https://support.exotel.com/support/solutions/articles/3000023019-how-to-find-my-api-key-api-token-account-sid-and-subdomain-
- DLT compliance: https://developer.exotel.com/docs/faqs/dlt-compliance
- DLT registration / TRAI (search snippets): https://developer.exotel.com/docs/sms-support/dlt-registration, https://support.exotel.com/support/solutions/articles/3000096504

## API version

- Voice v1 REST: `/v1/Accounts/{account_sid}/...` (append `.json` for JSON; default XML). A "voice-v3" API family also exists in docs (e.g., https://developer.exotel.com/docs/voice-v3/api-reference/make-a-call) — its differences **UNVERIFIED**.

## Authentication

- HTTP Basic with `API_KEY:API_TOKEN` (from Dashboard API settings), plus `account_sid` in path.
- Subdomain: **Singapore cluster `api.exotel.com`** (default; account URL my.exotel.com) or **Mumbai cluster `api.in.exotel.com`** (account URL my.mum1.exotel.com). Use the cluster your account lives on.
- Voicebot WebSocket inbound auth to Nirantar: (a) Basic Auth embedded in URL `wss://<API_KEY>:<API_TOKEN>@host/path` → sent as `Authorization` header, or (b) IP whitelisting (arranged via hello@exotel.com).

## Relevant endpoints (Nirantar scope)

- `POST /v1/Accounts/{sid}/Calls/connect.json` — **Connect two numbers**: calls `From` first, then `To`; `CallerId` = ExoPhone. Optional: `Record` (bool), `RecordingChannels` (`single`|`dual`), `RecordingFormat` (`mp3`|`mp3-hq`), `StatusCallback`, `TimeLimit` (≤ 14,400 s), `WaitUrl`. **Rate limit 200 requests/min.**
- `POST /v1/Accounts/{sid}/Calls/connect.json` with `Url` = call-flow (app) URL — **Outgoing call to a call flow** (calls `From` and connects to an App/flow, e.g., one containing the Voicebot applet). This is the pattern for AI-agent outbound calls. Exact param set **UNVERIFIED** (page 404).
- `GET /v1/Accounts/{sid}/Calls/{CallSid}.json` — call details (recording URL field name, e.g. `RecordingUrl` — **UNVERIFIED**).
- SMS send API with `DltEntityId` / `DltTemplateId` (param naming appears as both `DltEntityId` and `dlt_entity_id` across pages — **exact casing UNVERIFIED**).

Call statuses: `queued`, `in-progress`, `completed`, `failed`, `busy`, `no-answer`. `Duration`, `Price`, `EndTime` populate asynchronously (~2 minutes after call end).

## Voicebot / bidirectional media streaming (AgentStream)

- Applets: **Stream** (one-way Exotel → server; transcription/monitoring) and **Voicebot** (bidirectional; conversational bots).
- URL config: static `wss://` URL or an HTTPS endpoint that returns the WSS URL dynamically. Up to **3 custom query params** (≤ 256 chars total).
- Exotel → server events: `connected`, `start`, `media`, `dtmf` (Voicebot only), `mark` (Voicebot only), `stop` (`reason`: `stopped` | `callended`).
- Server → Exotel events: `media` (audio to play), `mark` (position tag), `clear` (flush buffered audio — used for **barge-in**).
- Audio: raw/slin **PCM linear16, 16-bit signed little-endian, mono, base64**. Default **8000 Hz**; supported **8000 / 16000 / 24000 Hz** via `?sample-rate=16000` on the URL. Exotel sends ~every 100 ms.
- Outbound chunks: **3,200–100,000 bytes, multiple of 320 bytes** (to avoid playback gaps).
- Conflict noted: one official search snippet says default is mu-law 8 kHz configurable via `MediaFormat` (`audio/x-mulaw;rate=8000`, `audio/L16;rate=8000`, `audio/L16;rate=16000`) — this appears to be the older/extension (beta) guide. The current protocol page states PCM linear16 default. **Confirm with Exotel on the account in use.**
- Concurrency limits per account for streams — **UNVERIFIED**. Recording while streaming — **UNVERIFIED**.

## Webhook events

- `StatusCallback` on call APIs (HTTP POST/GET to Nirantar URL with call status). Payload fields and `StatusCallbackEvents` options — **UNVERIFIED**.
- **Signature verification for Exotel callbacks: none documented — UNVERIFIED.** Nirantar must authenticate callbacks another way (secret path token + IP allowlist + re-fetch call details via API).

## Rate limits

- Connect-two-numbers: 200 req/min. Other APIs — **UNVERIFIED**.

## Idempotency behavior

- No idempotency key documented — **UNVERIFIED**. Retrying a call-create can place a second call. Nirantar must guard with its own dedupe (one in-flight call per attempt id; use `CustomField` to tag calls — `CustomField` support **UNVERIFIED** for connect API).

## Retry behavior (provider-side)

- StatusCallback retry policy — **UNVERIFIED**. Assume no retries; reconcile via call-details API.

## Sandbox / test-mode behavior

- No sandbox documented — **UNVERIFIED**. Trial accounts typically restrict calling to verified numbers (**UNVERIFIED**).

## Error model

- REST errors in XML/JSON with HTTP status; body structure (`RestException` with `Status`, `Message`, `Code`?) — **UNVERIFIED**.

## India regulatory notes

- **SMS (TRAI TCCCPR, DLT):** Entity registration (PAN, GST, authorization) → Entity ID; Header/Sender ID registration (6-char alpha or 6-digit numeric); every message body registered as a template with `{#var#}` variables; categories Transactional, Promotional, Service Implicit, Service Explicit; operator scrubs **character-by-character** — any mismatch fails delivery; unregistered entity/header → SMS will not be delivered in India. Entity ID configurable in Dashboard (Settings → SMS DLT Settings) or per request. Approval typically 3–7 business days.
- DLT does not apply to voice calls or WhatsApp (per Exotel docs).
- **Voice:** TRAI rules for commercial/telemarketing voice calls (140/160-series numbering, NCPR/DND scrubbing, calling-hour limits for collections under RBI Fair Practices Code) — **UNVERIFIED** from Exotel docs in this session; legal review required before automated collection calls.
- Call recording consent/disclosure requirements — **UNVERIFIED**; play a recording/AI disclosure prompt as a baseline.

## Limitations

- Default audio is narrowband 8 kHz — STT accuracy depends on telephony-optimized models (Sarvam Saaras is optimized for 8 kHz).
- Callback authenticity cannot be cryptographically verified (no documented signature).
- Billing/duration data is eventually consistent (~2 min).

## Security considerations

- Use `wss://` only; prefer IP whitelisting + Basic Auth (credentials in the WS URL can leak into logs — use a dedicated low-privilege credential for the WS endpoint, not the account API token, if Exotel permits; **UNVERIFIED**).
- Treat call audio and recordings as sensitive personal data; restrict recording URL access; set retention.

## Implementation notes for Nirantar

1. Outbound AI call = Calls/connect with `Url` of an Exotel flow containing the Voicebot applet pointing to Nirantar's voice gateway (`wss://.../exotel?campaign=..&attempt=..`).
2. Voice gateway: decode base64 PCM16 8 kHz → Sarvam realtime STT (`linear16`, 8000); TTS Bulbul v3 at 8 kHz `linear16` → re-chunk to multiples of 320 bytes (≥ 3,200 bytes) → `media` events; send `clear` on barge-in; use `mark` to track playback completion.
3. Throttle call creation ≤ 200/min; enforce calling-window and DND policy in Nirantar's contact-policy engine before dialing.
4. Reconcile outcomes by polling call details after `stop` rather than trusting callbacks alone.
5. For SMS reminders, register DLT templates identical byte-for-byte to Nirantar's rendered strings; store template id alongside each template version.

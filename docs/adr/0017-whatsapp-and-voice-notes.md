# ADR-0017: Real channels: WhatsApp Cloud API and Sarvam voice notes (Phase 2 · P6)

- Status: Accepted (SMS on hold; phone calls deferred) · Date: 2026-10-02
- Evidence: `tests/e2e/test_whatsapp_channel.py` exercises the channel against a faithful fake Graph API.
- Live checks against the real account on 2026-10-02 (approved by the product owner):
  - Graph API v23.0 reads succeed on the connected test number (quality GREEN).
  - The `hello_world` template sent to the owner's verified number was accepted.
  - 15 Nirantar templates were submitted for Meta review (PENDING at submission).
  - Sarvam text → MP3 speech → text round trip, word for word: Hindi in 405 ms, Telugu in 283 ms.

## Decisions

1. **WhatsApp Cloud API** (`channels/whatsapp.py`, Graph v23.0): text, templates, audio, media upload and download,
   template management.
   - Webhooks must be verified: the GET handshake with the verify token, and `X-Hub-Signature-256` HMAC over the raw
     body with the app secret.
   - Credentials come from the environment and are never logged. Tests strip `WHATSAPP_*` unless
     `NIRANTAR_LIVE_WHATSAPP=1`, so the suite can never message a real number.
2. **The 24-hour rule is enforced in code** (`channels/sink.py`).
   - Inside the customer's service window, the gateway-checked free text is sent.
   - Outside it, only the **Meta-approved template** for the same registry key is sent, with the same facts as
     parameters.
   - With neither, the sink refuses with `TemplateNotApproved` and the gateway records the action as failed. Nothing
     is silently dropped or faked.
   - Parameters are filled in each language's own placeholder order (Hindi puts the plan before the amount).
3. **Templates derive from the template registry** (`channels/wa_templates.py`): the registry text with
   `{placeholders}` becomes `{{n}}`.
   - Meta forbids a trailing variable, so those bodies get a closing line ("Thank you." / "धन्यवाद।" /
     "ధన్యవాదాలు.").
   - `status` syncs approval state into `config.whatsapp_templates`. `submit` only submits what Meta does not
     already have.
   - The 5 keys:
     - `recovery`, `predebit_notice`, `mandate_reauth` and `mandate_resume` are UTILITY.
     - `winback` is MARKETING, and still needs promotional consent.
4. **Mandatory pre-debit notice without SMS.**
   - The notice goes by SMS when the deployment has it (DLT); otherwise it goes by WhatsApp template.
   - The Compliance Guardian checks the same channel.
   - SMS is **on hold** by the owner's decision: DLT registration needs a registered business. The sink refuses SMS
     with a clear reason.
5. **Inbound** (`channels/inbound.py`, `POST /webhooks/whatsapp`):
   - **Routing:** the sender's number is matched, by peppered hash (never the number itself), to the
     tenant/customer we last messaged. Unknown numbers are not routed and nothing is stored.
   - **Deduplication:** on the provider message id, because Meta retries.
   - **Voice notes:** Sarvam speech-to-text (OGG/Opus accepted directly, codemix, the customer's language). OTPs and
     PINs read out are redacted before storage, and the audio is kept as evidence.
   - **Images:** run through the payment-screenshot evidence pipeline (OCR, tamper signals, provider cross-check;
     never authoritative).
   - **Documents:** stored as evidence.
   - **"STOP" in en/hi/te:** opts the customer out of WhatsApp immediately, whatever case is open.
   - Then `reply.received` → event bridge → the open DebitCycle.
   - **Delivery statuses** update `comms.messages` and never move backwards.
6. **Acknowledgement** (`comms.acknowledge_reply`, called by `record_reply`): an in-window reply in the customer's
   language saying what was understood.
   - It covers a promise with its date, hardship ("a team member will contact you"), opt-out confirmation, or a
     general acknowledgement.
   - **If the customer sent a voice note, Nirantar also answers with a spoken reply** (Sarvam TTS → MP3 → WhatsApp
     audio).
   - Policy: answering a customer-initiated message (`reply_acknowledgement`, `optout_confirmation`) is not
     outreach, so optional-contact rules (windows, fatigue) do not block it.
7. **Voice = WhatsApp voice notes, in both directions.** Live phone calls (LiveKit + SIP) are deferred by the
   owner's decision. The existing call engine stays for when a SIP trunk exists.

## Consequences / limits

- **Inbound needs a public HTTPS URL.** Meta must reach `/webhooks/whatsapp` (P8: domain or tunnel). Until then,
  inbound is proven with signed webhook payloads in tests. Outbound works now.
- **Test-number limits.** It delivers only to recipients verified in the Meta app; production needs the merchant's
  own number, verified business and display name.
- **One WhatsApp number per deployment for now** (env configuration). Per-merchant numbers (Embedded Signup) come
  with P8 onboarding. The routing table already keys by tenant.
- **Only en/hi/te.** The template registry and the Meta templates cover English, Hindi and Telugu. Sarvam can speak
  and understand 11 Indian languages; adding a language = registry texts + `submit`.

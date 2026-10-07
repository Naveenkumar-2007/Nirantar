# ADR-0026: Live voice recovery calls — Exotel + Sarvam, signed call tokens, outcomes that act (P11)

- Status: Accepted · Date: 2026-10-07
- Brief direction: "Hinglish voice recovery".
- Evidence:
  - `tests/e2e/test_voice_calls.py` (Exotel faked; tests can never dial: `EXOTEL_*` is stripped unless
    `NIRANTAR_LIVE_VOICE=1`):
    - an unregistered business is refused by policy, and nothing is dialled;
    - a registered one places the call with only a signed token;
    - a forged-token stream is closed before a word is spoken;
    - the stream's injected "merchant / amount" is ignored in favour of server-side context;
    - the recording disclosure comes first;
    - "haan bhai, kal pay kar dunga" creates a promise for tomorrow (source voice), sends a WhatsApp link for the
      exact amount, and stores an encrypted transcript;
    - Exotel's terminal callback marks the call completed with its duration.
  - `tests/unit/test_voice.py` (existing, still green): chunking to protocol, end-of-utterance, barge-in, OTP
    redaction, hardship transfer.
  - Live credentials: Exotel account API returned 200 (Trial, active). No live call was placed: Exotel must reach
    the gateway over a public WebSocket, which comes with go-live.

## Decisions

1. **The conductor's voice arm is real.** When arbitration chooses voice, `comms.place_call` runs through the
   gateway (`voice_call`): voice consent, opt-out, the contact window, fatigue, and **a registered calling header
   (TRAI)**. The registered header is off until the business records it.
2. **Exotel "connect to flow".** Exotel dials the customer from the ExoPhone and runs the App whose Voicebot applet
   streams 8 kHz audio to `/voice/exotel`.
3. **Signed call token, server-side context.**
   - The call carries only `tenant.call.keyed-hash`.
   - The gateway resolves the business name, the amount due and the language from `comms.calls` and the debit.
     Stream parameters are ignored.
   - An invalid token closes the socket with code 1008.
4. **Outcomes act.**
   - Promise to pay: an `ops.promises` row with the extracted date, plus the payment link on WhatsApp right away.
     That counts as customer-requested.
   - Hardship: an escalated case for a person.
   - Opt out: `voice` is added to the customer's opt-outs.
   - The transcript is already OTP-redacted by the turn engine, then encrypted with the business's key.
5. **Exotel status webhook** `/webhooks/exotel/{business}`. Exotel does not sign it, so it can only move a call
   Nirantar placed in that business to a terminal status. It never creates calls or touches money.
6. **Edge.** `/voice/*` is public via Caddy (WebSocket) and protected by the token.

## Consequences

- To go live the founder needs to:
  - set `EXOTEL_CALLER_ID` (the ExoPhone) and `EXOTEL_FLOW_ID` (an App with a Voicebot applet pointing at
    `wss://<domain>/voice/exotel`, passing the call's CustomField as `token`);
  - record the registered calling header for each business.
  - Trial accounts call only verified numbers.
- The dialogue is the scripted, compliant v1: disclosure, amount, ask, confirm, in the business's approved
  scripts. LLM-driven free conversation on money calls stays off until agent evals cover it.

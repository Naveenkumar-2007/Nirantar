# ADR-0020: Design system, Customer 360 and the Conversations inbox (Phase 2 · P8.3)

- Status: Accepted · Date: 2026-10-03
- Evidence:
  - `tests/integration/test_api.py::test_customer_360_hides_contact_details_and_respects_tenancy`
  - `tests/e2e/test_whatsapp_channel.py`: the inbox reads back the template (status `read`), the Sarvam voice-note
    transcript with the OTP redacted and its evidence id, and free text — while the stored bytes are ciphertext.
  - Browser check, signed in through Keycloak on the demo business: Overview, Debits, Customers, Customer 360 and
    the Conversations empty state in light and dark themes; `tsc` and `eslint` clean.

## Decisions

1. **One token set, shadcn names.** `globals.css` defines background/card/primary/muted/border plus reserved status
   tokens (`success`, `warning`, `danger`, `info`, each with `-soft`). Legacy ad-hoc variables were removed by a
   codemod so there is a single source of colour. Components come from shadcn 4 (radix base, nova preset) and
   live in `components/ui`; product-level pieces (PageHeader, Card, Stat, Badge, Table, Empty) in `components/kit`.
2. **Charts use a validated categorical palette** (`--series-1..8`), separately chosen for light and dark and
   checked for colour-blind separation and contrast. Hues are assigned in fixed order, never cycled. Bars are thin
   with values written in text ink.
3. **Status is never colour alone.** Badges carry a shape mark (● ■ ▲ ◆ ○) and a word; message delivery shows an
   icon and a word.
4. **Shell:** collapsible inset sidebar (state in a cookie, so no layout flash), business switcher, user menu with
   light/dark/system theme (`next-themes`), toasts (`sonner`). The CLI (`shadcn`) is a dev dependency only; the
   production audit is clean.
5. **Customer 360** (`GET /v1/customers/{id}`) assembles profile, consent, subscriptions with their mandates,
   debits, cases, contacts, agent actions with policy decisions and recorded replies. Phone and email are never
   returned — only whether they exist.
6. **Conversations inbox** (`GET /v1/conversations`, `/v1/conversations/{customer_id}`). Migration 0022 adds
   `comms.messages.body_enc` (AES-GCM with the business's data key) and `evidence_id`. Outbound text is stored on
   send; inbound text, or the redacted transcript of a voice note, on receipt. The thread shows whether the 24-hour
   reply window is open (last inbound + 24 h); outside it only approved templates can be sent.

## Consequences

- Messages sent before migration 0022 have no stored body and show "(no text)".
- The demo business uses the mock channel, so its inbox is honestly empty; the inbox fills from real WhatsApp use.
- Replying from the inbox (human takeover) is not built yet; it will go through the ToolGateway like agent sends.

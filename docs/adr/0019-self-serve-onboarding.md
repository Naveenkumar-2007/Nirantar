# ADR-0019: Self-serve onboarding, merchant-owned secrets, team invites (Phase 2 · P8.2)

- Status: Accepted · Date: 2026-10-03
- Evidence:
  - `tests/integration/test_onboarding.py`: keys verified then encrypted, webhooks verified with the generated
    secret, live keys refused outside production, import gating and start, invites claimable only with a verified
    email.
  - A live run on the real stack (Keycloak user → API → Razorpay → Temporal worker → lakehouse):
    - a business was created;
    - the owner's Razorpay test keys were verified **live with Razorpay** and stored encrypted;
    - the OnboardingWorkflow imported the Razorpay test account (1 billing cycle) through the always-on worker;
    - readiness came back honestly as "0 of 6 models" with each gap;
    - `/setup` and `/settings/team` rendered all of it for the signed-in owner.

## Decisions

1. **Merchant secrets belong to the merchant** (`core.tenant_secrets`, migration 0021).
   - Each is encrypted with the business's own data key (`core.crypto`), under RLS.
   - Accounts store references (`tenant:<id>`); the `resolve_secret` scheme needs the tenant and the engine.
   - Replacing a credential retires the old row instead of overwriting it.
   - Values never leave the API. The generated webhook secret is shown once.
2. **Connect Razorpay = verify, then store.**
   - The key format is checked.
   - Keys are confirmed with an authenticated read against Razorpay; a rejection is reported plainly.
   - Mode comes from the key prefix. Live keys are accepted only when `NIRANTAR_ENV=production`.
   - The answer gives the merchant's webhook URL, a fresh webhook secret and the event list to enable.
   - The audit records `provider.connected`.
3. **The checklist is computed from real state**, never stored as "done" flags:
   - **business:** the tenant;
   - **payments:** `core.provider_accounts`;
   - **messaging:** the deployment's channel;
   - **history:** the OnboardingWorkflow outcome;
   - **readiness:** data-health readiness per model.

   Import is refused until payments are connected. It starts the existing OnboardingWorkflow on the always-on
   worker (`onboarding:<tenant>`, `USE_EXISTING` while running, re-runnable after).
4. **Fix: onboarding state is merged.** It used to be overwritten, which wiped the payments verification when the
   import finished.
5. **Team invites** (`core.invites`).
   - An owner invites by email with one of the existing roles. The address is stored only as a peppered hash.
   - Invites are **claimed at sign-in only when the identity provider says the email is verified.** Otherwise
     anyone could register the invited address and take the seat.
   - Invites are single use and expire after 7 days.
6. **The dashboard** adds:
   - `/setup` (the wizard, which auto-refreshes while the import runs);
   - `/settings/team`;
   - `/choose-business`;
   - a sidebar business switcher. The chosen business is kept in the encrypted session and re-checked against the
     API on every switch.
   - A new business goes straight to its setup.
7. **Fix: every entry point loads `.env` through one helper** (`core/dotenv.py`). The services process previously
   ran without the deployment's configuration (no WhatsApp/Sarvam), silently falling back to `UnconnectedSink`.

## Limits / next

- **No email delivery for invites yet.** The owner tells the person to sign in. Production Keycloak needs SMTP and
  `verifyEmail=true` so that self-registered emails become verified.
- **Shared WhatsApp number.** Messaging uses the deployment's number. Per-merchant numbers come through Meta
  Embedded Signup.
- **Razorpay webhooks need a public URL** (P8.4). Until then, payments arrive through import and the 30-minute
  reconciliation.

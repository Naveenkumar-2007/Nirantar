# ADR-0018: People sign in: OIDC with Keycloak, businesses and memberships (Phase 2 · P8, step 1)

- Status: Accepted · Date: 2026-10-03
- Evidence:
  - `tests/integration/test_oidc_identity.py`: signature, iss, aud, exp and tamper checks; business creation;
    tenant selection; isolation between people; API keys alongside.
  - A live end-to-end run against the real stack. The dashboard redirects to `/login`. Keycloak 26.8.0 serves its
    login page with PKCE S256, state and nonce. Credentials are accepted, the callback runs, and the encrypted
    session cookies are set. The API verifies the real Keycloak token against the live JWKS, and a person with no
    business lands on onboarding, greeted by name.

## Context

The dashboard used server-side API keys from `.env.local`: whoever could open it acted as that key's role in one
tenant. That is why it had to stay off the internet, and why inbound WhatsApp, real MCP connectors and
self-onboarding were blocked.

## Decisions

1. **Identity provider: Keycloak 26.8** (official image, Apache-2.0), running in compose with its own Postgres
   database.
   - The realm is code (`infra/keycloak/realm-nirantar.json`), and secrets are injected from env placeholders at
     import.
   - Realm settings: self-registration on, email as username, password policy, brute-force protection, refresh-token
     rotation with no reuse, 10-minute access tokens, `fullScopeAllowed` off.
   - Plan v1 named Zitadel. Keycloak was chosen because the whole realm (clients, mappers, policies) imports
     declaratively from one versioned file, it is the most deployed open-source OIDC server, and its admin REST API
     makes test setup reproducible.
2. **Dashboard = OIDC relying party**, using `openid-client` 6 (certified) and `jose` 6, as the Next.js 16
   authentication guide recommends.
   - The flow is authorization code + PKCE S256 + state + nonce, with a confidential client.
   - Sessions are **stateless and encrypted** (JWE dir/A256GCM, httpOnly, SameSite=Lax). They are split into an
     access cookie and a refresh cookie, each under the 4 KB limit.
   - Token refresh runs in `proxy.ts`, which runs on Node in Next 16, before rendering, and it updates the request's
     cookies too.
   - `returnTo` accepts only same-site paths. Sign-out revokes the refresh token and ends the IdP session.
   - Pages live in an `(console)` route group; `/login` and `/onboarding` are full-page screens outside it.
3. **The API verifies tokens locally:** RS256/ES256 signature against the issuer's JWKS (discovered, cached,
   refreshed on rotation), plus `iss`, `aud=nirantar-api` (audience mapper), `exp` and `typ=Bearer`.
   - API keys (`nk_…`) still work for services and scripts.
4. **Roles come from Nirantar, not from the token.** `core.user_memberships` (user × business × roles) is decided
   by each business's owners, so an identity-provider admin cannot grant tenant power.
   - A person can belong to several businesses and picks one with `X-Nirantar-Tenant`.
   - The API answers 409 with `no_business` or `choose_business`, which the dashboard turns into onboarding or a
     chooser.
   - `core.tenants` is RLS-protected, so business names are read inside each business's own scope (the first version
     joined across tenants and saw nothing; a test caught it).
5. **Self-onboarding:** `POST /v1/businesses` creates a business with the caller as owner, audited as
   `business.created`. The limit is 10 businesses per person. The operator command `nirantar.security.members`
   attaches existing (seeded) businesses to an account.

## Limits / next

- **Local only for now.** Production needs `KC_HOSTNAME` on HTTPS and `start` instead of `start-dev`. The
  `nirantar-e2e` password-grant client must be disabled (`NIRANTAR_E2E_CLIENT_ENABLED=false`).
- **Not built yet:** team invites, a business chooser in the UI, MFA policy, and social login. These are the next P8
  steps, together with the onboarding wizard: connect Razorpay, then WhatsApp, then history import.
- **One part not driven in a real browser yet.** The final "Create business" click runs as a server action, which a
  script without JavaScript cannot submit. Its API is covered by tests.

# Security controls and scan policy

How Nirantar's security is enforced, and what CI checks on every change (`.github/workflows/ci.yml`).

## Enforced in code

| Control | Where |
|---|---|
| Tenant isolation | Postgres row-level security, `FORCE`d even for the table owner; the API connects as `nirantar_app` (no `BYPASSRLS`) and every query runs inside `tenant_tx` |
| Every side effect is gated | `ToolGateway.call`: scope → schema → dedupe → policy → approval → execute → audit |
| No duplicate actions | idempotency keys, plus an advisory lock and a 10-minute `executing` lease per key |
| Money only after provider truth | ledger settles only after `verify_capture`; payment-page callbacks are signature-checked, then re-fetched from the provider |
| Webhooks | provider signature verification, raw events stored once (replays are no-ops) |
| Conduct | template registry and conduct screen (RBI fair-practice wording), contact windows, fatigue limits, consent and opt-out |
| LLM inputs | `security/guardrails.py`: NFKC normalisation, hidden/bidirectional character removal, PII redaction, injection detection, fencing |
| Agents | import contracts: agents can never import provider adapters or the database (`lint-imports`) |
| Demo isolation | `core/demo.py`: mock payments only, mock messages, voice off; refuses to start with real credentials |
| Secrets | never in git (`.env` ignored, gitleaks on full history); tenant secrets encrypted with `DATA_KEYS`; API keys peppered and hashed |

## CI scan policy

- **gitleaks**: whole history, any finding fails the run.
- **bandit** (`-ll -ii`: medium-or-higher severity *and* confidence). Six low-confidence `B608` hits were reviewed
  by hand on 2026-10-07 and are false positives, because every interpolated fragment is a fixed string chosen in code
  and all values are bound parameters:
  - `api/queries.py` (3): fixed `WHERE` fragments and the keyset cursor clause.
  - `data/bronze.py`: table names come from the `CDC_SOURCES` constant.
  - `db/migrations/versions/0010_data_platform.py`: table names come from the `CDC_TABLES` constant.
  - `db/stores.py`: column names come from a fixed tuple.

  `B613` (bidirectional characters in source) was a real finding and is fixed: `guardrails.py` now writes them
  as escapes.
- **pip-audit**: `PYSEC-2026-3740` (nltk, pulled in by `evidently` for drift reports, never on a request path) is
  allowed by ID until a fixed release exists. Any other advisory fails the run.
- **npm audit**: production dependencies, high and above.
- **trivy**: every image; fixable HIGH and CRITICAL findings fail the run. Images must not run as root.

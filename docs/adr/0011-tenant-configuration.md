# ADR-0011: Per-tenant configuration, template registry and learned decision parameters (Phase 2 · P1)

- Status: Accepted · Date: 2026-09-29
- Evidence: tests/unit/test_settings.py, tests/integration/test_tenant_config.py, tests/integration/test_api.py,
  live browser verification (/automations, /automations/templates)

## Problem

Business numbers were constants in code: contact effects and costs (`contact_arbiter`), the M1 risk threshold
(`HIGH_RISK = 0.35`), channel capacity, holdout size, message/voice wording, LLM routes, and every tenant used
`TenantPolicyConfig()` defaults. A real B2B product needs each business to own these values, see where they came
from, and have them improve from its own outcomes.

## Decisions

1. **Versioned settings** (`config.settings_versions`, append-only, RLS): namespaces `channels`, `strategy`,
   `effects`, `experiments`, `policy`. Each change stores the full document as a new version with author and
   reason, writes a hash-chained audit record and emits `config.settings_changed` (outbox), in one transaction.
   Optimistic concurrency: stale edits get 409. Starting values come from `settings/defaults/platform.yaml`
   (version 0), which is labelled as placeholders/assumptions, not facts.
2. **Policy stays regulation-first**: the `policy` namespace holds only tightenings, validated by
   `TenantPolicyConfig.tightened` (loosening, or unknown keys, are rejected). The MCP gateway now loads each
   tenant's policy by default.
3. **Template registry** (`config.templates`): propose → a different person approves (checked in code AND by a
   table CHECK) → previous live version retired (unique partial index: one live version per key+language).
   Versions are immutable (trigger). Proposals must pass automated checks: required/unknown placeholders, no
   literal money amounts, conduct rules (plain-language reasons), script check for hi/te, no no-op or duplicate
   proposals. Runtime resolution: tenant approved (language) → platform default (language) → the same for English.
   Agents read wording through the MCP tool `content.get_template` (they still never touch the DB); every sent
   message records its `template_ref`.
4. **Learned parameters** (`config.learned_params`, append-only, stored with evidence):
   - contact effects per failure category: ITT from the randomised holdout ÷ contact rate (Wald/CACE),
     shrunk towards the tenant's prior; small groups keep the prior; with two channels in one category the
     split follows the prior ratio (stated in the evidence);
   - M1 risk threshold: cost-sensitive argmax over verified labels, with minimum label counts.
   Runtime reports the source of every value (`learned:vN (k/6 categories)`,
   `prior:insufficient_evidence`, `fallback:insufficient_evidence`, `fixed`) and never claims "learned" without
   evidence.
5. **LLM routing** moved from code to `settings/defaults/llm_routing.yaml` (override with `NIRANTAR_LLM_ROUTING`).
6. **Product surface**: Automations page (in-force values and their sources, per-namespace forms with reason,
   learned evidence, re-learn button) and Message templates page (propose/review with maker-checker).

## Known limits (honest)

- Learning runs on demand (API/button) and after the demo seed; scheduled runs arrive with the data platform (P2/P3).
- The threshold is chosen in-sample on past outcomes; `prevention_share` is an assumption until a pre-debit
  experiment measures it.
- Reply-intent and conduct detection are still rule-based (M8/M12 trained models are P3).
- The demo tenant (150 customers) has too little evidence to learn anything; the UI says so.
- The dashboard still uses server-side service keys; per-user login (OIDC) is P8.

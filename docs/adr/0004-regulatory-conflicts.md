# ADR-0004: Resolving conflicts between the product baseline and regulation

- Status: Accepted (engineering position; **not legal advice** — counsel review required before any "compliant" claim)
- Date: 2026-09-28 · Traces: BB-§4, BB-§21, gap-report C1–C5 · Evidence: docs/compliance/policies.yaml v0.2.0

| # | Baseline assumption | Finding | Decision |
|---|---|---|---|
| a | L2 may shift the debit to after predicted salary day | A shift needs a fresh ≥24h pre-debit notice, a non-peak execution slot, remaining retry budget and mandate headroom (RBI E-mandate Framework 2026; NPCI OC-215/215A partially verified). For loans, debiting after the due date creates DPD | Debit shift is a **policy-gated action**. The policy engine computes the earliest legal date deterministically; lending tenants must explicitly allow it; the LLM never chooses dates |
| b | 8am–7pm window applies to lending only | NBFC RBC Directions 2025 para 100: 08:00–19:00; microfinance 09:00–18:00; 2026 amendment unconfirmed. No verified clock rule for non-lending, but TRAI preferences apply | Contact windows come from policy records per tenant segment; **platform default window applies to all tenants**; tenants may only tighten |
| c | Voice agent calls for subscription recovery | Service robo-calls need registered 1600-series headers and pre-declaration; any offer makes it promotional (140-series + preference checks) | Subscription voice is **service-only scripts** by default; offer scripts require promotional-consent checks. Voice is disabled for a tenant until telephony registration is recorded in its config |
| d | Always-on 5–10 % holdout (BB-§21) | For lending, withholding required notices is not acceptable; fair-treatment and bureau risks | Holdout **off by default for lending tenants**; requires per-lender opt-in, and only ever withholds *optional* interventions. Mandatory notices, agent disclosures, grievance and hardship responses are never withheld. Stop rule on harm metrics. Non-lending tenants keep the 5–10 % default |
| e | Store call recordings | Allowed with disclosure at call start, India-only storage, tenant isolation; DPDP Rules r.8(3) one-year minimum retention for processing logs | Record only after the disclosure is played and consent/notice is logged; retention policy per tenant with a one-year floor on logs |
| f | Cross-tenant model training | Conflicts with NBFC outsourcing isolation (paras 26/27/70), lending data limits, DPDP purpose limitation | **Per-tenant models by default.** Global models are trained only on RecurSim and licensed public data. Pooling customer data across tenants is prohibited without a new ADR, contract basis and legal sign-off |
| g | Lending collections in production | 2026 recovery directions not confirmed on a primary source | **Launch gate**: lending collections can run in RecurSim and sandbox only until counsel confirms the final directions (policy IN-RBI-RBC-RECOVERY-2026AMEND-001) |

All values marked partially verified / unverified in `policies.yaml` are loaded as configuration tied to their `policy_id`, and tenants may only make them stricter.

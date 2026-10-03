# Nirantar Regulatory Knowledge Base (India)

- **KB version:** 0.2.0
- **Retrieved:** 2026-09-28
- **Machine-readable records:** [`policies.yaml`](./policies.yaml)
- **Source register:** [`sources.md`](./sources.md)

> **Not legal advice.** This is an engineering research snapshot (BB-§4, BB-§48). No capability may be called "compliant" until counsel has reviewed it (BB-§49).
>
> Records marked `partially_verified` or `unverified` are loaded as **configurable, tenant-tightenable parameters** carrying their `policy_id`. They are never hard-coded.

**Positioning.** Nirantar is an orchestration and recovery platform that integrates with licensed PAs. It is **not** an RBI-authorised Payment Aggregator. Most payment-rail obligations fall on issuers, acquirers, PAs and lenders, and reach Nirantar through contracts. Nirantar's job is to make it impossible for its agents to cause a tenant to breach them.

**Totals:** 38 records: 24 verified, 12 partially verified, 2 unverified.

## 1. Summary table

| policy_id | Rule (short) | Effective | Status | Nirantar enforcement point |
|---|---|---|---|---|
| IN-RBI-EMANDATE-SCOPE-001 | E-mandate Framework 2026 consolidates and repeals the 2019-24 circulars. Covers cards, PPI and UPI only (not NACH). AFA needed for register, modify, withdraw | 2026-04-21 | verified | `rbi_emandate_2026` rule-pack chosen by `mandate.rail` |
| IN-RBI-EMANDATE-PREDEBIT-001 | Issuer's pre-debit notice ≥24h before debit, stating merchant, amount, date/time, mandate ref, reason | 2026-04-21 | verified | DebitCycle timer; `emandate.predebit_notice_lead_ok`; any date or amount change voids the notice |
| IN-RBI-EMANDATE-AFA-001 | No AFA needed up to ₹15,000 per txn. Up to ₹1,00,000 for insurance premium, MF subscription, credit-card bill only | 2026-04-21 | verified | `emandate.afa_threshold`; above the limit, route to an AFA payment link |
| IN-RBI-EMANDATE-OPTOUT-001 | Customer can opt out of one txn or the whole mandate, and modify or withdraw it at any time; customer-set cap | 2026-04-21 | verified | `emandate.no_debit_after_optout`, `emandate.within_customer_cap` |
| IN-RBI-EMANDATE-POSTDEBIT-001 | Post-debit notice includes grievance details | 2026-04-21 | verified | Template lint `comms.includes_grievance_contact` |
| IN-RBI-EMANDATE-NOCHARGE-001 | No charge to customers for the e-mandate facility | 2026-04-21 | partial | Fee-grid validator |
| IN-NPCI-UPIAUTOPAY-EXEC-001 | AutoPay executions only in non-peak hours (peak = 10-13, 17-21:30). Max 1 attempt + 3 retries per sequence | 2025-08-01 | partial (NPCI PDF blocked) | `upi_autopay.nonpeak_window`, `attempts_per_seq<=4`; M3 bandit is clipped to these |
| IN-NPCI-UPIAUTOPAY-LIFECYCLE-001 | View, port, revoke, pause, modify; UPI PIN for payer actions; no inducements to port; port once per 90 days; MIC | 2025-12-31 (circular 2025-10-07) | verified | Lint `upi_autopay.no_port_inducement`; PAUSED/REVOKED block debits |
| IN-NPCI-NACH-SCOPE-001 | NACH presentation, return and re-presentation rules come from NPCI and the sponsor bank | UNVERIFIED | unverified | `npci_nach` pack from tenant sponsor-bank parameters; missing value returns REQUIRE_MORE_INFORMATION |
| IN-GOI-PSSA-S25-001 | Dishonour of an EFT for insufficient funds can be an offence; written demand notice | UNVERIFIED | partial | `collections.legal_notice` is always REQUIRE_APPROVAL; no threats |
| IN-RBI-CARD-DATASTORE-001 | Card credentials never in merchant-accessible storage; PCI-DSS/SSF | 2025-09-15 | partial | `pci.no_pan_storage` (Luhn redaction) |
| IN-RBI-DLD-2025-RECOVERY-001 | Recovery agent's details sent to borrower by email/SMS **before** first contact; LSPs acting as recovery agents disclosed | 2025-05-08 | verified | Precondition `collections.agent_details_sent` |
| IN-RBI-DLD-2025-GRO-001 | Nodal grievance officer shown on web and DLA; 30 days, then escalation to RB-IOS | 2025-05-08 | verified | `lending.gro_in_comms`; 30-day grievance SLA timer |
| IN-RBI-DLD-2025-DATA-001 | Data collection need-based, with explicit consent and audit trail; LSP stores only minimal data; **servers in India only** | 2025-05-08 | verified | `data.residency=IN_ONLY`; field allowlist |
| IN-RBI-DLD-2025-REPAYMENT-001 | Repayment goes straight to the RE's account; no third-party pool account | 2025-05-08 | verified | Payment-link beneficiary check |
| IN-RBI-RBC-RECOVERY-HOURS-001 | No recovery calls before 08:00 or after 19:00 | 2025-11-28 | verified | Contact Arbiter hard constraint `recovery.contact_window` |
| IN-RBI-RBC-RECOVERY-MFI-HOURS-001 | Microfinance: 09:00-18:00 only | 2025-11-28 | partial | Window switches on `loan.product_class=MICROFINANCE` |
| IN-RBI-RBC-RECOVERY-CONDUCT-001 | No intimidation, threats, anonymous calls, false representations or shaming; no harassing relatives, friends or co-workers | 2025-11-28 | verified | M12 conduct judge; `recovery.contact_party` allowlist; wrong-party exit |
| IN-RBI-RBC-RECOVERY-AGENTINFO-001 | Agency details given to borrower; agent carries ID and authorisation | 2025-11-28 | verified | Mandatory voice opening disclosure (lender, agent, AI) |
| IN-RBI-RBC-RECOVERY-2026AMEND-001 | New all-RE recovery-conduct directions (drafted 12 Feb 2026). Final reportedly issued 6 Aug 2026 | UNVERIFIED (reports say 2026-07-01 or 2027-01-01) | partial | Conservative frequency caps; **launch gate blocks production collections** |
| IN-RBI-RECOVERY-CALLRECORDING-001 | Whether RBI requires call recording | UNVERIFIED | unverified | Contact log always on; recording only with disclosure |
| IN-RBI-OUTSOURCING-NBFC-001 | RE keeps ultimate responsibility; responsible for its agents; vendor must use RE-level care | 2025-11-28 | verified | Vendor DD pack; overrides can only tighten |
| IN-RBI-OUTSOURCING-NBFC-DATAISOLATION-001 | No comingling of multiple clients' data; data can be isolated per RE | 2025-11-28 | verified | `ml.training_scope=tenant`; cross-tenant training pre-flight |
| IN-RBI-OUTSOURCING-NBFC-AUDIT-001 | Audit rights; prior approval for subcontractors; offshore limits; RE owns grievances | 2025-11-28 | verified | Subprocessor registry and per-tenant approval |
| IN-RBI-OMBUDSMAN-RBIOS-001 | RB-IOS 2026: complain to Ombudsman after 30 days with no reply or a rejection; file within 90 days | 2026-07-01 | verified | Tenant-owned grievance case with 30-day SLA |
| IN-RBI-TAT-FAILEDTXN-001 | Rail TATs (UPI merchant/POS T+5; NACH/IMPS/card-to-card T+1); ₹100/day if missed | 2019-10-15 | verified | `tat.no_double_debit` while debited-not-credited |
| IN-RBI-FRAUDCOMP-2027-001 | Wider fraud-txn coverage, faster TATs, small-value compensation (reported: 85% up to ₹25k, loss ≤₹50k, once per lifetime, report within 5 days) | 2027-01-01 | partial (amounts secondary) | Fraud intent pauses recovery; representment needs approval |
| IN-GOI-DPDP-COMMENCEMENT-001 | Rules published 13 Nov 2025: Board provisions immediate; Consent Managers from 13 Nov 2026; core duties from 13 May 2027 | 2025-11-13 | verified | `dpdp.*` shadow mode until 2027-05-13, then enforced |
| IN-GOI-DPDP-NOTICE-CONSENT-001 | Standalone, itemised notice; purpose-limited consent, as easy to withdraw as to give | 2027-05-13 | partial | Purpose ledger; `dpdp.purpose_permitted` |
| IN-GOI-DPDP-RETENTION-001 | **Keep personal data and processing logs ≥1 year** (r.8(3)), then erase unless law requires; 48h pre-erasure notice for Third-Schedule classes | 2027-05-13 | verified | Retention floor 365d; erasure workflow and crypto-shred |
| IN-GOI-DPDP-BREACH-001 | Notify affected people without delay; notify the Board without delay, detailed report within 72h | 2027-05-13 | verified | Incident timers (tenant ≤24h) |
| IN-GOI-DPDP-SDF-001 | SDF: yearly DPIA and audit; algorithmic due diligence; possible localisation | 2027-05-13 | verified | `tenant.is_sdf` requires DPIA before model promotion |
| IN-GOI-DPDP-PROCESSOR-001 | Processor works only under contract; Fiduciary stays responsible | 2027-05-13 | partial | `dpdp.processor_scope` per DPA |
| IN-GOI-DPDP-RIGHTS-001 | Publish DPO/contact; answer grievances within ≤90 days | 2027-05-13 | verified | DSR queue and timer |
| IN-TRAI-TCCCPR-DLT-001 | Registered headers/series only. Promotional needs opt-out and preference/consent. Promo mixed into service counts as promo. Transactional = within 30 min | UNVERIFIED (regs dated 2025-02-12) | partial | Comms MCP requires DLT ids and message category |
| IN-TRAI-TCCCPR-VOICE-001 | Commercial calls from designated series: promo and robo on 140-series; service/transactional robo on 1600-series; robo calls pre-declared | UNVERIFIED (regs dated 2025-02-12) | partial | `start_call` requires series, registration and pre-declaration |
| IN-RBI-DATALOCAL-001 | All payment-system data stored only in India | 2018-10-15 | verified | `residency.india_only`; redaction for foreign subprocessors |
| IN-RBI-PA-2025-VENDOR-001 | PA duties (merchant due diligence, PCI, storage, escrow) reach vendors by contract | 2025-09-15 | partial | No pooled funds; copy lint against "RBI-authorised"/"PA" claims |

## 2. Key findings

1. **The e-mandate rules are now one direction.** RBI's *Digital Payments – E-mandate Framework, 2026* (21 Apr 2026, effective immediately) replaces the 2019-2024 circulars. Cite it, not the old circulars. It covers **cards, PPI and UPI only**. NACH/eNACH is governed by NPCI and sponsor-bank rules, so the 24h / ₹15,000 logic must not be applied to NACH EMIs.
2. **The ₹1,00,000 AFA-free tier covers only three categories:** insurance premiums, mutual-fund subscriptions and credit-card bills. EMIs and subscriptions above ₹15,000 need AFA on every debit.
3. **UPI AutoPay execution limits:**
   - Non-peak hours only, and 1 + 3 attempts per sequence (NPCI OC-215/215A). The primary PDF was not machine-readable, so confirm with each PA.
   - No inducements to port a mandate (OC-223).
4. **Recovery conduct:**
   - Currently in-force text (NBFC Responsible Business Conduct Directions 2025, para 100): calls only 08:00-19:00, no harassment of relatives, friends or co-workers.
   - Microfinance: 09:00-18:00.
   - A rewrite covering all REs was drafted in Feb 2026. The final text is **not confirmed on a primary source**: secondary reports say it was issued 6 Aug 2026, and disagree on whether it took effect 1 Jul 2026 or takes effect 1 Jan 2027.
5. **DLD 2025 has three hard constraints for lending tenants:**
   - The recovery agent's details must reach the borrower **before** first contact.
   - Borrower data must be stored **only in India**.
   - LSPs may store only minimal data.
6. **DPDP timing:** core duties begin **13 May 2027**. Rule 8(3) sets a **minimum** retention of one year for personal data and processing logs, which is easy to miss. Aggressive auto-deletion can itself be a breach.
7. **TRAI 2025 amendment:** a failed-payment reminder that includes an offer becomes a *promotional* communication. Robo/AI calls must come from designated series (1600 for service, 140 for promotional).
8. **Fraud compensation:** the RBI press release confirms the final framework is effective **1 Jan 2027**. The rupee figures come from the press, not from the Direction text.

## 3. CONFLICTS WITH PRODUCT ASSUMPTIONS

Severity: **HIGH** = redesign or block before launch; **MEDIUM** = must add a constraint; **LOW** = documentation or config.

### (a) "Debit may be shifted within a mandate window to after predicted salary day": CONFLICT (MEDIUM)

- **Pre-debit notice (IN-RBI-EMANDATE-PREDEBIT-001).** The issuer's notice fixes the **date/time and amount**. Moving the debit after the notice needs a *new* notice, then 24 hours more, before execution. You cannot pick the day on the day.
- **UPI AutoPay execution limits (IN-NPCI-UPIAUTOPAY-EXEC-001).** Executions are limited to non-peak hours and 4 attempts per sequence. A shifted debit uses the same attempt budget.
- **Mandate terms.** The mandate's registered frequency, validity and debit-day rule (UPI recurrence rule, card mandate terms, NACH frequency) limit how far a debit can move. These NPCI rule details were **not verified** here.
- **Customer consent to the timing.** A shift is only safe where the mandate terms or tenant contract allow it and the customer was told. Otherwise, a debit on an unexpected day is a likely dispute and "unauthorised" chargeback trigger.
- **Lending.** For EMIs, moving the debit past the due date can create DPD and credit-bureau effects and penal charges. The lender must approve this as a policy (repayment-schedule change), not as an optimiser choice.
- **Salary-day prediction.** Salary-day prediction uses income/transaction data, so it needs a DPDP purpose basis (IN-GOI-DPDP-NOTICE-CONSENT-001). For lending tenants it must also be need-based (IN-RBI-DLD-2025-DATA-001).
- **Required change.** L2 "Pre-debit Guardian" may only choose a slot that satisfies all of the following:
  1. Inside the mandate's permitted date range (a tenant-declared `mandate.shift_window` sourced from the mandate terms).
  2. At least 24h after a *new* issuer pre-debit notice.
  3. In a non-peak window, with attempts remaining.
  4. For lending tenants, not past the due date unless the lender's grid allows it.

  Otherwise it must fall back to a reminder or payment link.

### (b) "Contact window 8am–7pm applies to lending recovery only": CONFLICT (MEDIUM)

- **Microfinance is narrower.** The 08:00-19:00 window is verified (NBFC Responsible Business Conduct para 100), but microfinance loans are restricted to **09:00-18:00** (partially verified). One fixed lending window is wrong.
- **The 2026 amendments may change it.** The all-RE recovery rewrite (IN-RBI-RBC-RECOVERY-2026AMEND-001) may add frequency caps and restate hours. The window must be config tied to the policy_id, not a constant.
- **Non-lending contacts have other rules.** RBI's hour rule does not directly govern D2C/SaaS subscription reminders. TRAI TCCCPR does:
  - Promotional content respects the customer's preferences, including time bands.
  - Calls must come from designated series.
  
  There is no verified statutory clock-hour window for non-lending service calls. That makes a platform default window (e.g. 08:00-19:00 or tenant-set) a **product safety choice**, not a legal floor. It should apply to all tenants to avoid complaints and spam tagging.
- **Pre-due EMI reminders.** Reminders before the due date are arguably not "recovery". Apply the recovery window to them anyway (conservative), and to every channel, not only calls.
- **Required change.** `contact_window` becomes a policy lookup keyed by (tenant type, product class, contact purpose), with the regulatory floor locked:
  - Lending recovery: 08:00-19:00.
  - Microfinance: 09:00-18:00.
  - Non-lending: platform default, overridable by the tenant only to narrow it.

### (c) "Voice agent may call customers for subscription recovery": CONDITIONAL (MEDIUM-HIGH)

Allowed only if all of these hold (IN-TRAI-TCCCPR-VOICE-001, IN-TRAI-TCCCPR-DLT-001):

- **Registered sender.** The tenant (Principal Entity) is a registered sender. The call comes from a **designated series**: 1600-series for service/transactional robo/AI calls, 140-series for promotional. Robo-call use and purpose are pre-declared to the operator.
- **Service vs promotional.**
  - A failed-payment reminder about a service the customer already has is a *service* call.
  - Adding **any** offer, discount or upsell (typical in "win-back") makes it **promotional**, which requires the 140-series plus preference or explicit-consent checks, and an opt-out.
  - A promotional script on the 1600-series is a violation.
- **Data protection.** Calling and recording are DPDP processing:
  - The notice must cover the purpose (IN-GOI-DPDP-NOTICE-CONSENT-001).
  - Recording needs a disclosure at the start of the call.
  - For payment data, audio and transcripts stay in India (IN-RBI-DATALOCAL-001).
- **No porting nudges.** The voice agent must never push the customer to port the UPI mandate to another app (OC-223).
- **Conflict flag.** Plain 10-digit Exotel numbers, or scripts that mix offers into reminders, conflict with TRAI. Treat these as **HIGH** until the telephony setup is confirmed.

### (d) "Randomized holdout (not contacting some customers) for lending collections": CONFLICT (HIGH, needs legal and tenant sign-off)

- **What is not prohibited.** No verified rule bans choosing not to make *discretionary* nudges.
- **What the holdout must never suppress:**
  - The issuer's pre-debit and post-debit notices (not Nirantar's to suppress anyway).
  - Recovery-agent details required **before** contact (DLD 8(v)).
  - Grievance and GRO information.
  - Replies to borrower-initiated contact: hardship, complaint, fraud or data-rights requests.
  - Any lender-mandated statutory or contractual notices. RBI's SMA/NPA borrower-intimation rules were **not verified** in this retrieval.
- **Borrower harm.** A holdout borrower who would have been reminded may roll into DPD, bureau reporting and penal charges. That is a fair-treatment risk under the Responsible Business Conduct framework. The RE carries it, since outsourcing leaves responsibility with the RE (IN-RBI-OUTSOURCING-NBFC-001).
- **Required change:**
  - Lending holdouts are **opt-in per lender**, approved by the RE, and limited to discretionary AI nudges.
  - Vulnerable, hardship-flagged, complaint-open and fraud-flagged borrowers are excluded.
  - A cap on the holdout's DPD impact, with monitoring and a stop rule.
  - The holdout is recorded in the experiment register, so a regulator or auditor can see what was withheld and why.

  The build-brief default of "5-10% per tenant, always on" (BB-§21) should **not** apply to lending tenants without this.

### (e) "Storing call recordings": CONDITIONAL (MEDIUM)

- **DPDP:**
  - Recordings are personal data, so they need a notice and purpose (Rule 3) and security safeguards (Rule 6), and breach duties apply (Rule 7).
  - Rule 8(3) requires keeping personal data and processing logs for at least one year from processing. After the purpose ends, erase them unless another law requires more. A tenant setting of "delete audio after 30 days" may conflict with Rule 8(3) from 13 May 2027 (legal review needed on whether audio counts as "logs of the processing").
- **Digital lending:**
  - Borrower data must be need-based and consented, and stored **in India only** (DLD 12-13).
  - An LSP may hold only minimal data. If Nirantar is the LSP, recordings should sit in RE-controlled, tenant-isolated storage.
- **RBI recording duty unknown.** Whether RBI *requires* recovery-call recording is **UNVERIFIED** (IN-RBI-RECOVERY-CALLRECORDING-001).
- **Required change.** Recording only after an announced disclosure. India-region, encrypted, tenant-isolated storage. A retention floor and ceiling per policy. Erasure propagates to transcripts, features and training sets. Contact logs are kept separately from audio.

### (f) "Using customer payment history to train models across tenants": CONFLICT (HIGH)

- **NBFC outsourcing directions, paras 26, 27, 70 (verified):** no comingling or combining of multiple clients' information, and clear isolation of each NBFC's data.
- **DLD 2025, paras 12-13 (verified):** borrower data is need-based, with explicit, purpose-specific consent and an audit trail. LSPs store only minimal data.
- **DPDP:**
  - As a **processor**, Nirantar may process only for the tenant's purposes under contract. Using tenant A's data to improve tenant B's models is a new purpose.
  - That likely makes Nirantar a Fiduciary for it, needing its own notice and consent (IN-GOI-DPDP-PROCESSOR-001, IN-GOI-DPDP-NOTICE-CONSENT-001).
- **Payment data localisation (verified):** payment data used for training must stay in India.
- **The build brief already says** "no cross-tenant retrieval" (BB-§7). The nightly retrain loop must extend that to training.
- **Required change:**
  - Default `ml.training_scope=tenant`.
  - Global or cross-tenant models only with express tenant contracts, a DPDP basis, and documented irreversible anonymisation or aggregation reviewed by counsel.
  - Never for regulated-lender data without that lender's written permission.
  - Public or synthetic data (RecurSim) remains the route for shared priors.

### Other product-spec observations

- **L6 Dispute Defender.** Pre-debit notice proof is strong evidence only if Nirantar gets the *issuer's* notice evidence. Nirantar's own reminders are not the regulatory notice.
- **L8 Treasury** must never route funds through Nirantar (IN-RBI-PA-2025-VENDOR-001). For lending, repayments must go directly to the RE account (IN-RBI-DLD-2025-REPAYMENT-001).
- **Fraud claims.** "Screenshots are never proof" is consistent. A customer's fraud claim must also **stop** recovery for that debit, and the fraud compensation framework starts 1 Jan 2027.

## 4. Unverified or partially verified items to close before launch

1. **Final 2026 recovery-agent directions:** date, effective date (1 Jul 2026 vs 1 Jan 2027), call-frequency caps, recording and logging duties. *Blocks production collections.*
2. **Call recording.** Whether RBI requires recording of recovery calls.
3. **UPI AutoPay (NPCI OC-215/215A).** Retry cap and peak-hour text from the primary PDF; UPI mandate debit-day/recurrence rules for shifting.
4. **NACH/eNACH.** Presentation, re-presentation and return rules; any pre-debit notice duty.
5. **Card tokenisation.** CoF tokenisation circulars: which card fields merchants and vendors may keep.
6. **Fraud compensation.** Exact figures and conditions in the 24 Jun 2026 Amendment Directions (rbidocs PDF behind CAPTCHA).
7. **TRAI.** Gazette publication date of the 2025 amendment, and the operative DoT/TRAI directions on the 1600-series.
8. **DPDP.** Act section text (ss.4-10); Gazette date vs PIB "14 Nov 2025"; any MeitY timeline amendments (meity.gov.in returned 403).
9. **E-mandate "no charges" clause.** Paragraph number.
10. **Microfinance window.** Paragraph number for 09:00-18:00.
11. **PSS Act s.25.** Statute text.
12. **Equivalents for bank tenants.** RBC and outsourcing directions for commercial banks, SFBs and UCBs (only the NBFC versions were read).
13. **SMA/NPA intimation.** RBI borrower-intimation rules, relevant to holdouts.

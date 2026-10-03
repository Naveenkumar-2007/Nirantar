# Nirantar — Product Specification (baseline v1.0, 2026-09-28)

## 1. One line

Nirantar ("uninterrupted") is an autonomous operating system for recurring revenue. It keeps subscription, EMI, SIP, insurance-premium and B2B-retainer payments flowing: it predicts failures before the debit date, fixes them, recovers what still fails, and proves every extra rupee against a randomized holdout.

## 2. Regulatory positioning

Nirantar is an orchestration and recovery platform that **integrates with licensed payment providers** (Razorpay, Cashfree, Stripe). It is not a payment aggregator, does not hold funds, and does not claim RBI authorisation. Provider-neutral by design.

## 3. Customers and value

| Customer (tenant) | Recurring money | Pain | What Nirantar does | KPI |
|---|---|---|---|---|
| D2C subscription brands | UPI AutoPay, card mandates | Involuntary churn from failed debits, expired cards | Predict/prevent failures, repair mandates, recover via WhatsApp/voice | Involuntary churn %, recovered ₹ |
| SaaS / OTT / Edtech | Monthly/annual plans | Failed renewals, "I didn't authorise" chargebacks | Retry timing, dispute evidence packs | Renewal success, dispute win % |
| NBFCs / digital lenders | EMIs (NACH/eNACH/AutoPay) | Bounces, costly call centres, conduct rules | Pre-debit prediction, compliant voice collections, agency handoff | Bounce rate, roll-rate, cost per ₹ collected |
| AMCs / investment apps | SIPs | SIP failures, drop-offs | Pre-debit fixes, nudges only where they change outcomes | SIP continuity % |
| Insurers | Premiums | Lapses | Premium retry, lapse revival | Persistency |
| B2B companies | Retainers/invoices on mandate | Late collections | Forecast-driven collection priority | DSO, forecast accuracy |

## 4. Core loop

TRIGGER → CONTEXT → PREDICT → PLAN → ARBITRATE → COMPLIANCE CHECK → ACT → WAIT → VERIFY → RECOVER/ESCALATE → MEASURE VS HOLDOUT → LABEL → LEARN

## 5. The eight operating loops (24/7)

| # | Loop | Trigger | Automatic fix |
|---|---|---|---|
| L1 | Mandate Doctor | Card expiring, mandate paused/revoked, AutoPay limit below bill | Re-auth link at best time, switch to healthier instrument on file, confirm new mandate |
| L2 | Pre-debit Guardian (T-3) | Predicted P(fail) above threshold | Shift debit inside the permitted window, useful pre-debit notice, early payment link |
| L3 | Failure Triage (T+0) | Decline code | Technical decline → wait for bank recovery then retry; insufficient funds → model-timed retry; revoked → win-back; expired → update link |
| L4 | Conversation | Recovery needed | Contact Arbiter picks one channel/agent/time; WhatsApp or voice; payment link; promise-to-pay; webhook verification |
| L5 | Collections (lending) | Missed EMI, DPD buckets | Bucket strategy, voice negotiation inside the lender's grid, contact logs, human/agency handoff |
| L6 | Dispute Defender | Renewal chargeback | Evidence pack (mandate record, pre-debit notice proof, usage logs) → representment |
| L7 | Revival | Lapsed subscription/policy/SIP | Win-back only where uplift model predicts a real effect |
| L8 | Treasury | Hourly cash forecast | Raise collection priority on high-value at-risk debits; suggest payout reschedule / credit draw (human approves) |

Plus the nightly learning loop: outcomes → labels → retrain → offline gate → shadow → canary → promote.

## 6. Differentiation (vs single-purpose gateway agents, e.g. Razorpay Agent Studio, launched 2026-03-12)

| | Single-purpose gateway agents | Nirantar |
|---|---|---|
| Structure | Separate agents | One Conductor + specialists sharing one customer memory |
| Timing | React after failure | Predict 3 days before debit and fix in advance |
| Targeting | Propensity nudges | Causal uplift |
| Proof | Before/after | Always-on randomized holdout → incremental ₹ |
| Customer contact | Each agent independently | Contact Arbiter: one fatigue budget per customer |
| Gateways | One | Razorpay, Cashfree, Stripe via adapters/MCP |
| Forecast | Alert | Forecast → treasury actions with approval |
| Compliance | Review-first mode | Policy-as-code gate + conduct judge on every outbound action |
| Learning | Not described | Every action → verified outcome → label → retrain |
| Interop | — | A2A with collection agencies and customers' own agents |

## 7. Non-negotiables

- LLMs never compute amounts, dates, retry schedules, authorisation or reconciliation.
- Every outbound action passes the Compliance Guardian (ALLOW / DENY / REQUIRE_APPROVAL / REQUIRE_MORE_INFORMATION).
- Screenshots are never proof of payment; provider/ledger state wins.
- Voice agent never collects OTPs or PINs; identity stays with the tenant's existing auth.
- Tenant isolation at every layer.
- No claim of "production ready", "compliant", "provider compatible" or "improved revenue" without the corresponding tests, review or controlled evaluation.

## 8. Success metrics

Incremental ₹ vs holdout · payment success lift · recovery rate · involuntary churn reduction · dispute recovery · contact cost per recovered ₹ · zero compliance violations · verified-outcome coverage.

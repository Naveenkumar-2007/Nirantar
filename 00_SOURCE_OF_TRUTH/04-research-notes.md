# Research notes (snapshot, retrieved 2026-09-28)

Secondary sources are marked [SC]. Re-verify before any figure is used in product copy or decisions.

## Competitive landscape

- Razorpay Agent Studio launched at FTX'26 on 2026-03-12, built on Anthropic's Claude Agent SDK. Agents include Cart Recovery, COD Confirmation, Dispute Responder, Subscription Recovery (voice), Cashflow Forecaster, RTO Shield, RTO Insights, Settlement Insights; lending (EMI Recovery, Collection Prediction…), investments (SIP Failure Recovery…), insurance (Payment Retry, Lapsed Policy Revival…). Review-first mode; merchant-approved offers only.
  - https://razorpay.com/newsroom/razorpay-launches-the-worlds-first-ai-native-agent-studio-for-payments-at-ftx26-powered-by-anthropics-claude/
  - https://razorpay.com/agent-studio/
  - https://razorpay.com/blog/agent-studio-ai-agents-by-razorpay/
  - https://razorpay.com/blog/razorpay-agent-studio-principles-guardrails-and-merchant-control/
- Critique (dark patterns, price discrimination, unproven incrementality) [SC]: https://www.medianama.com/2026/03/223-razorpay-launches-ai-agent-studio-questions-loom-dark-patterns-price-discrimination/
- Official MCP servers: https://github.com/razorpay/razorpay-mcp-server · https://github.com/cashfree/cashfree-mcp
- Card dispute AI: Casap $25M Series A (Aug 2025), Chargeflow $35M, Quavo, Rivero.

## Regulatory snapshot (verify in docs/compliance)

- E-mandates: pre-debit notice ≥ 24h; AFA-free up to ₹15,000; ₹1,00,000 for insurance, mutual funds, credit-card bills [SC: Mint, RocketPay].
- Recovery conduct: contact 8am–7pm, no third-party contact, contact logs [SC].
- RBI harmonised TAT for failed transactions (20 Sep 2019), ₹100/day compensation: https://www.rbi.org.in/commonman/English/scripts/Notification.aspx?Id=3074
- RBI digital-fraud compensation framework finalised, effective 2027-01-01 [SC: Business Standard, Jun 2026].
- RBI Payment Aggregator Master Directions, 15 Sep 2025 [SC].

## Market figures

- UPI: 24.51 bn transactions in Aug 2026 (Business Standard) [SC].
- RBI Ombudsman FY25: 13.34 lakh complaints (+13.55 %) [SC].

## Datasets

| Dataset | Licence | Use |
|---|---|---|
| KKBox churn (WSDM 2018) | Kaggle competition terms — research only | M6, M1 method dev |
| Home Credit installments | Kaggle competition terms — research only | M9 |
| Criteo Uplift v2.1 (25M rows) | CC BY-NC-SA 4.0 — non-commercial | M5 method dev |
| Hillstrom email | Verify | M5 |
| NPCI UPI statistics | Public aggregates | M4 priors |

## Voice

Sarvam: Saaras v3 STT (streaming WebSocket, code-mix mode; Saarika legacy), Bulbul TTS, Sarvam-M/30B/105B chat. https://docs.sarvam.ai/api-reference-docs/getting-started/models

# Decision log (product discovery, 2026-09-28)

| # | Decision | Why | Rejected alternatives |
|---|---|---|---|
| D1 | Focus on recurring money (subscriptions, EMIs, SIPs, premiums, retainers) | Same rails (UPI AutoPay, eNACH, card mandates), measurable outcomes, a large incumbent (Razorpay) proves demand | Bank dispute ops (no public data), merchant-trust network (Razorpay not in it, but weaker founder pull), card chargebacks (crowded) |
| D2 | Differentiate on prevention, one brain, causal proof, multi-gateway, closed loop | Gaps visible in Agent Studio's own descriptions and public critique | Building another single-purpose agent |
| D3 | Orchestration/recovery platform, not a payment aggregator | No RBI authorisation; lower regulatory burden; provider-neutral | Holding funds / PA licence |
| D4 | RecurSim with true counterfactuals before any partner data | Only way to evaluate uplift and offline policies honestly without customer data | Claiming results on public data that doesn't match the domain |
| D5 | Contact Arbiter is a deterministic optimiser | Money-adjacent decisions must be reproducible and auditable | LLM-based arbitration |
| D6 | Sarvam for Indic voice behind an abstraction | Best-supported Indic STT/TTS; abstraction keeps a fallback path | Hard-wiring one provider |

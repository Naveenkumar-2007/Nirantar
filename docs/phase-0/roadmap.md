# Implementation and evaluation roadmap

## Principle

Get a **thin, real end-to-end path** working early (the ₹999 acceptance scenario on mocks and RecurSim), then deepen each layer. A layer counts as done only when it meets the Definition of Done (BB-§50).

## Implementation roadmap

| Milestone | Phases | Outcome you can see |
|---|---|---|
| M-A Foundation | 0–4 | Monorepo, Compose stack, schemas, events, ledger, audit chain, auth + tenant isolation tests green |
| M-B Money rails | 5–6 | Provider interface with mock + Razorpay/Cashfree/Stripe adapters (contract-tested against official fixtures), webhook ingress with replay/spoof/duplicate protection, Verifier, reconciliation |
| M-C Learning core | 7–8 | RecurSim with counterfactuals, dataset registry, features, M1/M4/M5/M10 baselines, holdout service, MLflow gates |
| M-D Thin E2E | 10–12 (slice) | Conductor + Debit Strategist + Contact Arbiter + Compliance Guardian + Verifier; DebitCycleWorkflow; gateway/mandate/comms/policy/experiment/ledger MCP tools → **₹999 scenario passes on mocks** |
| M-E Breadth | 9–16 | RAG + memory, remaining agents and models, all MCP servers, evidence + screenshots, Sarvam voice, A2A, approvals |
| M-F Product | 17 | Merchant dashboard + platform console on real APIs |
| M-G Hardening | 18–21 | Benchmarks, security/chaos/load, staging IaC, final acceptance trace |

## Evaluation roadmap

| Area | First measurable | Gate introduced | Data |
|---|---|---|---|
| ML (M1, M4, M10) | M-C | AUC/PR-AUC/Brier/calibration; WAPE for M10 | RecurSim, then partner |
| Uplift (M5) + holdout | M-C | Qini/AUUC vs true counterfactual uplift; CI coverage of incremental ₹ estimate | RecurSim (truth known); Criteo for method sanity only |
| Agents | M-D | Task success, tool correctness, invalid action rate, policy violation rate = 0 | Scripted scenarios + RecurSim |
| Compliance | M-D | 100 % of policy test_cases pass; bypass attempts fail | policies.yaml |
| RAG | M-E | Retrieval recall@k, citation precision/recall, unsupported-claim rate | Labelled Q/A over regulatory corpus |
| Voice | M-E | WER, code-mixed WER, turn latency p50/p95, PTP extraction accuracy | Recorded/synthetic Telugu/Hindi/English calls |
| Platform | M-G | p50/p95, throughput, error rate, workflow completion under chaos | Load + chaos suites |
| Business | M-G | Incremental ₹ vs holdout, recovery rate, contact cost per recovered ₹ | RecurSim now; design partner later |

All benchmark outputs are versioned under `evals/results/` with dataset provenance, so no number is ever reported without its source.

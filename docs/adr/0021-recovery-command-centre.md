# ADR-0021: Recovery Command Centre — queue, bounded batches with a holdout, verified proof (P8.5)

- Status: Accepted · Date: 2026-10-06
- Brief: "find revenue that's slipping away and win it back … show measured money recovered across a batch, with
  compliant escalation, stopping rules, and an audit trail."
- Evidence:
  - `tests/e2e/test_recovery_batch.py`:
    - 40 debits declined through signed webhooks; the queue ranks them; the dry-run preview allows 36 and refuses
      the 4 without WhatsApp consent, and sends nothing;
    - a batch with a 25 % randomised holdout runs on Temporal, and payment links go only to consenting treatment
      customers;
    - half of them pay; one holdout customer pays on their own;
    - the proof report shows verified ₹ per arm, uplift statistics, every action and an intact audit chain, and the
      outcomes land in the batch's experiment;
    - the stop button skips every remaining item;
    - a person's inbox reply passes the gateway, a wrong ₹ figure is refused, and threatening wording is refused.
  - A read-only check on the demo business (no sends):
    - 19 stalled declines (₹22,481) and 33 settled failures in its history (51.5 % recovered), so ₹11,581 is
      expected to be recoverable;
    - at 22:54 IST every preview is correctly refused: "outside contact window 09:00-20:00".

## Decisions

1. **The queue is computed, never stored.** Three sources from the system of record:
   - declined debits not yet collected;
   - upcoming debits whose M1 decision score is at or above a threshold (default 0.3);
   - open mandate-repair cases.

   Each item gets a state (stalled, promise broken, blocked by policy, needs approval, agent working, in a batch)
   with the reason in words.
2. **Expected recoverable ₹ comes from the business's own history.** It is amount × the historical recovery rate for
   that decline code, shrunk towards the business's overall rate (10 pseudo-observations).
   - History counts only failures with a known outcome: verified recoveries with money, or failures more than
     14 days old by the provider's clock.
   - With no history the field is empty and the UI says so; there is no default guess.
   - The first version used ingest time and counted ₹0 on-time payments as recoveries. The demo data exposed this
     (a 100 % rate), and it is fixed.
3. **A batch is an experiment.** Launching creates an experiment (treatment, plus a holdout of 0–50 %), and
   customers are assigned by stable hash.
   - The proof uses the same `experiments.incremental` statistics as the Overview. That function was factored out of
     `analyze`, so every proof in the product uses one implementation.
   - At close, verified outcomes are written to the experiment.
4. **Bounds and stopping rules.**
   - At most 500 items per batch; an item can be in only one running batch.
   - Actions run only as the `recovery_batch` agent, with tools the agents already have (payment link, WhatsApp
     recovery, pre-debit notice, mandate repair). Every call goes through the ToolGateway with an idempotency key
     per (batch, item, step).
   - Consent, contact window, fatigue and STOP are re-checked at send time. Window denials are retried when the
     window opens (up to 3 rounds, inside the measurement window).
   - The stop button marks the batch `stopping`; every remaining item is skipped at its next step even if the
     Temporal signal is lost.
   - A batch whose workflow cannot start is marked stopped immediately ("nothing was sent").
5. **Recovery counts only with provider confirmation.**
   - Debits: a captured payment after launch, with the debit settled by the verifier.
   - Mandates: the repair case closed as repaired.
   - The proof report also lists every gateway action (policy decision, parameters hash) and verifies the tenant's
     audit chain.
6. **Preview equals execution.** `ToolGateway.preview` runs the same scope, schema and policy phases as `call`,
   with no writes. The operator sees the exact rendered message from the approved template; the payment link is
   created only at send.
7. **Human takeover** uses `comms.operator_reply`, run as `human_operator`. The tool records the signed-in person.
   - Conduct rules screen the text.
   - Any ₹ figure must be an amount the customer actually owes.
   - Free text works only inside WhatsApp's 24-hour window; the channel refuses otherwise.
8. **UI.**
   - `/recovery`: stats, a queue with tabs and selection, a preview dialog showing allowed / needs approval /
     refused with reasons, and launch with holdout and window.
   - `/recovery/batches/[id]`: the proof report, refresh, print, and stop.
   - Conversations has a reply box.

## Consequences

- The local services process sends through the real WhatsApp channel. Launching a batch on the demo business would
  try to message synthetic numbers, so batches were exercised end to end only against the mock channel. Real sends
  wait for the permanent token and real customers (P8.4).
- Upcoming-debit risk needs the M1 decision scores written by DebitCycleWorkflow. A business without the feature
  store simply has no "at risk" items.
- Checkout abandonment, B2B receivables and degradation-driven holds are separate item kinds (P10). The batch
  engine is kind-agnostic, so each new kind needs a queue source, a tool and an outcome rule.

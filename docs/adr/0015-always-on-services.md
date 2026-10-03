# ADR-0015: Always-on services, per-tenant providers, disputes and the event bridge (Phase 2 · P5)

- Status: Accepted · Date: 2026-10-01
- Evidence: tests/e2e/test_always_on_services.py (bridge + Dispute/Onboarding/Reconciliation workflows on Postgres,
  object store and a Temporal test server), tests/soak/test_services_soak.py (the real services under traffic on
  Redpanda + Temporal server; invariants below), tests/e2e/test_acceptance_999.py (unchanged, still green).

## Context

Until P4 the workflows existed but nothing ran them continuously. Raw webhooks were stored but **nothing processed
them in production**, debit workflows were started by tests, and activities, the webhook route and
reconciliation used one process-wide provider key. A multi-tenant product needs each merchant's own provider account.

## Decisions

1. **Per-tenant provider resolution** (`payments/providers/resolver.py`). Every provider call (activities, webhook
   re-fetch, reconciliation, the bridge) resolves the tenant's own account from `core.provider_accounts`: the live
   account in production, else the test account. Credentials are secret references resolved at use and cached for
   5 minutes, so a rotated key takes effect. `mock` is never built from configuration. It exists only where a
   process injects one (tests, demo, soak), so production cannot silently run against a simulator. A webhook for a
   tenant with no matching account is answered 404.
2. **One process, three supervised services** (`python -m nirantar.services`):
   - the **Temporal worker** runs every workflow and activity on one task queue (`NIRANTAR_TASK_QUEUE`, default
     `nirantar-main`; the old per-workflow queues are gone);
   - the **outbox relay** (unchanged, at-least-once);
   - the **event bridge**.

   Each service has a heartbeat. A crash restarts only that service, with backoff. `/health` (port 18090) returns
   200 only while all three are alive, and reports restarts and the last error.
3. **Event bridge = Kafka → durable work.**
   - `provider.webhook_received` → `process_raw_event` with the tenant's account.
   - `subscription.debit_scheduled` → start DebitCycleWorkflow with `start_delay` until the notice day.
   - `payment.*` / `reply.received` → **signal-with-start**.
   - `dispute.opened` → start DisputeWorkflow.
   - `dispute.updated` → signal, or signal-with-start while the dispute is open.

   Every handler is idempotent:
   - Workflow ids are deterministic, and `REJECT_DUPLICATE` stops a replayed event from re-running a finished
     workflow.
   - Raw webhooks carry a processed status.
   - Signals are idempotent inside workflows.

   The Redis seen-set is only a shortcut.

   Errors split two ways:
   - **Outages** (Temporal or DB unreachable): retried without committing the offset, so the bridge pauses and
     loses nothing.
   - **Bad events**: retried 3 times, then written to `events.consumer_dead_letters` (tenant RLS, visible in the
     product) and to the Kafka DLQ, and the partition moves on.
4. **Arrival order must not matter** (found while designing the soak): Kafka orders events only within a topic, so
   a payment event can overtake its `debit_scheduled`.
   - Payment events therefore use signal-with-start.
   - A cycle that starts after the debit time skips the "pre-debit" notice rather than send a misleading one.
   - A dispute whose payment webhook has not been processed imports the payment from the provider first, so it is
     still linked to its debit and the evidence is complete. The 45-second soak exposed this: without it, a fast
     chargeback scored 0.4 and went to a human.
5. **Debit-cycle timing is tenant configuration**, in a new namespace `operations` (versioned, audited, ADR-0011):
   - notice lead (default T-3; RBI requires ≥ 24 h);
   - local debit time and timezone;
   - payment wait;
   - recovery window, round gap and contact rounds;
   - `disputes.min_win_probability`.

   Every DebitCycle joins the tenant's running `recovery` experiment, created on first use with the tenant's
   holdout, so recovery uplift is always measured.
6. **DisputeWorkflow**, triggered by Razorpay dispute webhooks re-fetched with `GET /disputes/{id}`.
   - **Prepare:** builds verified evidence items, the win-probability prior and the representment draft (the LLM
     draft is screened, with a template fallback), then a **PDF evidence pack** (reportlab) stored in the object
     store, and opens a dispute case.
   - **Decide:** contest when the probability is at least the tenant's minimum and at least 24 h remain; otherwise
     escalate to a human. **Accepting a chargeback is never automatic.**
   - **Submit:** through the MCP gateway (`dispute.submit_representment`, scope `money`), which uploads the pack to
     the Documents API (`POST /v1/documents`, purpose `dispute_evidence`) and contests it (`PATCH
     /v1/disputes/{id}/contest`, action `submit`). Above `representment_approval_above_minor` it goes through
     maker-checker; the workflow polls the approval until the deadline.
   - **Close:** on the network's outcome, with a `dispute_won` label sourced from the provider (future M11 training
     data).
7. **OnboardingWorkflow**:
   - verify credentials with a cheap authenticated read (a failure stops with a reason);
   - import history (data pipeline: bronze → gold → health);
   - feature-store build and Redis materialisation;
   - retention fit;
   - record the status and summary on the tenant.
8. **ReconciliationSweepWorkflow**, a Temporal Schedule (every 30 minutes, overlap SKIP):
   - for every active tenant with a connected provider, pull provider truth for debits stuck in `attempting`;
   - re-process raw webhooks still `received` after 5 minutes.

   One tenant's provider outage is reported and does not stop the sweep.
9. **No fake delivery.** Until a real channel is connected (P6), the worker's comms sink is `UnconnectedSink`: every
   send fails loudly and the gateway records the action as failed. `MockCommsSink` is used only when
   `NIRANTAR_COMMS=mock` and the environment is not production.
10. **Mandates from provider truth + MandateHealth.** Until now nothing wrote `billing.mandates` in production.
    - **Discovery:** a payment carries the debited token (`token_id`) and the provider customer. That customer's
      token list (`GET /customers/{id}/tokens`) supplies rail, status, limit and expiry. The mandate is upserted
      (id derived from the token) and linked to the charged subscription.
    - **Token webhooks** (`token.confirmed/rejected/paused/cancelled`) carry only the token entity. Their status is
      confirmed against that list. Razorpay has no single-token endpoint, and the list omits expired, rejected and
      unused tokens, so a token missing from the list is accepted only as an end state. A webhook can never make a
      mandate look healthier than the provider says it is.
    - An unknown limit is stored as NULL, never guessed (migration 0016).
    - **MandateSweepWorkflow** (daily schedule) runs the Mandate Doctor's rules on every active subscription's
      mandate against its next debit, and starts one **MandateRepairWorkflow** per problem. The id is derived from
      the mandate, the repair and the state, so a problem is worked once.
    - A repair either asks the customer to act (a provider re-authorisation link via
      `POST /subscription_registration/auth_links`, or "resume in your UPI app" for a paused AutoPay; templates in
      en/hi/te, service purpose, consent and contact window enforced) or, for a fraud revocation, hands it to a
      human.
    - Repaired means the SAME doctor rules now find nothing to fix on a mandate the provider lists. The first
      version only checked active + validity, and closed an expiring mandate as "repaired" before the customer acted;
      the e2e test caught it.
    - Re-registration creates a new token. Its authorisation payment discovers it, `mandate.updated` wakes the
      repair through the bridge, and the subscription is relinked. Outcome labels are `mandate_repaired`, sourced
      from the provider.
11. **Dead-letter replay:** `POST /v1/events/dead-letters/{id}/replay` (needs `agents:operate`, audited). It marks the
    original outbox row unpublished, so the normal relay → bridge path re-delivers it.
12. **Worker versioning** = replay tests. Histories of all 7 workflow types are recorded from the e2e runs
    (`tests/replay/histories`) and must replay on the current code in every test run. A deliberate non-deterministic
    change, tried as a check, failed with `NondeterminismError`. Changes go behind `workflow.patched`, and the worker
    carries `NIRANTAR_BUILD_ID`. Temporal 1.25 predates Worker Deployments, so there is no build-id routing.
    Runbook: `docs/runbooks/services.md`.
13. **Not built, on purpose:**
    - **CustomerLifecycle entity workflow:** a long-lived workflow per customer would duplicate state that already
      lives in Postgres (system of record) and in the per-debit, dispute, mandate and revival workflows, at millions
      of open workflows. Revisit only if a cross-case orchestration need appears.
    - **Retrain:** done by the hourly Dagster schedule (`should_train` → train → monitoring → rollout, ADR-0013).
      The plan allowed either.

## Soak results (local, 2026-10-01)

| Run | Tenants | Traffic | Outcome |
|---|---|---|---|
| 30 s | 3 | 126 webhook deliveries (63 unique, each delivered twice); 42 delayed debit workflows; 11 disputes | 7 won, 4 lost |
| 45 s ×2 | 3 | — | 18 and 22 disputes, all contested once and closed |
| 120 s | 5 | 582 webhook deliveries (291 unique); 198 delayed debit workflows; 989 events relayed; 50 disputes | 26 won, 24 lost |
| 20 min (2026-10-02) | 5 | 5,988 webhook deliveries (2,994 unique); 1,866 delayed debit workflows; 9,323 events relayed; 576 disputes | 258 won, 318 lost |

Every run held all the invariants:
- outbox fully published;
- duplicate deliveries stored once;
- 0 unprocessed raw events;
- 0 duplicate payment rows;
- 0 dead letters;
- every future debit has exactly one workflow;
- every dispute contested exactly once and closed with the network's outcome;
- `/health` green throughout, 0 restarts.

## Consequences / limits

- Everything is verified against `MockProvider`. The Razorpay dispute and document endpoints follow the published
  API but have **not been exercised on a live dispute**; the test account has none.
- The worker and bridge scale by running more processes: Kafka consumer groups and Temporal task queues share the
  load. For now one process is the unit.
- Dead-letter replay is an API action (no dashboard button yet).
- The 72-hour soak in the plan's exit criterion has not been run (longest clean run: 20 min). A run interrupted by
  the machine sleeping ~2.4 h recovered on its own (the bridge rejoined Kafka after one supervised restart, no data
  lost), but it doesn't count as a clean soak. A 72 h run needs a machine or CI runner that does not sleep.

# Runbook — always-on services (worker · relay · event bridge)

Process: `uv run python -m nirantar.services` (one process; scale by running more, see §7). ADR-0015.

## 1. Is it healthy?

`GET http://127.0.0.1:18090/health` returns 200 only when all three components are alive.

| Field | Meaning |
|---|---|
| `components.<name>.alive` | heartbeat within 60 s |
| `restarts`, `last_error` | the supervisor restarted this component after a crash, with this error |
| `stats.bridge` | `<event_type>:<outcome>` counters |
| `stats.relay.published` | events published since start |

Platform view (`/platform/health`) shows the size of the outbox backlog and the age of the oldest unpublished event.

## 2. Symptom → cause → action

| Symptom | Likely cause | Action |
|---|---|---|
| `relay` not alive, outbox backlog growing | Redpanda down or DB role `nirantar_relay` broken | Restore Redpanda (`docker compose up -d redpanda`). The relay resumes from the outbox; nothing is lost. |
| `event_bridge` restarting, `last_error` names Kafka | broker unreachable | As above. Offsets are committed only after handling, so events are not skipped. |
| bridge alive but `stats` flat, outbox drained | consumer group stuck or wrong `KAFKA_BOOTSTRAP` | Check the group lag: `docker compose exec redpanda rpk group describe nirantar-event-bridge`. |
| bridge logs `bridge_infra_retry` | Temporal or Postgres unreachable | This is an outage pause, by design: the bridge holds the offset and retries with backoff. Fix the dependency; no action is needed afterwards. |
| `worker` restarting | Temporal unreachable, or an activity import error after a deploy | Check `last_error`. Roll back the build if it is an import error. |
| dead letters appear (`GET /v1/events/dead-letters`) | a bad event: unknown provider account, a missing record, a code bug | Fix the cause, then replay (§3). |
| raw webhooks stuck in `received` | the bridge was down, or a transient provider error | The reconciliation sweep re-processes them every 30 minutes. You can also start `ReconciliationSweepWorkflow` manually. |
| debits stuck in `attempting` | webhook never delivered | Same sweep: it pulls the payment from the provider (`reconcile_open_debits`). |
| a provider returns 401 for one tenant | the merchant rotated or revoked keys | That tenant's events dead-letter and the sweep reports `error`. Ask the merchant to reconnect; the resolver cache expires within 5 minutes. |

## 3. Replaying a dead letter

1. Read the error with `GET /v1/events/dead-letters` and fix the cause.
2. Run `POST /v1/events/dead-letters/{event_id}/replay` with `{"reason": "..."}`. This needs the `agents:operate` permission (owner or compliance officer).
3. The original outbox row is marked unpublished. The relay re-delivers it and the bridge handles it again (idempotently). The replay is in the audit chain as `event.replayed`.

## 4. Schedules (created on first start; operators may edit them in the Temporal UI)

| Schedule id | Every | Workflow |
|---|---|---|
| `nirantar-reconciliation-sweep` | 30 min (`NIRANTAR_RECON_INTERVAL_MIN`) | ReconciliationSweepWorkflow |
| `nirantar-mandate-health` | 24 h (`NIRANTAR_MANDATE_SWEEP_HOURS`) | MandateSweepWorkflow, which starts one MandateRepairWorkflow per problem |

Both use overlap policy SKIP. Data pipeline, training, monitoring and rollout run hourly in Dagster (ADR-0012/0013).

## 5. Cases that need a human

| Case | What it means |
|---|---|
| `ops.cases` kind `dispute`, status `escalated` | evidence too weak, or the deadline is under 24 h. Decide whether to contest or accept: accepting is never automatic. |
| kind `dispute`, approval pending | representment above `representment_approval_above_minor`. Approve in Approvals. |
| kind `mandate`, status `escalated` | fraud-related revocation, or the repair message could not be sent (consent, channel). |

## 6. Changing workflow code safely

Workflows that are already running replay their history against new code.

1. Make the change. If it alters the sequence of commands (activities, timers, child workflows, signals handled), wrap it in `if workflow.patched("<change-id>"):`, keeping the old path for existing runs.
2. Run `uv run pytest tests/replay`. A `NondeterminismError` means the change would break running workflows.
3. Record new histories: `NIRANTAR_CAPTURE_HISTORIES=1 uv run pytest tests/e2e`. Commit them with the change.
4. Deploy with `NIRANTAR_BUILD_ID=<git sha>` so workflow task history shows which build handled each step.
5. Remove the old branch of a patch (with `workflow.deprecate_patch`) only after no running workflow started before it.

## 7. Scaling

- Workers: run more processes. They share the task queue `NIRANTAR_TASK_QUEUE`.
- Bridges: run more processes with the same group id. Kafka assigns partitions, ordered per tenant and subject.
- Relays: run more processes safely. Rows are claimed with `FOR UPDATE SKIP LOCKED`.
- Canary bridge: set `NIRANTAR_BRIDGE_TENANTS=ten_a,ten_b` to handle only those tenants.

## 8. Never

- Never set `NIRANTAR_COMMS=mock` in production (the process ignores it when `NIRANTAR_ENV=production` and uses the
  failing `UnconnectedSink` until a real channel exists).
- Never delete `events.outbox` rows to "clear a backlog".
- Never reset consumer offsets to `latest` on the production group: events would be skipped silently.

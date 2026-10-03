# ADR-0012: Data platform — change capture, lakehouse, contracts, orchestration (Phase 2 · P2)

- Status: Accepted · Date: 2026-09-29
- Evidence: tests/unit/test_data_platform.py, tests/integration/test_data_platform.py (real Postgres + Iceberg on
  SeaweedFS + Dagster), tests/integration/test_api.py (sync/health), tests/tenancy (RLS on new tables),
  tests/sandbox (live Razorpay test-mode backfill), browser/API verification of the Data page.

## Problem

Models were trained on simulator data generated in memory. A real product must learn from each business's own
history (its payment provider) and from what Nirantar itself did (debits, contacts, outcomes), with lineage,
validation and privacy — and keep doing it 24/7.

## Decisions

1. **Lakehouse = Apache Iceberg (PyIceberg) on S3-compatible storage (SeaweedFS locally), SQL catalog in
   Postgres (`nirantar_lake` database).** Layers are namespaces: `bronze` (raw, append-only), `silver` (typed,
   deduplicated, contract-checked), `gold` (training-ready). Every table has `tenant_id` and is partitioned by it;
   `Lake` has no method that reads or writes without naming a tenant, and refuses rows of other tenants.
2. **Change capture by transaction id, not timestamps** (migration 0010). A trigger stamps `cdc_txid =
   pg_current_xact_id()` on 13 tables; the extractor reads `cdc_txid >= watermark` and advances the watermark to
   the snapshot's `xmin`, so a transaction still running during extraction is re-read next run — proven by
   `test_change_capture_never_skips_a_transaction_that_commits_late`. Business timestamps are not monotonic here
   (workflows write historical `now`), so a timestamp watermark would silently lose rows.
   *Why not Debezium now:* it needs `wal_level=logical`, Kafka Connect and replication-slot operations; the
   txid approach is correct at our volume with no extra services. Revisit when tables exceed ~10⁷ changed rows/day.
3. **Provider backfill** (`data/sources.py`, verified Razorpay endpoints: payments/subscriptions/customers with
   `from`/`to`/`count≤100`/`skip`; invoices per subscription). Resumable: a window is checkpointed only after its
   rows are in bronze and only if it ended ≥ 2 days ago (recent windows keep changing). A product that the
   account has not enabled (observed: Razorpay test account → subscriptions 401) is recorded per entity; wrong
   keys (every entity rejected) fail the run. Credentials come from `core.provider_accounts.secret_ref`.
4. **PII is minimised at the lake boundary**: names, phones, emails, VPAs, addresses, cards and free-text notes are
   replaced by the tenant-scoped keyed hash already used for `contact_hash`; encrypted columns and display names
   are never extracted.
5. **Data contracts (Pandera) on every silver table.** Builders convert types first (bad values → null) so each
   failure is attributable to a row; failing rows go to `silver.quarantine` with the reason; a missing/unexpected
   column fails the whole run.
6. **Gold `charge_outcomes`**: one row per billing cycle from invoices → Nirantar debits → grouped subscription
   payments (each attempt used once). Two labels with different availability: *first attempt failed* (known at
   the first attempt) and *recovered* (final only when paid or after the 30-day horizon). Health statistics use
   the right denominator for each — the first version divided by final cycles only and understated the failure
   rate (13.6% vs the true 24% on the demo tenant). The same censoring hit the recovery rate in reverse (94% over
   "settled" failures, because recoveries settle when paid but non-recoveries only after 30 days); the recovery
   rate is now computed only on the matured cohort (failures older than the horizon) and shows the cohort size.
   Both were caught while checking the page against the seed's known totals, fixed, and asserted in tests.
7. **Orchestration = Dagster**: one dynamic partition per tenant; assets bronze → silver → gold → health with asset
   checks (contracts, label invariants); `tenants_sensor` (max 25 new tenants/tick) and `hourly_refresh`;
   `QueuedRunCoordinator` caps concurrency at 4 (`infra/dagster/dagster.yaml`, telemetry disabled). Automation is
   on by default in staging/production; in local dev only with `NIRANTAR_AUTOMATION=on`. Assets, the API and
   tests call the same plain functions and all record runs in `ingest.pipeline_runs`.
8. **Readiness gates** (`data/readiness.yaml`) say, per model, what a tenant has vs needs before a per-tenant
   model is trained (P3); shown on the Data page.

## Known limits (honest)

- Silver/gold are full per-tenant rebuilds from bronze (fine for current volumes; incremental merge later).
- The Razorpay test account used here has no payments and Subscriptions is not enabled, so the live backfill
  lands zero rows; real history needs a merchant account (or test payments made through Razorpay's checkout by
  the account owner).
- Cashfree/Stripe backfill sources are not implemented yet.
- Bronze retention/compaction and lake-level encryption keys per tenant are not configured yet.

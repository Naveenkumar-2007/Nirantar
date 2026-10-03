"""Activities for OnboardingWorkflow and ReconciliationSweepWorkflow."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, create_engine, text
from temporalio import activity

from nirantar.db.session import tenant_tx
from nirantar.payments.domain import ProviderAuthError, ProviderError
from nirantar.payments.processing import process_raw_event
from nirantar.payments.providers.resolver import ProviderNotConfigured, ProviderResolver, provider_for
from nirantar.payments.reconciliation import reconcile_open_debits
from nirantar.workflows.platform import OnboardingInput, SweepInput

RETRY_RAW_AFTER = timedelta(minutes=5)


@dataclass
class PlatformDeps:
    engine: Engine
    provider: Any                       # ProviderResolver in services; a fixed provider in tests
    lake: Any = None                    # nirantar.data.lake.Lake (lazy: data extra)
    owner_url: str | None = None        # platform role: lists tenant ids only, never reads tenant data


class PlatformActivities:
    def __init__(self, deps: PlatformDeps) -> None:
        self.d = deps

    def _lake(self) -> Any:
        if self.d.lake is None:
            from nirantar.data.lake import Lake

            self.d.lake = Lake.from_env()
        return self.d.lake

    def _set_onboarding(self, tenant_id: str, state: dict[str, Any]) -> None:
        with tenant_tx(tenant_id, self.d.engine) as c:
            # merge into settings.onboarding (other steps, e.g. payments verification, live there too)
            c.execute(text("UPDATE core.tenants SET settings = jsonb_set(settings, '{onboarding}', "
                           "COALESCE(settings->'onboarding', '{}'::jsonb) || CAST(:s AS jsonb)) WHERE tenant_id=:t"),
                      {"s": json.dumps(state, default=str), "t": tenant_id})

    # ---------------------------------------------------------------- onboarding
    @activity.defn(name="onboarding_mark")
    def onboarding_mark(self, inp: OnboardingInput) -> dict[str, Any]:
        self._set_onboarding(inp.tenant_id, {"status": "running", "started_at": datetime.now(UTC).isoformat()})
        return {"status": "running"}

    @activity.defn(name="onboarding_verify_provider")
    def onboarding_verify_provider(self, inp: OnboardingInput) -> dict[str, Any]:
        """A cheap authenticated read proves the credentials before we import anything."""
        try:
            p = provider_for(self.d.provider, inp.tenant_id)
            now = datetime.now(UTC)
            next(iter(p.list_payments(now - timedelta(days=1), now)), None)
        except (ProviderNotConfigured, ProviderAuthError) as exc:
            return {"ok": False, "reason": f"provider credentials rejected or missing: {exc}"[:300]}
        except ProviderError as exc:
            raise RuntimeError(f"provider unavailable, will retry: {exc}") from exc
        return {"ok": True, "provider": p.name}

    @activity.defn(name="onboarding_import_history")
    def onboarding_import_history(self, inp: OnboardingInput) -> dict[str, Any]:
        from nirantar.data.pipeline import provider_sources, run_tenant

        now = datetime.now(UTC)
        out = run_tenant(self.d.engine, self._lake(), inp.tenant_id, now=now, trigger="onboarding",
                         sources=provider_sources(self.d.engine, inp.tenant_id),
                         backfill_since=now - timedelta(days=inp.backfill_days))
        labels = out["health"]["labels"]
        return {"run_id": out["run_id"], "cycles": labels["cycles"], "history_days": out["health"]["history_days"],
                "ready": [m for m, r in out["health"]["readiness"].items() if r["ready"]]}

    @activity.defn(name="onboarding_features_and_fits")
    def onboarding_features_and_fits(self, inp: OnboardingInput) -> dict[str, Any]:
        from nirantar.features.offline import build_training_set
        from nirantar.features.online import OnlineStore
        from nirantar.ml.tenant_training import _data_source
        from nirantar.retention.fit import fit_retention

        now = datetime.now(UTC)
        ts = build_training_set(self._lake(), inp.tenant_id, now=now, source=_data_source(self.d.engine,
                                                                                          inp.tenant_id))
        online = OnlineStore().materialize(self._lake(), inp.tenant_id, now=now)
        fit = fit_retention(self.d.engine, self._lake(), inp.tenant_id, now=now)
        return {"training_rows": len(ts), "online_entities": online["entities"], "sbg_fitted": "sbg" in fit}

    @activity.defn(name="onboarding_finish")
    def onboarding_finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = {"status": "done" if payload["ok"] else "failed", "finished_at": datetime.now(UTC).isoformat(),
                 **({"summary": payload["summary"]} if payload.get("summary") else {}),
                 **({"reason": payload["reason"]} if payload.get("reason") else {})}
        self._set_onboarding(payload["tenant_id"], state)
        return state

    # ---------------------------------------------------------------- reconciliation sweep
    @activity.defn(name="sweep_list_tenants")
    def sweep_list_tenants(self, inp: SweepInput) -> list[str]:
        url = self.d.owner_url or os.environ.get(
            "DATABASE_OWNER_URL", "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar")
        eng = create_engine(url)
        try:
            with eng.connect() as c:
                rows = c.execute(text("SELECT DISTINCT a.tenant_id FROM core.provider_accounts a JOIN core.tenants t "
                                      "ON t.tenant_id=a.tenant_id WHERE t.status='active' ORDER BY 1")).all()
        finally:
            eng.dispose()
        return [r[0] for r in rows]

    @activity.defn(name="sweep_reconcile_tenant")
    def sweep_reconcile_tenant(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant = payload["tenant_id"]
        try:
            provider = provider_for(self.d.provider, tenant)
        except (ProviderNotConfigured, ValueError) as exc:
            return {"tenant_id": tenant, "skipped": str(exc)[:200]}
        now = datetime.now(UTC)
        try:
            report = reconcile_open_debits(self.d.engine, provider, tenant,
                                           stale_after=timedelta(minutes=int(payload["stale_minutes"])))
            with tenant_tx(tenant, self.d.engine) as c:
                stuck = [r[0] for r in c.execute(text(
                    "SELECT raw_event_id FROM ingest.provider_events WHERE tenant_id=:t AND status='received' "
                    "AND signature_valid AND received_at < :cut ORDER BY received_at LIMIT 200"),
                    {"t": tenant, "cut": now - RETRY_RAW_AFTER})]
            for raw in stuck:
                process_raw_event(self.d.engine, provider, tenant, raw)
            return {"tenant_id": tenant, "debits_changed": report.changed, "events_reprocessed": len(stuck)}
        except ProviderError as exc:      # one tenant's provider outage must not stop the sweep
            return {"tenant_id": tenant, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def all(self) -> list[Any]:
        return [self.onboarding_mark, self.onboarding_verify_provider, self.onboarding_import_history,
                self.onboarding_features_and_fits, self.onboarding_finish, self.sweep_list_tenants,
                self.sweep_reconcile_tenant]


def resolver(engine: Engine) -> ProviderResolver:
    return ProviderResolver(engine)

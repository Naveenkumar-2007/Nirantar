"""Activities for RecoveryBatchWorkflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from temporalio import activity

from nirantar.core.clock import FixedClock
from nirantar.mcp.gateway import ToolGateway
from nirantar.mcp.tools import AGENT_SCOPES, TOOLS
from nirantar.payments.providers.resolver import provider_for
from nirantar.recovery import batch
from nirantar.workflows.recovery import BatchStep


@dataclass
class RecoveryDeps:
    engine: Engine
    provider: Any                       # ProviderResolver in services; a fixed provider in tests
    comms: Any
    environment: str = "local"


class RecoveryActivities:
    def __init__(self, deps: RecoveryDeps) -> None:
        self.d = deps

    @activity.defn(name="rb_items")
    def rb_items(self, step: BatchStep) -> list[str]:
        return batch.treatment_items(self.d.engine, step.tenant_id, step.batch_id)

    @activity.defn(name="rb_execute")
    def rb_execute(self, step: BatchStep) -> dict[str, Any]:
        now = datetime.fromisoformat(step.now_iso)
        gw = ToolGateway(self.d.engine, TOOLS, AGENT_SCOPES,
                         {"engine": self.d.engine, "comms": self.d.comms,
                          "provider": provider_for(self.d.provider, step.tenant_id)},
                         clock=FixedClock(now), environment=self.d.environment)
        return batch.execute_item(self.d.engine, gw, step.tenant_id, step.batch_id, step.item_id, now)

    @activity.defn(name="rb_close")
    def rb_close(self, step: BatchStep) -> dict[str, Any]:
        r = batch.measure_and_close(self.d.engine, step.tenant_id, step.batch_id,
                                    datetime.fromisoformat(step.now_iso))
        return {"batch_id": step.batch_id, "status": r["batch"]["status"], "recovered": r["recovered"]}

    def all(self) -> list[Any]:
        return [self.rb_items, self.rb_execute, self.rb_close]

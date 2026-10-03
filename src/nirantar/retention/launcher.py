"""Start RevivalWorkflows for newly created revival cases, and persist the daily at-risk snapshot.

Workflow ids are deterministic (`revival:<tenant>:<case>`), so re-running a launch never starts a duplicate.
The workflows are executed by a Temporal worker running RevivalActivities (P5 packages it as an always-on service).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.workflows import TASK_QUEUE
from nirantar.workflows.revival import RevivalInput, RevivalWorkflow, revival_workflow_id


async def _start(tenant_id: str, cases: list[dict[str, Any]], address: str) -> dict[str, Any]:
    from temporalio.client import Client
    from temporalio.exceptions import WorkflowAlreadyStartedError

    client = await Client.connect(address)
    started, existing = [], 0
    for x in cases:
        try:
            await client.start_workflow(RevivalWorkflow.run, RevivalInput(tenant_id, x["case_id"],
                                                                          int(x["window_days"])),
                                        id=revival_workflow_id(tenant_id, x["case_id"]), task_queue=TASK_QUEUE)
            started.append(x["case_id"])
        except WorkflowAlreadyStartedError:
            existing += 1
    return {"started": len(started), "already_running": existing, "task_queue": TASK_QUEUE}


def start_revivals(tenant_id: str, cases: list[dict[str, Any]], address: str | None = None) -> dict[str, Any]:
    if not cases:
        return {"started": 0, "already_running": 0, "task_queue": TASK_QUEUE}
    return asyncio.run(_start(tenant_id, cases, address or os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")))


def store_at_risk(engine: Engine, tenant_id: str, report: dict[str, Any], now: datetime) -> str:
    sid = new_id("risk")
    summary = {k: v for k, v in report.items() if k != "at_risk"} | {"at_risk": len(report["at_risk"])}
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ai.at_risk_snapshots (tenant_id, snapshot_id, computed_at, summary, items) "
                       "VALUES (:t, :s, :at, CAST(:sum AS jsonb), CAST(:items AS jsonb))"),
                  {"t": tenant_id, "s": sid, "at": now, "sum": json.dumps(summary),
                   "items": json.dumps(report["at_risk"][:500], default=str)})
    return sid

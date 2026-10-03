"""Worker versioning guard: every recorded workflow history must replay deterministically against the CURRENT
workflow code. A change that would break workflows already running in production fails here; such a change must
be wrapped in `workflow.patched(...)` (old histories keep their old path) before new histories are recorded."""

from __future__ import annotations

import json

import pytest

from tests.replay.capture import HISTORIES

FILES = sorted(HISTORIES.glob("*.json"))


def test_histories_are_recorded_for_every_workflow_type() -> None:
    from nirantar.services.worker import WORKFLOWS

    recorded = {json.loads(f.read_text(encoding="utf-8"))["events"][0]["workflowExecutionStartedEventAttributes"]
                ["workflowType"]["name"] for f in FILES}
    missing = {w.__name__ for w in WORKFLOWS} - recorded
    assert not missing, f"no recorded history for {sorted(missing)}: run the e2e tests with NIRANTAR_CAPTURE_HISTORIES=1"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", FILES, ids=[f.stem for f in FILES])
async def test_history_replays_on_current_code(path: object) -> None:
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer

    from nirantar.services.worker import WORKFLOWS

    assert hasattr(path, "read_text")
    history = WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    await Replayer(workflows=WORKFLOWS).replay_workflow(history)

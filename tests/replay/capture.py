"""Record workflow histories from e2e runs for the replay (determinism) test.

    NIRANTAR_CAPTURE_HISTORIES=1 uv run pytest tests/e2e      # refresh tests/replay/histories/*.json

Commit refreshed histories only together with a deliberate workflow change (guarded by `workflow.patched`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

HISTORIES = Path(__file__).resolve().parent / "histories"


async def capture(handle: Any, name: str) -> None:
    if os.environ.get("NIRANTAR_CAPTURE_HISTORIES") != "1":
        return
    history = await handle.fetch_history()
    HISTORIES.mkdir(parents=True, exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json(), encoding="utf-8")

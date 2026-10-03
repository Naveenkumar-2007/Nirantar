"""Temporal workflows (durable, long-running) and their activities (short, side-effecting)."""

import os

# One queue for every Nirantar workflow: the always-on worker (nirantar.services) registers them all (ADR-0015).
TASK_QUEUE = os.environ.get("NIRANTAR_TASK_QUEUE", "nirantar-main")

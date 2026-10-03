"""Typed, idempotent event consumption with dead-lettering.

A handler receives a validated EventEnvelope. Handler failures are retried a
bounded number of times; poison messages go to the DLQ with the error, so one
bad event never blocks a partition.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from nirantar.contracts.events import EventEnvelope

Handler = Callable[[EventEnvelope], None]


@dataclass
class ProcessResult:
    handled: int = 0
    duplicates: int = 0
    dead_lettered: list[tuple[str, str]] = field(default_factory=list)  # (raw, error)


class SeenStore:
    """Consumer-side dedupe on event_id. Production uses Redis SET NX with TTL."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def first_time(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        return True


def process_message(
    raw: bytes, handler: Handler, seen: SeenStore, result: ProcessResult, max_attempts: int = 3
) -> None:
    try:
        envelope = EventEnvelope.model_validate(json.loads(raw))
    except (ValidationError, json.JSONDecodeError) as exc:
        result.dead_lettered.append((raw.decode(errors="replace"), f"invalid envelope: {exc}"))
        return
    if not seen.first_time(envelope.event_id):
        result.duplicates += 1
        return
    last_error = ""
    for _ in range(max_attempts):
        try:
            handler(envelope)
            result.handled += 1
            return
        except Exception as exc:  # handler errors are data here: retried then dead-lettered
            last_error = f"{type(exc).__name__}: {exc}"
    result.dead_lettered.append((raw.decode(errors="replace"), last_error))

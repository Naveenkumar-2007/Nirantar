"""Event → workflow signal bridge. Consumes payment.* / reply.* events and signals the debit's workflow.

Workflow id convention: debit-<tenant_id>-<debit_id>. Signals are idempotent in the workflow, so
at-least-once event delivery is safe.
"""

from __future__ import annotations

from typing import Any

from temporalio.client import Client

from nirantar.contracts.events import EventEnvelope


def workflow_id(tenant_id: str, debit_id: str) -> str:
    return f"debit-{tenant_id}-{debit_id}"


def debit_signal(event: EventEnvelope) -> tuple[str, dict[str, Any]] | None:
    """(signal name, argument) for an event that concerns a debit's workflow, else None."""
    payload: dict[str, Any] = event.payload
    if not payload.get("debit_id"):
        return None
    if event.event_type in ("payment.captured", "payment.failed"):
        return "payment_update", {"event_type": event.event_type, "event_id": event.event_id,
                                  "error_code": payload.get("error_code"),
                                  "error_reason": payload.get("error_reason"),
                                  "provider_payment_id": payload.get("provider_payment_id")}
    if event.event_type == "reply.received":
        return "customer_reply", {"message_id": event.event_id, "text": payload.get("text", ""),
                                  "promised_date": payload.get("promised_date"), "via": payload.get("via"),
                                  "language": payload.get("language")}
    return None


async def signal_for_event(client: Client, event: EventEnvelope) -> bool:
    sig = debit_signal(event)
    if sig is None:
        return False
    await client.get_workflow_handle(workflow_id(event.tenant_id, event.payload["debit_id"])).signal(*sig)
    return True

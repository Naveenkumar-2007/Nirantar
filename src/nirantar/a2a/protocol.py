"""A2A primitives: agent cards, Ed25519-signed messages, replay protection, lifecycle.

Signature covers the canonical JSON of {task_id, sender, recipient, kind, body, ts, nonce}. A receiver
accepts a message only if: the sender's card is trusted for this tenant and not revoked, the signature
verifies against the card's key, ts is within the freshness window, and (tenant, sender, nonce) is unseen.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, Field

from nirantar.core.canonical import canonical_bytes
from nirantar.core.errors import NirantarError

FRESHNESS = timedelta(minutes=5)


class A2AError(NirantarError):
    pass


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}
ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED, TaskState.INPUT_REQUIRED},
    TaskState.WORKING: {TaskState.INPUT_REQUIRED, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED},
    TaskState.INPUT_REQUIRED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED},
    TaskState.COMPLETED: set(), TaskState.FAILED: set(), TaskState.CANCELED: set(),
}


def check_transition(current: TaskState, new: TaskState) -> None:
    if new not in ALLOWED[current]:
        raise A2AError(f"illegal task transition {current.value} → {new.value}")


class AgentCard(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    organization: str
    description: str
    skills: list[str]
    endpoint: str
    authentication: str = "ed25519-signed-messages"
    public_key: str                        # base64 raw Ed25519 key
    version: str = "1"


@dataclass(frozen=True)
class Identity:
    card: AgentCard
    private_key: Ed25519PrivateKey

    @classmethod
    def create(cls, name: str, organization: str, skills: list[str], endpoint: str, description: str = "") -> Identity:
        key = Ed25519PrivateKey.generate()
        pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        return cls(AgentCard(name=name, organization=organization, description=description or name, skills=skills,
                             endpoint=endpoint, public_key=pub), key)

    def sign(self, task_id: str, recipient: str, kind: str, body: dict[str, Any], now: datetime) -> SignedMessage:
        nonce = secrets.token_hex(16)
        payload = {"task_id": task_id, "sender": self.card.name, "recipient": recipient, "kind": kind,
                   "body": body, "ts": now.isoformat(), "nonce": nonce}
        sig = base64.b64encode(self.private_key.sign(canonical_bytes(payload))).decode()
        return SignedMessage(task_id=task_id, sender=self.card.name, recipient=recipient, kind=kind, body=body,
                             ts=now.isoformat(), nonce=nonce, signature=sig)


class SignedMessage(BaseModel):
    task_id: str
    sender: str
    recipient: str
    kind: str                              # task.create | task.update | task.result | task.cancel | task.input
    body: dict[str, Any]
    ts: str
    nonce: str
    signature: str

    def signed_payload(self) -> bytes:
        return canonical_bytes({k: getattr(self, k) for k in
                                ("task_id", "sender", "recipient", "kind", "body", "ts", "nonce")})


def verify(msg: SignedMessage, card: AgentCard, me: str, now: datetime) -> None:
    if msg.recipient != me:
        raise A2AError("message addressed to another agent")
    if msg.sender != card.name:
        raise A2AError("sender does not match card")
    ts = datetime.fromisoformat(msg.ts)
    if abs(now - ts) > FRESHNESS:
        raise A2AError("stale or future-dated message")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(card.public_key)).verify(
            base64.b64decode(msg.signature), msg.signed_payload())
    except (InvalidSignature, ValueError) as exc:
        raise A2AError("bad signature") from exc

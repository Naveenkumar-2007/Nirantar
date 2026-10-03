"""Channel sinks. The mock sink stands in for WhatsApp Cloud API until credentials exist;
it assigns provider message ids so the Verifier can confirm 'message accepted' (BB-§33)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SentMessage:
    provider_message_id: str
    channel: str
    to_ref: str          # customer id (never the raw phone number in logs)
    text: str
    at: datetime


@dataclass(frozen=True)
class OutboundTemplate:
    """Which registry template a message came from, with the facts that filled it. Channels that need
    pre-approved templates (WhatsApp outside the 24 h window) send these values as template parameters."""
    key: str
    language: str
    values: dict[str, str]


class CommsSink(Protocol):
    def send(self, channel: str, to_ref: str, text: str, at: datetime, *, tenant_id: str | None = None,
             template: OutboundTemplate | None = None) -> str: ...
    def accepted(self, provider_message_id: str) -> bool: ...


@dataclass
class MockCommsSink:
    messages: list[SentMessage] = field(default_factory=list)
    fail_next: bool = False
    channels: frozenset[str] = frozenset({"whatsapp", "sms"})
    templates: list[OutboundTemplate | None] = field(default_factory=list)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def send(self, channel: str, to_ref: str, text: str, at: datetime, *, tenant_id: str | None = None,
             template: OutboundTemplate | None = None) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("mock comms provider unavailable")
        self.templates.append(template)
        mid = f"wamid.mock{next(self._seq):06d}"
        self.messages.append(SentMessage(mid, channel, to_ref, text, at))
        return mid

    def accepted(self, provider_message_id: str) -> bool:
        return any(m.provider_message_id == provider_message_id for m in self.messages)


class ChannelNotConnected(ConnectionError):
    pass


@dataclass
class UnconnectedSink:
    """Used by the always-on worker until a real channel (WhatsApp Cloud API / SMS DLT) is connected (P6).

    Every send fails loudly; the MCP gateway records the action as failed with this reason, so nothing is ever
    reported as delivered that wasn't. The mock sink is only for tests, the demo and local soak runs."""

    reason: str = "no messaging channel connected for this deployment"
    channels: frozenset[str] = frozenset()

    def send(self, channel: str, to_ref: str, text: str, at: datetime, *, tenant_id: str | None = None,
             template: OutboundTemplate | None = None) -> str:
        raise ChannelNotConnected(f"{channel}: {self.reason}")

    def accepted(self, provider_message_id: str) -> bool:
        return False

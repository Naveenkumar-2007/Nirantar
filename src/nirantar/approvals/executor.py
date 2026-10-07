"""Executes an action after a human approved it (maker-checker), with the SAME tool set and agent scope that
proposed it: Nirantar's own agents, an external MCP client (`mcp_client:*`) or an A2A partner (`a2a_partner:*`).

Provider calls use the tenant's own account (ProviderResolver); messages use the deployment's real channel, or
fail loudly with UnconnectedSink until one is connected — an approved action never runs against a simulator unless
a test or demo injected one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from nirantar.mcp.gateway import Tool, ToolGateway


def default_comms(engine: Engine) -> Any:
    """The deployment's real channel: WhatsApp Cloud API when configured (with Sarvam for spoken replies), otherwise
    UnconnectedSink (every send fails loudly). The mock only when NIRANTAR_COMMS=mock outside production."""
    from nirantar.comms.sink import MockCommsSink, UnconnectedSink
    from nirantar.core.demo import is_demo

    local = os.environ.get("NIRANTAR_ENV", "local") != "production"
    if is_demo() or (local and os.environ.get("NIRANTAR_COMMS") == "mock"):
        return MockCommsSink()
    from nirantar.channels.sink import ChannelSink
    from nirantar.channels.whatsapp import WhatsAppCloud

    wa = WhatsAppCloud.from_env()
    if wa is None:
        return UnconnectedSink()
    speech = None
    if os.environ.get("SARVAM_API_KEY"):
        from nirantar.voice.providers import SarvamSpeech

        speech = SarvamSpeech()
    return ChannelSink(engine, wa, speech)


@dataclass
class ApprovalExecutor:
    engine: Engine
    provider: Any = None                 # ProviderResolver, or an injected provider (tests / demo)
    comms: Any = None
    environment: str = field(default_factory=lambda: os.environ.get("NIRANTAR_ENV", "local"))

    def __post_init__(self) -> None:
        if self.provider is None:
            from nirantar.payments.providers.resolver import ProviderResolver

            self.provider = ProviderResolver(self.engine)
        if self.comms is None:
            self.comms = default_comms(self.engine)

    def services(self, tenant_id: str) -> dict[str, Any]:
        from nirantar.payments.providers.resolver import ProviderNotConfigured, provider_for

        try:
            provider = provider_for(self.provider, tenant_id)
        except (ProviderNotConfigured, ValueError):
            provider = None              # tools that need a provider fail with a clear error; reads still work
        from nirantar.voice.exotel import default_client

        return {"engine": self.engine, "provider": provider, "comms": self.comms, "voice": default_client()}

    @staticmethod
    def toolset(agent_id: str) -> tuple[dict[str, Tool], dict[str, frozenset[str]]]:
        if agent_id.startswith("mcp_client:"):
            from nirantar.mcp.server import EXTERNAL_TOOLS, SCOPE_OF

            return EXTERNAL_TOOLS, {agent_id: frozenset(SCOPE_OF)}
        if agent_id.startswith("a2a_partner:"):
            from nirantar.a2a.server import A2A_TOOLS

            return A2A_TOOLS, {agent_id: frozenset(A2A_TOOLS)}
        from nirantar.mcp.tools import AGENT_SCOPES, TOOLS

        return TOOLS, AGENT_SCOPES

    def gateway(self, tenant_id: str, agent_id: str) -> ToolGateway:
        tools, scopes = self.toolset(agent_id)
        return ToolGateway(self.engine, tools, scopes, self.services(tenant_id), environment=self.environment)

"""Nirantar MCP server (P7, ADR-0016): a merchant connects Claude — or any MCP client — to THEIR tenant.

Transport: MCP streamable HTTP (official Python SDK). Auth: OAuth 2.1 + PKCE with dynamic client registration
(nirantar.mcp.oauth); the merchant approves in their dashboard and chooses scopes.

Every tool call goes through the same ToolGateway as Nirantar's own agents: scope → schema → idempotency → Compliance
Guardian → approval → execute → hash-chained audit. The calling agent is recorded as `mcp_client:<client_id>`.

What an external client may do (by OAuth scope):
  nirantar:read   overview, open cases, customer profile, policy dry-run, templates, ledger verification
  nirantar:act    customer messages and case updates (consent, contact windows, fatigue still enforced)
  nirantar:money  payment links, representments, discount offers — ALWAYS held for a human approval in the
                  dashboard, whatever the amount (external agents never move money on their own)
Never exposed: experiment assignment (would bias the holdout), the mandatory pre-debit notice (workflow-owned),
treasury credit draws (gated).
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import replace
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from sqlalchemy import Engine

from nirantar.mcp.gateway import Tool, ToolGateway
from nirantar.mcp.oauth import DEFAULT_SCOPES, SCOPES, NirantarOAuthProvider, OAuthStore
from nirantar.mcp.tools import TOOLS

EXPOSED: dict[str, tuple[str, ...]] = {
    "nirantar:read": ("insights.overview", "cases.list_open", "customer.get_profile", "policy.check_action",
                      "content.get_template", "ledger.verify_credit"),
    "nirantar:act": ("comms.send_whatsapp", "case.record_reply", "mandate.send_repair", "comms.send_winback"),
    "nirantar:money": ("gateway.create_payment_link", "dispute.submit_representment", "retention.create_offer"),
}
SCOPE_OF = {tool: scope for scope, tools in EXPOSED.items() for tool in tools}
# money tools: always a human approval for external callers
EXTERNAL_TOOLS: dict[str, Tool] = {name: replace(TOOLS[name], approval="always")
                                   if scope == "nirantar:money" else TOOLS[name] for name, scope in SCOPE_OF.items()}
INSTRUCTIONS = ("Tools act on the merchant's own Nirantar tenant. Every call passes Nirantar's compliance policy and "
                "audit; money actions return status 'pending_approval' until a person approves them in the "
                "Nirantar dashboard. Amounts and dates always come from Nirantar's records, never from the model.")


class ToolScopeError(ToolError):
    """Anticipated refusal: the client sees the message (not a generic crash)."""


def _signature(tool: Tool) -> inspect.Signature:
    params = [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=field.annotation, default=field)
              for name, field in tool.input_model.model_fields.items()]
    return inspect.Signature(params, return_annotation=dict[str, Any])


def _service_factory(engine: Engine, provider: Any, comms: Any, environment: str) -> Any:
    def services(tenant_id: str) -> dict[str, Any]:
        from nirantar.payments.providers.resolver import ProviderNotConfigured, provider_for

        try:
            resolved = provider_for(provider, tenant_id)
        except (ProviderNotConfigured, ValueError, AssertionError):
            resolved = None                 # read tools work without a connected provider
        return {"engine": engine, "provider": resolved, "comms": comms}
    return services


def build_server(engine: Engine, *, base_url: str, consent_url: str, provider: Any, comms: Any,
                 environment: str = "local", allowed_hosts: list[str] | None = None) -> tuple[MCPServer, OAuthStore]:
    store = OAuthStore(engine, consent_url)
    server: MCPServer = MCPServer(
        name="nirantar", title="Nirantar", instructions=INSTRUCTIONS, version="1",
        auth_server_provider=NirantarOAuthProvider(store),
        auth=AuthSettings(issuer_url=AnyHttpUrl(base_url), resource_server_url=AnyHttpUrl(f"{base_url}/mcp"),
                          validate_token_resource=False,     # tokens are only ever issued by this server, for it
                          client_registration_options=ClientRegistrationOptions(
                              enabled=True, valid_scopes=list(SCOPES), default_scopes=DEFAULT_SCOPES),
                          revocation_options=RevocationOptions(enabled=True), required_scopes=["nirantar:read"]))
    services = _service_factory(engine, provider, comms, environment)

    for name, tool in EXTERNAL_TOOLS.items():
        scope = SCOPE_OF[name]

        def make(name: str = name, scope: str = scope) -> Any:
            async def call(**kwargs: Any) -> dict[str, Any]:
                token = get_access_token()
                tenant = getattr(token, "tenant_id", None)
                if token is None or tenant is None:
                    raise ToolScopeError("not authenticated")
                if scope not in token.scopes:
                    raise ToolScopeError(f"this connection was not granted {scope}")
                agent = f"mcp_client:{token.client_id}"
                gw = ToolGateway(engine, EXTERNAL_TOOLS, {agent: frozenset(SCOPE_OF)}, services(tenant),
                                 environment=environment)
                args = {k: v for k, v in kwargs.items()}
                r = await asyncio.to_thread(gw.call, tenant_id=tenant, agent_id=agent, tool_name=name, args=args,
                                            case_id=args.get("case_id"))
                out: dict[str, Any] = {"status": r.status, "action_id": r.action_id, "output": r.output}
                if r.approval_id:
                    out["approval_id"] = r.approval_id
                    out["message"] = "held for a human approval in the Nirantar dashboard"
                if r.error:
                    out["error"] = r.error
                if r.decision is not None:
                    out["policy"] = {"outcome": r.decision.outcome.value,
                                     "hits": [{"policy_id": h.policy_id, "message": h.message}
                                              for h in r.decision.hits]}
                return out

            call.__signature__ = _signature(EXTERNAL_TOOLS[name])  # type: ignore[attr-defined]
            call.__name__ = name.replace(".", "_")
            return call

        server.add_tool(make(), name=name, description=f"{tool.description} [requires {scope}]",
                        structured_output=True)
    return server, store


def asgi_app(server: MCPServer, allowed_hosts: list[str]) -> Any:
    return server.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
        allowed_origins=[f"http://{h}" for h in allowed_hosts] + [f"https://{h}" for h in allowed_hosts]))


def main() -> None:
    import uvicorn
    from sqlalchemy import create_engine

    from nirantar.approvals.executor import default_comms
    from nirantar.payments.providers.resolver import ProviderResolver

    port = int(os.environ.get("NIRANTAR_MCP_PORT", "18100"))
    base = os.environ.get("NIRANTAR_MCP_URL", f"http://localhost:{port}")
    engine = create_engine(os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://nirantar_app:nirantar_app@localhost:25432/nirantar"), pool_pre_ping=True)
    server, _ = build_server(engine, base_url=base,
                             consent_url=os.environ.get("NIRANTAR_MCP_CONSENT_URL",
                                                        "http://localhost:3010/connect/mcp"),
                             provider=ProviderResolver(engine), comms=default_comms(engine),
                             environment=os.environ.get("NIRANTAR_ENV", "local"))
    hosts = [h for h in os.environ.get("NIRANTAR_MCP_ALLOWED_HOSTS", f"localhost:{port},127.0.0.1:{port}").split(",")]
    uvicorn.run(asgi_app(server, hosts), host="127.0.0.1", port=port, log_level="warning")

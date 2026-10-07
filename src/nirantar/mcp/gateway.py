"""MCP tool gateway — the only action boundary for agents (BB-§25).

call(agent, tool, args) pipeline:
  1. scope: tool must be in the agent's allow-list (and not forbidden)
  2. validate args against the tool's input schema
  3. idempotency: (agent, tool, case, params_hash) → an existing executed action returns its stored result
  4. compliance: tools that touch customers/money are evaluated by the policy engine
  5. approval: REQUIRE_APPROVAL → needs a signed token (redeemed exactly once) or a pending approval is created
  6. execute the handler OUTSIDE the DB transaction (provider/network calls never hold locks)
  7. record result + hash-chained audit (tool request, policy decision, approval, provider response)
Each phase commits separately, so a crash mid-way leaves an action row that tells us exactly where it stopped.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.approvals.service import ApprovalError, redeem, request_approval
from nirantar.audit.chain import AuditChain
from nirantar.core.canonical import sha256_hex
from nirantar.core.clock import Clock, SystemClock
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlAuditStore
from nirantar.policy.engine import ActionRequest, Decision, Outcome, TenantPolicyConfig, evaluate
from nirantar.settings.service import policy_config as settings_policy_config


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    agent_id: str
    case_id: str | None
    now: datetime
    services: Mapping[str, Any]          # provider, comms sink, experiment ids… injected by the worker


PolicyBuilder = Callable[[Connection, ToolContext, BaseModel], ActionRequest | None]
Handler = Callable[[ToolContext, BaseModel], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    scope: str                            # read | write | money
    idempotent: bool = True
    timeout_s: float = 20.0
    policy: PolicyBuilder | None = None   # None = no customer/money effect (reads)
    approval: str = "policy"              # policy | always | never
    records_decision: bool = False        # output carries a Guardian decision (dry-runs) → persist it for audit


@dataclass(frozen=True)
class ToolResult:
    status: str                           # executed | denied | pending_approval | needs_info | failed | invalid
    action_id: str | None
    output: dict[str, Any] = field(default_factory=dict)
    decision: Decision | None = None
    approval_id: str | None = None
    error: str | None = None


class ScopeError(PermissionError):
    pass


@dataclass
class ToolGateway:
    engine: Engine
    tools: dict[str, Tool]
    agent_scopes: dict[str, frozenset[str]]
    services: dict[str, Any]
    # Tenant policy = platform/regulatory defaults tightened by the tenant's versioned settings (ADR-0011).
    policy_config: Callable[[Connection, str], TenantPolicyConfig] = settings_policy_config
    clock: Clock = field(default_factory=SystemClock)
    environment: str = "local"

    def list_tools(self, agent_id: str) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "scope": t.scope,
                 "input_schema": t.input_model.model_json_schema()}
                for n, t in sorted(self.tools.items()) if n in self.agent_scopes.get(agent_id, frozenset())]

    def preview(self, *, tenant_id: str, agent_id: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dry run of the scope, schema and policy phases with NO writes: would this call run, need approval, or be
        denied — and why. Same checks as `call`, so an operator's preview cannot disagree with the real run (except
        for state that changes in between, e.g. a contact window closing)."""
        if tool_name not in self.agent_scopes.get(agent_id, frozenset()):
            raise ScopeError(f"agent {agent_id} is not allowed to call {tool_name}")
        tool = self.tools[tool_name]
        try:
            parsed = tool.input_model.model_validate(args)
        except ValidationError as exc:
            return {"outcome": "INVALID", "messages": [str(exc)[:300]], "policy_ids": []}
        ctx = ToolContext(tenant_id, agent_id, None, self.clock.now(), self.services)
        decision: Decision | None = None
        with tenant_tx(tenant_id, self.engine) as c:
            if tool.policy is not None:
                request = tool.policy(c, ctx, parsed)
                if request is not None:
                    decision = evaluate(ActionRequest(**{**request.__dict__, "environment": self.environment}),
                                        self.policy_config(c, tenant_id))
        ids = [h.policy_id for h in decision.hits] if decision else []
        if tool.approval == "always":
            return {"outcome": Outcome.REQUIRE_APPROVAL.value, "messages": ["always needs a person's approval"],
                    "policy_ids": ids}
        if decision is None:
            return {"outcome": Outcome.ALLOW.value, "messages": [], "policy_ids": []}
        return {"outcome": decision.outcome.value, "messages": [h.message for h in decision.hits], "policy_ids": ids,
                "policy_version": decision.policy_version,
                "retry_after": decision.retry_after.isoformat() if decision.retry_after else None}

    def call(self, *, tenant_id: str, agent_id: str, tool_name: str, args: dict[str, Any],
             case_id: str | None = None, approval_token: str | None = None,
             idempotency_key: str | None = None) -> ToolResult:
        if tool_name not in self.agent_scopes.get(agent_id, frozenset()):
            raise ScopeError(f"agent {agent_id} is not allowed to call {tool_name}")
        tool = self.tools[tool_name]
        try:
            parsed = tool.input_model.model_validate(args)
        except ValidationError as exc:
            return ToolResult("invalid", None, error=str(exc)[:500])
        now = self.clock.now()
        params = parsed.model_dump(mode="json")
        params_hash = sha256_hex({"tool": tool_name, "params": params})
        # Reads are never deduplicated (their answer depends on the current state and time); writes are.
        key = idempotency_key or (new_id("rd") if tool.scope == "read" else
                                  sha256_hex({"agent": agent_id, "tool": tool_name, "case": case_id, "p": params_hash}))
        ctx = ToolContext(tenant_id, agent_id, case_id, now, self.services)

        # ---- phase 1: record intent + policy decision (one transaction)
        with tenant_tx(tenant_id, self.engine) as c:
            existing = c.execute(
                text("SELECT action_id, status, result, approval_id FROM ops.actions "
                     "WHERE tenant_id=:t AND idempotency_key=:k"), {"t": tenant_id, "k": key}).one_or_none()
            # Executed actions are never repeated. Denials are NOT cached: they depend on time (contact windows),
            # consent and fatigue, so a retry re-evaluates policy on the same action row.
            if existing is not None and existing.status in ("executed", "verified"):
                return ToolResult("executed", existing.action_id, dict(existing.result or {}),
                                  approval_id=existing.approval_id)
            action_id = existing.action_id if existing else new_id("act")
            decision: Decision | None = None
            if tool.policy is not None:
                request = tool.policy(c, ctx, parsed)
                if request is not None:
                    decision = evaluate(ActionRequest(**{**request.__dict__, "environment": self.environment}),
                                        self.policy_config(c, tenant_id))
            if tool.approval == "always":
                decision = Decision(Outcome.REQUIRE_APPROVAL, decision.hits if decision else (),
                                    decision.policy_version if decision else "n/a")
            status = "proposed"
            approval_id = None
            if decision is not None and decision.outcome == Outcome.DENY:
                status = "denied"
            elif decision is not None and decision.outcome == Outcome.REQUIRE_MORE_INFORMATION:
                status = "proposed"
            elif decision is not None and decision.outcome == Outcome.REQUIRE_APPROVAL:
                if approval_token:
                    try:
                        approval_id = redeem(c, approval_token, tenant_id, tool_name, params_hash, now)
                    except ApprovalError as exc:
                        return ToolResult("denied", action_id, decision=decision, error=f"approval: {exc}")
                else:
                    status = "pending_approval"
            if existing is None:
                c.execute(
                    text("INSERT INTO ops.actions (tenant_id, action_id, case_id, agent_id, tool_name, params, "
                         "params_hash, idempotency_key, policy_decision, policy_version, status, approval_id, "
                         "created_at, updated_at) VALUES (:t, :a, :c, :ag, :tn, CAST(:p AS jsonb), :ph, :k, :pd, :pv, "
                         ":s, :ap, :now, :now)"),
                    {"t": tenant_id, "a": action_id, "c": case_id, "ag": agent_id, "tn": tool_name,
                     "p": json.dumps(params), "ph": params_hash, "k": key,
                     "pd": decision.outcome.value if decision else None,
                     "pv": decision.policy_version if decision else None, "s": status, "ap": approval_id, "now": now},
                )
            else:
                c.execute(text("UPDATE ops.actions SET status=:s, approval_id=coalesce(:ap, approval_id), "
                               "updated_at=:now WHERE tenant_id=:t AND action_id=:a"),
                          {"s": status, "ap": approval_id, "t": tenant_id, "a": action_id, "now": now})
            if status == "pending_approval" and approval_token is None:
                reason = "; ".join(h.message for h in decision.hits) if decision else "approval required"
                approval_id = request_approval(c, tenant_id, action_id, tool_name, params_hash, f"agent:{agent_id}",
                                               reason or "approval required", now)
                c.execute(text("UPDATE ops.actions SET approval_id=:ap WHERE tenant_id=:t AND action_id=:a"),
                          {"ap": approval_id, "t": tenant_id, "a": action_id})
            self._audit(c, tenant_id, agent_id, "tool.requested", self.clock, {
                "action_id": action_id, "tool": tool_name, "params_hash": params_hash,
                "decision": decision.outcome.value if decision else None,
                "policy_version": decision.policy_version if decision else None,
                "hits": [h.policy_id for h in decision.hits] if decision else []})
        if status == "denied":
            return ToolResult("denied", action_id, decision=decision)
        if status == "pending_approval":
            return ToolResult("pending_approval", action_id, decision=decision, approval_id=approval_id)
        if decision is not None and decision.outcome == Outcome.REQUIRE_MORE_INFORMATION:
            return ToolResult("needs_info", action_id, decision=decision)

        # ---- phase 2: execute outside any transaction
        try:
            output = tool.handler(ctx, parsed)
            final, error = "executed", None
        except Exception as exc:  # tool failures are recorded, never swallowed silently
            output, final, error = {}, "failed", f"{type(exc).__name__}: {exc}"[:500]

        # ---- phase 3: record outcome + audit
        # Persist the failure reason with the action: an operator must see WHY a tool failed without log digging.
        recorded = output if error is None else {**output, "error": error}
        # Dry-run decisions (e.g. "WhatsApp denied: outside contact window") are compliance events too.
        dry_decision = output.get("outcome") if tool.records_decision else None
        with tenant_tx(tenant_id, self.engine) as c:
            c.execute(text("UPDATE ops.actions SET status=:s, result=CAST(:r AS jsonb), provider_request_id=:pr, "
                           "policy_decision=coalesce(:pd, policy_decision), "
                           "policy_version=coalesce(:pv, policy_version), updated_at=:now "
                           "WHERE tenant_id=:t AND action_id=:a"),
                      {"s": final, "r": json.dumps(recorded, default=str), "pr": output.get("provider_ref"),
                       "pd": dry_decision, "pv": output.get("policy_version") if dry_decision else None,
                       "t": tenant_id, "a": action_id, "now": self.clock.now()})
            self._audit(c, tenant_id, agent_id, f"tool.{final}", self.clock, {
                "action_id": action_id, "tool": tool_name, "approval_id": approval_id,
                "output_hash": sha256_hex(output), "error": error})
        return ToolResult(final, action_id, output, decision, approval_id, error)

    @staticmethod
    def _audit(conn: Connection, tenant_id: str, agent_id: str, action: str, clock: Clock,
               data: dict[str, Any]) -> None:
        # Audit time = the gateway clock (workflow/business time), so replays and backfills stay truthful.
        AuditChain(SqlAuditStore(conn), clock).append(tenant_id, f"agent:{agent_id}", action, data)

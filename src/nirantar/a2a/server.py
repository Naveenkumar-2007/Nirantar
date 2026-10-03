"""A2A v1.0 server (P7, ADR-0016) on the official `a2a-sdk`: JSON-RPC binding at POST /a2a, Agent Card at
/.well-known/agent-card.json (platform) and /a2a/tenants/{tenant}/agent-card.json (per merchant).

Who may call: A2A partners — e.g. a customer's own AI agent — that the merchant registered. Each partner holds a
bearer key (HTTP bearer security scheme) bound to ONE tenant and the `a2a_partner` role, so the tenant always comes
from authentication, never from the request. A partner sees only its own tasks.

Skills (each one executes through the MCP ToolGateway: policy, approvals and audit apply):
  subscription.status  read a customer's subscriptions and next debit (needs the customer's consent reference)
  subscription.pause   pause for 1-3 months; ALWAYS waits for the merchant's approval. The task stays WORKING
                       and becomes COMPLETED / FAILED when the merchant decides (GetTask reflects it).

Request format: a user Message with one data Part: {"skill": ..., "customer_ref": ..., "consent_ref": ...,
"months": n}. A missing field → TASK_STATE_INPUT_REQUIRED with what is needed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from a2a.helpers.proto_helpers import get_data_parts, new_data_artifact, new_task_from_user_message, new_text_message
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.task_store import TaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    ListTasksRequest,
    ListTasksResponse,
    Role,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    Task,
    TaskState,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import Engine, text
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from nirantar.db.session import tenant_tx
from nirantar.mcp.gateway import Tool, ToolGateway
from nirantar.mcp.tools import TOOLS
from nirantar.security.keys import AuthError, authenticate_api_key
from nirantar.security.rbac import Permission, Principal

RPC_PATH = "/a2a"
SKILLS: dict[str, tuple[str, list[str]]] = {          # A2A skill → (gateway tool, required data fields)
    "subscription.status": ("subscription.status_for_customer", ["customer_ref", "consent_ref"]),
    "subscription.pause": ("subscription.request_pause", ["customer_ref", "consent_ref", "months"]),
}
# delegated requests that change something always wait for the merchant
A2A_TOOLS: dict[str, Tool] = {tool: replace(TOOLS[tool], approval="always") if TOOLS[tool].scope != "read"
                              else TOOLS[tool] for tool, _ in SKILLS.values()}
STATE_NAME = {TaskState.TASK_STATE_SUBMITTED: "submitted", TaskState.TASK_STATE_WORKING: "working",
              TaskState.TASK_STATE_INPUT_REQUIRED: "input-required",
              TaskState.TASK_STATE_AUTH_REQUIRED: "auth-required",
              TaskState.TASK_STATE_COMPLETED: "completed", TaskState.TASK_STATE_FAILED: "failed",
              TaskState.TASK_STATE_CANCELED: "canceled", TaskState.TASK_STATE_REJECTED: "rejected"}


def _principal(ctx: ServerCallContext) -> Principal:
    p = ctx.state.get("principal")
    if not isinstance(p, Principal):
        raise PermissionError("unauthenticated")
    return p


def _agent_id(p: Principal) -> str:
    return f"a2a_partner:{p.principal_id}"


# ---------------------------------------------------------------- authentication
class PartnerAuth:
    """ASGI middleware: POST /a2a needs `Authorization: Bearer <partner key>` with the a2a:call permission."""

    def __init__(self, app: ASGIApp, engine: Engine) -> None:
        self.app, self.engine = app, engine

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == RPC_PATH:
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            principal = None
            if auth.lower().startswith("bearer "):
                try:
                    principal = await asyncio.to_thread(authenticate_api_key, self.engine, auth[7:].strip())
                except AuthError:
                    principal = None
            if principal is None or not principal.can(Permission.A2A_CALL):
                resp = JSONResponse({"error": "a valid A2A partner key is required"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Bearer realm="nirantar-a2a"'})
                await resp(scope, receive, send)
                return
            scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


class PartnerContextBuilder(DefaultServerCallContextBuilder):
    """The SDK's context (headers → A2A-Version negotiation, extensions) plus the authenticated partner."""

    def build(self, request: Request) -> ServerCallContext:
        ctx = super().build(request)
        p = request.scope.get("state", {}).get("principal")
        ctx.state["principal"] = p
        ctx.tenant = p.tenant_id if p else ""
        return ctx


# ---------------------------------------------------------------- task store (Postgres, RLS, owner-scoped)
class PostgresTaskStore(TaskStore):
    """Tasks live in ops.a2a_tasks under the tenant's RLS scope; a partner only ever sees its own tasks.
    `get` also folds in the merchant's approval decision for a task waiting on one (pull model)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _save(self, task: Task, p: Principal) -> None:
        body = MessageToDict(task)
        first = get_data_parts(task.history[0].parts) if task.history else []
        skill = str(first[0].get("skill", "")) if first and isinstance(first[0], dict) else ""
        with tenant_tx(p.tenant_id, self.engine) as c:
            c.execute(text(
                "INSERT INTO ops.a2a_tasks (tenant_id, task_id, skill, counterparty, direction, state, request, "
                "context_id, task_proto, updated_at) VALUES (:t, :id, :sk, :cp, 'inbound', :s, CAST(:r AS jsonb), :cx, "
                "CAST(:b AS jsonb), now()) ON CONFLICT (tenant_id, task_id) DO UPDATE SET state=EXCLUDED.state, "
                "task_proto=EXCLUDED.task_proto, skill=CASE WHEN EXCLUDED.skill='' THEN ops.a2a_tasks.skill ELSE "
                "EXCLUDED.skill END, updated_at=now() WHERE ops.a2a_tasks.counterparty=EXCLUDED.counterparty"),
                {"t": p.tenant_id, "id": task.id, "sk": skill or "unknown", "cp": _agent_id(p),
                 "s": STATE_NAME.get(task.status.state, "submitted"),
                 "r": json.dumps(MessageToDict(task.history[0]) if task.history else {}), "cx": task.context_id,
                 "b": json.dumps(body)})

    def _get(self, task_id: str, p: Principal) -> Task | None:
        with tenant_tx(p.tenant_id, self.engine) as c:
            row = c.execute(text("SELECT task_proto, state, action_id FROM ops.a2a_tasks WHERE task_id=:id AND "
                                 "counterparty=:cp"), {"id": task_id, "cp": _agent_id(p)}).one_or_none()
            if row is None or row.task_proto is None:
                return None
            task = ParseDict(row.task_proto if isinstance(row.task_proto, dict) else json.loads(row.task_proto),
                             Task())
            if row.state == "working" and row.action_id:
                action = c.execute(text("SELECT status, result FROM ops.actions WHERE action_id=:a"),
                                   {"a": row.action_id}).one_or_none()
                if action is not None and action.status in ("executed", "denied", "failed", "expired"):
                    result = action.result if isinstance(action.result, dict) else json.loads(action.result or "{}")
                    done = action.status == "executed"
                    task.status.state = TaskState.TASK_STATE_COMPLETED if done else TaskState.TASK_STATE_FAILED
                    note = ("Approved by the merchant and done." if done
                            else f"The merchant did not approve ({action.status}).")
                    task.status.message.CopyFrom(new_text_message(note, role=Role.ROLE_AGENT))
                    if done:
                        task.artifacts.append(new_data_artifact("result", result))
                    c.execute(text("UPDATE ops.a2a_tasks SET state=:s, task_proto=CAST(:b AS jsonb), "
                                   "result=CAST(:r AS jsonb), updated_at=now() WHERE task_id=:id"),
                              {"s": STATE_NAME[task.status.state], "b": json.dumps(MessageToDict(task)),
                               "r": json.dumps(result), "id": task_id})
        return task

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await asyncio.to_thread(self._save, task, _principal(context))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return await asyncio.to_thread(self._get, task_id, _principal(context))

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        p = _principal(context)
        size = params.page_size or 50

        def q() -> list[Any]:
            with tenant_tx(p.tenant_id, self.engine) as c:
                return list(c.execute(text("SELECT task_id FROM ops.a2a_tasks WHERE counterparty=:cp AND "
                                           "task_proto IS NOT NULL ORDER BY updated_at DESC LIMIT :n"),
                                      {"cp": _agent_id(p), "n": min(size, 100)}).scalars())
        ids = await asyncio.to_thread(q)
        tasks = [t for t in [await self.get(i, context) for i in ids] if t is not None]
        return ListTasksResponse(tasks=tasks, page_size=len(tasks), total_size=len(tasks))

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        raise PermissionError("tasks are records; they are not deleted")


# ---------------------------------------------------------------- the agent
class NirantarExecutor(AgentExecutor):
    def __init__(self, engine: Engine, services: Any, environment: str = "local") -> None:
        self.engine, self.services, self.environment = engine, services, environment

    def _link_action(self, p: Principal, task_id: str, skill: str, action_id: str | None) -> None:
        """Remember which gateway action the task waits on. The SDK may persist the task AFTER this runs, so this
        is an upsert; the task store's later save keeps action_id (it never overwrites it)."""
        with tenant_tx(p.tenant_id, self.engine) as c:
            c.execute(text("INSERT INTO ops.a2a_tasks (tenant_id, task_id, skill, counterparty, direction, state, "
                           "request, action_id) VALUES (:t, :id, :sk, :cp, 'inbound', 'working', '{}'::jsonb, :a) "
                           "ON CONFLICT (tenant_id, task_id) DO UPDATE SET action_id=EXCLUDED.action_id"),
                      {"t": p.tenant_id, "id": task_id, "sk": skill, "cp": _agent_id(p), "a": action_id})

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        p = _principal(context.call_context)
        assert context.message is not None
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        data = next((d for d in get_data_parts(context.message.parts) if isinstance(d, dict)), {})
        skill = data.get("skill")
        if skill not in SKILLS:
            await updater.reject(new_text_message(f"Unknown skill {skill!r}. Supported: {sorted(SKILLS)}.",
                                                  role=Role.ROLE_AGENT))
            return
        tool, required = SKILLS[skill]
        missing = [f for f in required if data.get(f) in (None, "")]
        if missing:
            await updater.requires_input(new_text_message(
                f"Please send a data part with: {', '.join(missing)}.", role=Role.ROLE_AGENT))
            return
        args = {f: data[f] for f in required}
        gw = ToolGateway(self.engine, A2A_TOOLS, {_agent_id(p): frozenset(A2A_TOOLS)}, self.services(p.tenant_id),
                         environment=self.environment)
        r = await asyncio.to_thread(gw.call, tenant_id=p.tenant_id, agent_id=_agent_id(p), tool_name=tool,
                                    args=args, idempotency_key=f"a2a:{p.principal_id}:{task.id}")
        if r.status == "executed":
            await updater.add_artifact([*new_data_artifact("result", r.output).parts], name="result")
            await updater.complete()
        elif r.status == "pending_approval":
            await asyncio.to_thread(self._link_action, p, task.id, skill, r.action_id)
            await updater.start_work(new_text_message(
                "Waiting for the merchant's approval. Check back with GetTask.", role=Role.ROLE_AGENT))
        elif r.status == "denied":
            reasons = "; ".join(h.message for h in r.decision.hits) if r.decision else "policy"
            await updater.reject(new_text_message(f"Not allowed: {reasons}", role=Role.ROLE_AGENT))
        else:
            await updater.failed(new_text_message(r.error or str(r.output.get("error") or r.status),
                                                  role=Role.ROLE_AGENT))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()


# ---------------------------------------------------------------- cards and app
def agent_card(base_url: str, *, name: str = "Nirantar", tenant: str = "") -> AgentCard:
    return AgentCard(
        name=name, version="1.0.0",
        description="Recurring-payments agent for Indian subscription businesses: answers a customer's agent about "
                    "their subscription and takes pause requests, under the merchant's policies and approvals.",
        provider=AgentProvider(organization="Nirantar", url=base_url),
        supported_interfaces=[AgentInterface(url=f"{base_url}{RPC_PATH}", protocol_binding="JSONRPC",
                                             protocol_version="1.0", tenant=tenant)],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        security_schemes={"partnerKey": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(
            scheme="bearer", description="Partner key issued by the merchant (Nirantar → A2A partners)."))},
        security_requirements=[SecurityRequirement(schemes={"partnerKey": StringList(list=[])})],
        default_input_modes=["application/json"], default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(id="subscription.status", name="Subscription status", tags=["subscriptions"],
                       description="A customer's subscriptions, amounts and next debit date. Data part: "
                                   "{skill, customer_ref, consent_ref}.",
                       examples=['{"skill": "subscription.status", "customer_ref": "C-1042", '
                                 '"consent_ref": "consent-7781"}']),
            AgentSkill(id="subscription.pause", name="Pause subscription", tags=["subscriptions"],
                       description="Pause for 1-3 months on the customer's behalf; the merchant approves first. "
                                   "Data part: {skill, customer_ref, consent_ref, months}."),
        ])


def build_routes(engine: Engine, *, base_url: str, services: Any, environment: str = "local") -> list[Route]:
    card = agent_card(base_url)
    handler = DefaultRequestHandlerV2(agent_executor=NirantarExecutor(engine, services, environment),
                                      task_store=PostgresTaskStore(engine), agent_card=card)

    async def tenant_card(request: Request) -> Response:
        tenant = request.path_params["tenant"]

        def name() -> str | None:
            try:
                with tenant_tx(tenant, engine) as c:
                    n = c.execute(text("SELECT name FROM core.tenants WHERE tenant_id=:t"), {"t": tenant})
                    return n.scalar_one_or_none()
            except ValueError:
                return None
        merchant = await asyncio.to_thread(name)
        if merchant is None:
            return JSONResponse({"error": "unknown merchant"}, status_code=404)
        return JSONResponse(MessageToDict(agent_card(base_url, name=f"{merchant} (via Nirantar)", tenant=tenant)))

    return [*create_agent_card_routes(card),
            Route("/a2a/tenants/{tenant}/agent-card.json", tenant_card, methods=["GET"]),
            *create_jsonrpc_routes(handler, RPC_PATH, context_builder=PartnerContextBuilder())]

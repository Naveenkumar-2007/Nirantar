"""Nirantar HTTP API.

Run: uv run uvicorn nirantar.api.app:create_app --factory --port 8080
Auth: `Authorization: Bearer nk_<tenant>.<key_id>.<secret>`; permissions via RBAC. Platform console routes
use `X-Platform-Key` and a dedicated platform role for cross-tenant health (never tenant data contents).
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from nirantar.api import queries
from nirantar.api.deps import (
    Services,
    current_identity,
    default_services,
    platform_admin,
    require,
    services,
    tenant_conn,
)
from nirantar.approvals.service import ApprovalError, decide
from nirantar.audit.chain import AuditChain
from nirantar.core.clock import FixedClock
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.db.stores import SqlAuditStore
from nirantar.experiments.service import analyze
from nirantar.mcp.gateway import ToolGateway
from nirantar.payments.ingress import ingest_webhook
from nirantar.policy.engine import TenantPolicyConfig
from nirantar.security.oidc import Identity
from nirantar.security.rbac import Permission, Principal
from nirantar.settings import learning, templates
from nirantar.settings import runtime as tenant_runtime
from nirantar.settings import service as settings_service
from nirantar.settings.schema import NAMESPACES

REPO = Path(__file__).resolve().parents[3]
MAX_LIMIT = 100


class DecideIn(BaseModel):
    grant: bool
    note: str | None = None


class AskIn(BaseModel):
    question: str


class BusinessIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    segment: str = Field(default="subscription",
                         pattern=r"^(subscription|saas|edtech|media|fitness|insurance|lending|other)$")


class RazorpayKeysIn(BaseModel):
    key_id: str = Field(min_length=10, max_length=64)
    key_secret: str = Field(min_length=12, max_length=128)


class InviteIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    role: str


class PartnerIn(BaseModel):
    name: str = Field(min_length=3, max_length=80)


class ConsentIn(BaseModel):
    approve: bool
    scopes: list[str] | None = None


class ReplayIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


class SettingsIn(BaseModel):
    value: dict[str, Any]
    reason: str
    expected_version: int


class RetireIn(BaseModel):
    reason: str


class PlanIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    amount_rupees: float = Field(ge=1, le=100_000)
    interval: str = Field(pattern=r"^(weekly|monthly|quarterly|yearly)$")
    collection_method: str = Field(default="payment_link", pattern=r"^(payment_link|mandate)$")
    description: str | None = Field(default=None, max_length=300)


class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")              # E.164
    email: str | None = Field(default=None, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    language: str = Field(default="en", pattern=r"^(en|hi|te)$")
    reference: str | None = Field(default=None, max_length=64)
    whatsapp_consent: bool
    consent_source: str | None = Field(default=None, max_length=200)   # where/how the customer agreed


class EnrollIn(BaseModel):
    customer_id: str = Field(pattern=r"^cus_[0-9A-Z]{26}$")
    plan_id: str = Field(pattern=r"^pln_[0-9A-Z]{26}$")
    start_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class CheckoutConfirmIn(BaseModel):
    razorpay_order_id: str = Field(min_length=6, max_length=64)
    razorpay_payment_id: str = Field(min_length=6, max_length=64)
    razorpay_signature: str = Field(min_length=16, max_length=128)


class ImportIn(BaseModel):
    csv: str = Field(min_length=10, max_length=1_000_000)
    dry_run: bool = True


class InvoiceIn(BaseModel):
    customer_id: str = Field(pattern=r"^cus_[0-9A-Z]{26}$")
    number: str = Field(min_length=1, max_length=40)
    amount_rupees: float = Field(ge=1, le=10_000_000)
    issued_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    due_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None = Field(default=None, max_length=300)


class InvoiceStatusIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class BatchItemsIn(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=500)


class BatchLaunchIn(BatchItemsIn):
    name: str = Field(min_length=1, max_length=120)
    holdout_pct: float = Field(default=20, ge=0, le=50)
    window_days: int = Field(default=7, ge=1, le=30)


class OperatorReplyBody(BaseModel):
    text: str = Field(min_length=2, max_length=1000)


class TemplateProposalIn(BaseModel):
    key: str
    language: str
    body: str


def _actor(p: Principal) -> str:
    return f"{p.kind}:{p.principal_id}"


def _policy_dict(cfg: TenantPolicyConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dataclasses.asdict(cfg).items():
        out[k] = [t.strftime("%H:%M") for t in v] if k.endswith("_window") else v
    return out


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=_json_default, ensure_ascii=False).encode()


def approval_executor(s: Services) -> Any:
    """One per app: approved actions run with the proposing agent's tools and the tenant's own provider."""
    from nirantar.approvals.executor import ApprovalExecutor

    ex = s.extra.get("approval_executor")
    if ex is None:
        ex = s.extra["approval_executor"] = ApprovalExecutor(s.engine, s.provider, s.comms)
    return ex


def _migration_head() -> str:
    """The newest migration shipped with this build (file names start with the revision id)."""
    versions = Path(__file__).resolve().parent.parent / "db" / "migrations" / "versions"
    return max(p.name.split("_", 1)[0] for p in versions.glob("[0-9]*_*.py"))


def create_app(svc: Services | None = None) -> FastAPI:
    from nirantar.core.demo import assert_safe

    assert_safe()                                   # a demo process with real credentials refuses to start
    app = FastAPI(title="Nirantar API", version="0.1.0", default_response_class=JSON)
    app.state.services = svc or default_services()
    s_root: Services = app.state.services
    app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(queries.BadCursor)
    async def bad_cursor(_: Request, exc: queries.BadCursor) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    from nirantar.api.ratelimit import RateLimiter

    limiter: RateLimiter = app.state.services.extra.get("rate_limiter") or RateLimiter()

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Any:
        """Unauthenticated surfaces only (pay page, webhooks); signed-in traffic is bounded by identity instead."""
        client = request.client.host if request.client else "unknown"
        ok, retry = limiter.allow(request.url.path, request.method, client)
        if not ok:
            return JSONResponse({"detail": "too many requests"}, status_code=429,
                                headers={"Retry-After": str(retry)})
        return await call_next(request)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        response.headers["x-response-ms"] = str(int((time.perf_counter() - t0) * 1000))
        return response

    @app.get("/health")
    def health(s: Services = Depends(services)) -> dict[str, Any]:
        try:
            with s.engine.connect() as c:
                c.execute(text("select 1"))
            db = "ok"
        except Exception as exc:  # health reports, never raises
            db = f"error: {type(exc).__name__}"
        return {"status": "ok" if db == "ok" else "degraded", "db": db}

    @app.get("/ready")
    def ready(s: Services = Depends(services)) -> JSONResponse:
        """Readiness: 200 only when the database answers AND is migrated to the revision this build expects."""
        expected = _migration_head()
        try:
            with s.engine.connect() as c:
                at = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        except Exception as exc:  # readiness reports, never raises
            return JSONResponse({"ready": False, "db": f"error: {type(exc).__name__}"}, status_code=503)
        ok = at == expected
        return JSONResponse({"ready": ok, "db": "ok", "migration": at, "expected_migration": expected},
                            status_code=200 if ok else 503)

    @app.get("/version")
    def version() -> dict[str, Any]:
        return {"service": "nirantar-api", "build": os.environ.get("NIRANTAR_BUILD_ID", "dev"),
                "environment": os.environ.get("NIRANTAR_ENV", "local"), "migration": _migration_head(),
                "demo": os.environ.get("NIRANTAR_DEMO") == "1"}

    # ------------------------------------------------------------- merchant API
    @app.get("/v1/overview")
    def overview(c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.overview(c, datetime.now(UTC))

    @app.get("/v1/debits")
    def debits(status: str | None = None, limit: int = Query(25, ge=1, le=MAX_LIMIT), cursor: str | None = None,
               c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.debits(c, status, limit, cursor)

    @app.get("/v1/debits/{debit_id}")
    def debit_detail(debit_id: str, c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        d = queries.debit_detail(c, debit_id)
        if d is None:
            raise HTTPException(404, "debit not found")
        return d

    @app.get("/v1/customers")
    def customers(limit: int = Query(25, ge=1, le=MAX_LIMIT), cursor: str | None = None,
                  c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.customers(c, limit, cursor)

    @app.get("/v1/customers/{customer_id}")
    def customer_detail(customer_id: str, p: Principal = Depends(require(Permission.READ)),
                        c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        d = queries.customer_360(c, p.tenant_id, customer_id)
        if d is None:
            raise HTTPException(404, "customer not found")
        return d

    @app.get("/v1/conversations")
    def conversation_list(limit: int = Query(50, ge=1, le=MAX_LIMIT), c: Connection = Depends(tenant_conn)
                          ) -> dict[str, Any]:
        return {"items": queries.conversations(c, limit)}

    @app.get("/v1/conversations/{customer_id}")
    def conversation_thread(customer_id: str, p: Principal = Depends(require(Permission.READ)),
                            c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        """The WhatsApp thread with one customer. Bodies are decrypted only here, for members with read access."""
        cust = c.execute(text("SELECT display_name, preferred_language FROM billing.customers WHERE customer_id=:c"),
                         {"c": customer_id}).one_or_none()
        if cust is None:
            raise HTTPException(404, "customer not found")
        msgs = queries.thread(c, p.tenant_id, customer_id)
        last_in = max((m["created_at"] for m in msgs if m["direction"] == "inbound"), default=None)
        return {"customer_id": customer_id, "display_name": cust.display_name, "language": cust.preferred_language,
                "window_open_until": (last_in + timedelta(hours=24)) if last_in else None, "messages": msgs}

    @app.post("/v1/conversations/{customer_id}/reply")
    def conversation_reply(customer_id: str, body: OperatorReplyBody,
                           p: Principal = Depends(require(Permission.CUSTOMERS_WRITE)),
                           s: Services = Depends(services)) -> dict[str, Any]:
        """Human takeover: a person answers in the inbox, through the same gateway, policy and audit as the agents."""
        gw: ToolGateway = s.extra.get("operator_gateway") or approval_executor(s).gateway(p.tenant_id,
                                                                                         "human_operator")
        r = gw.call(tenant_id=p.tenant_id, agent_id="human_operator", tool_name="comms.operator_reply",
                    args={"customer_id": customer_id, "text": body.text, "operator": _actor(p)},
                    idempotency_key=new_id("opr"))
        if r.status == "executed":
            return {"status": "sent", "action_id": r.action_id, "message_id": r.output.get("provider_ref")}
        why = [h.message for h in r.decision.hits] if r.decision else []
        detail = r.error or r.output.get("error") or "; ".join(why) or r.status
        if "TemplateNotApproved" in detail:
            detail = "the customer has not written in the last 24 hours: WhatsApp allows only approved templates"
        raise HTTPException(409, {"status": r.status, "reason": detail, "action_id": r.action_id})

    # ---------------------------------------------------------------- plans and enrollment (P8.6, ADR-0022)
    @app.get("/v1/plans")
    def plans_list(c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        from nirantar.billing.plans import list_plans

        return {"items": list_plans(c)}

    @app.post("/v1/plans")
    def plans_create(body: PlanIn, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                     s: Services = Depends(services)) -> dict[str, Any]:
        from decimal import Decimal

        from nirantar.billing.plans import NewPlan, PlanError, create_plan
        from nirantar.core.money import Money

        amount = Money.of(str(Decimal(str(body.amount_rupees)).quantize(Decimal("0.01"))))
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                pid = create_plan(c, p.tenant_id, NewPlan(body.name, amount, body.interval, body.collection_method,
                                                          body.description), _actor(p))
                AuditChain(SqlAuditStore(c), FixedClock(datetime.now(UTC))).append(
                    p.tenant_id, _actor(p), "plan.created", {"plan_id": pid, "amount_minor": amount.minor})
        except PlanError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"plan_id": pid}

    @app.post("/v1/customers")
    def customers_create(body: CustomerIn, p: Principal = Depends(require(Permission.CUSTOMERS_WRITE)),
                         s: Services = Depends(services)) -> dict[str, Any]:
        """Add a customer. Contact details are encrypted with the business's key; WhatsApp consent must be explicit
        and is stored with its evidence (who recorded it, where the customer agreed, when)."""
        from nirantar.billing.service import NewCustomer, create_customer

        if body.whatsapp_consent and not (body.consent_source or "").strip():
            raise HTTPException(422, "say how the customer agreed to WhatsApp messages (e.g. 'signup form')")
        now = datetime.now(UTC)
        consents: dict[str, Any] = {"whatsapp": body.whatsapp_consent}
        if body.whatsapp_consent:
            consents["evidence"] = {"whatsapp": {"source": body.consent_source, "recorded_by": _actor(p),
                                                 "at": now.isoformat()}}
        with tenant_tx(p.tenant_id, s.engine) as c:
            if body.reference and c.execute(text("SELECT 1 FROM billing.customers WHERE external_ref=:r"),
                                            {"r": body.reference}).first():
                raise HTTPException(409, "a customer with this reference already exists")
            cid = create_customer(c, p.tenant_id, NewCustomer(body.reference or new_id("ref"), body.name.strip(),
                                                              body.phone, body.email, body.language,
                                                              consents=consents))
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                p.tenant_id, _actor(p), "customer.created", {"customer_id": cid,
                                                             "whatsapp_consent": body.whatsapp_consent})
        return {"customer_id": cid}

    @app.post("/v1/customers/import")
    def customers_import(body: ImportIn, p: Principal = Depends(require(Permission.CUSTOMERS_WRITE)),
                         s: Services = Depends(services)) -> dict[str, Any]:
        """CSV import: dry run first (per-row problems, nothing written), then the real run (valid rows, one
        transaction). Enrolling needs subscriptions:write as well."""
        from nirantar.billing.importer import ImportError_, parse, run

        now = datetime.now(UTC)
        try:
            enrolls = any(r.plan for r in parse(body.csv))
        except ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc
        if enrolls and not body.dry_run and not p.can(Permission.SUBSCRIPTIONS_WRITE):
            raise HTTPException(403, "enrolling customers on plans needs subscriptions:write")
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                out = run(c, p.tenant_id, body.csv, actor=_actor(p), now=now, dry_run=body.dry_run)
                if not body.dry_run and out.get("created"):
                    AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                        p.tenant_id, _actor(p), "customers.imported",
                        {k: out[k] for k in ("created", "enrolled", "invalid")})
        except ImportError_ as exc:
            raise HTTPException(422, str(exc)) from exc
        return out

    @app.post("/v1/subscriptions")
    def subscriptions_enroll(body: EnrollIn, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                             s: Services = Depends(services)) -> dict[str, Any]:
        """Put a customer on a plan. The first debit is created at once when it falls due within 3 days; its workflow
        sends the payment link on the due date (pay-by-link) and verifies the payment with the provider."""
        from datetime import date as _date

        from nirantar.billing.plans import PlanError, enroll

        now = datetime.now(UTC)
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                out = enroll(c, p.tenant_id, body.customer_id, body.plan_id, _date.fromisoformat(body.start_on), now)
                AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                    p.tenant_id, _actor(p), "subscription.enrolled", {"subscription_id": out["subscription_id"],
                                                                      "plan_id": body.plan_id})
        except (PlanError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return out

    @app.post("/v1/subscriptions/{subscription_id}/cancel")
    def subscriptions_cancel(subscription_id: str, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                             s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.billing.plans import PlanError, cancel

        now = datetime.now(UTC)
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                n = cancel(c, subscription_id, now)
                AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                    p.tenant_id, _actor(p), "subscription.cancelled", {"subscription_id": subscription_id})
        except PlanError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"subscription_id": subscription_id, "status": "cancelled", "debits_cancelled": n}

    @app.post("/v1/debits/{debit_id}/payment-request")
    def debit_payment_request(debit_id: str, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                              s: Services = Depends(services)) -> dict[str, Any]:
        """Send (or re-send) the payment link for a debit now, through the gateway as the billing agent."""
        gw: ToolGateway = s.extra.get("billing_gateway") or approval_executor(s).gateway(p.tenant_id, "billing_agent")
        r = gw.call(tenant_id=p.tenant_id, agent_id="billing_agent", tool_name="billing.send_payment_request",
                    args={"debit_id": debit_id}, idempotency_key=new_id("prq"))
        if r.status != "executed":
            why = [h.message for h in r.decision.hits] if r.decision else []
            raise HTTPException(409, {"status": r.status, "reason": r.error or r.output.get("error") or "; ".join(why)
                                      or r.status})
        return {"url": r.output["url"], "sent": r.output["sent"], "channel_error": r.output.get("channel_error"),
                "text": r.output.get("text")}

    @app.post("/v1/payments/check")
    def payments_check(p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                       s: Services = Depends(services)) -> dict[str, Any]:
        """Ask the provider about every open payment link now (the same check the workflows run every 30 minutes)."""
        from nirantar.payments.providers.resolver import provider_for
        from nirantar.payments.reconciliation import reconcile_payment_requests

        provider = s.extra.get("provider_override") or provider_for(approval_executor(s).provider, p.tenant_id)
        r = reconcile_payment_requests(s.engine, provider, p.tenant_id)
        return {"scanned": r.scanned, "changed": r.changed, "events": r.events, "errors": r.errors[:5]}

    # ------------------------------------------------------------- public pay page (no sign-in: the token is the key)
    def _pay_provider(s: Services, token: str) -> Any:
        from nirantar.billing.checkout import parse_token
        from nirantar.payments.providers.resolver import provider_for

        override = s.extra.get("provider_override")
        return override if override is not None else provider_for(approval_executor(s).provider, parse_token(token)[0])

    @app.get("/v1/public/pay/{token}")
    def pay_view(token: str, s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.billing.checkout import PayError, view

        try:
            provider = _pay_provider(s, token)
            return view(s.engine, token, getattr(provider, "checkout_key", None))
        except PayError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/public/pay/{token}/confirm")
    def pay_confirm(token: str, body: CheckoutConfirmIn, s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.billing.checkout import PayError, confirm

        try:
            return confirm(s.engine, _pay_provider(s, token), token, order_id=body.razorpay_order_id,
                           payment_id=body.razorpay_payment_id, signature=body.razorpay_signature,
                           now=datetime.now(UTC))
        except PayError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---------------------------------------------------------------- payment health (P10, ADR-0024)
    # ---------------------------------------------------------------- B2B receivables (P10, ADR-0025)
    @app.post("/v1/invoices")
    def invoices_create(body: InvoiceIn, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        """Issue an invoice: booked as a receivable now; its reminder ladder starts from the event it emits."""
        from datetime import date as _date
        from decimal import Decimal

        from nirantar.core.money import Money
        from nirantar.receivables.service import InvoiceError, create_invoice

        now = datetime.now(UTC)
        amount = Money.of(str(Decimal(str(body.amount_rupees)).quantize(Decimal("0.01"))))
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                iid = create_invoice(c, p.tenant_id, customer_id=body.customer_id, number=body.number, amount=amount,
                                     issued_on=_date.fromisoformat(body.issued_on),
                                     due_on=_date.fromisoformat(body.due_on), description=body.description,
                                     actor=_actor(p), now=now)
                AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                    p.tenant_id, _actor(p), "invoice.issued", {"invoice_id": iid, "amount_minor": amount.minor})
        except (InvoiceError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"invoice_id": iid}

    @app.get("/v1/invoices")
    def invoices_list(status: str | None = Query(None, pattern=r"^(open|partially_paid|paid|disputed|written_off)$"),
                      c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        from nirantar.receivables.service import ageing

        rows = [dict(r) for r in c.execute(text(
            "SELECT i.invoice_id, i.number, i.customer_id, cu.display_name, i.issued_on, i.due_on, i.amount_minor, "
            "i.paid_minor, i.status, i.updated_at, (SELECT step FROM ops.invoice_chases x WHERE "
            "x.tenant_id=i.tenant_id AND x.invoice_id=i.invoice_id ORDER BY created_at DESC LIMIT 1) AS last_step "
            "FROM billing.invoices i JOIN billing.customers cu ON cu.tenant_id=i.tenant_id AND "
            "cu.customer_id=i.customer_id WHERE (CAST(:s AS text) IS NULL OR i.status = :s) "
            "ORDER BY i.due_on ASC LIMIT 500"), {"s": status}).mappings()]
        for r in rows:
            for k in ("issued_on", "due_on", "updated_at"):
                r[k] = r[k].isoformat()
        return {"items": rows, "ageing": ageing(c, datetime.now(UTC).date())}

    @app.get("/v1/invoices/{invoice_id}")
    def invoice_detail(invoice_id: str, c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        inv = c.execute(text("SELECT i.*, cu.display_name FROM billing.invoices i JOIN billing.customers cu ON "
                             "cu.tenant_id=i.tenant_id AND cu.customer_id=i.customer_id WHERE i.invoice_id=:i"),
                        {"i": invoice_id}).mappings().one_or_none()
        if inv is None:
            raise HTTPException(404, "invoice not found")
        chases = [dict(r) for r in c.execute(text(
            "SELECT step, status, action_id, detail, created_at FROM ops.invoice_chases WHERE invoice_id=:i "
            "ORDER BY created_at"), {"i": invoice_id}).mappings()]
        payments = [dict(r) for r in c.execute(text(
            "SELECT provider_payment_id, amount_minor, status, method, created_at FROM billing.payments WHERE "
            "invoice_id=:i ORDER BY created_at"), {"i": invoice_id}).mappings()]
        requests = [dict(r) for r in c.execute(text(
            "SELECT request_id, url, amount_minor, status, created_at, paid_at FROM billing.payment_requests WHERE "
            "invoice_id=:i ORDER BY created_at DESC"), {"i": invoice_id}).mappings()]
        from nirantar.receivables.service import ladder_dates

        return {"invoice": dict(inv), "ladder": [{"step": st, "on": d.isoformat()} for st, d in
                                                 ladder_dates(inv["due_on"])],
                "chases": chases, "payments": payments, "requests": requests}

    @app.post("/v1/invoices/{invoice_id}/send")
    def invoice_send(invoice_id: str, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                     s: Services = Depends(services)) -> dict[str, Any]:
        gw: ToolGateway = s.extra.get("receivables_gateway") or approval_executor(s).gateway(p.tenant_id,
                                                                                            "receivables_agent")
        r = gw.call(tenant_id=p.tenant_id, agent_id="receivables_agent", tool_name="billing.send_invoice_request",
                    args={"invoice_id": invoice_id, "step": "manual"}, idempotency_key=new_id("ivs"))
        if r.status != "executed":
            why = [h.message for h in r.decision.hits] if r.decision else []
            raise HTTPException(409, {"status": r.status, "reason": r.error or r.output.get("error") or "; ".join(why)
                                      or r.status})
        return {"url": r.output["url"], "sent": r.output["sent"], "owed_minor": r.output["owed_minor"],
                "channel_error": r.output.get("channel_error")}

    def _invoice_status(p: Principal, s: Services, invoice_id: str, status: str, reason: str | None) -> dict[str, Any]:
        from nirantar.receivables.service import InvoiceError, set_status

        now = datetime.now(UTC)
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                set_status(c, invoice_id, status, now, reason)
                AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                    p.tenant_id, _actor(p), f"invoice.{status}", {"invoice_id": invoice_id, "reason": reason})
        except InvoiceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"invoice_id": invoice_id, "status": status}

    @app.post("/v1/invoices/{invoice_id}/dispute")
    def invoice_dispute(invoice_id: str, body: InvoiceStatusIn,
                        p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        """The customer disputes the invoice: chasing stops at the ladder's next check, until it is resolved."""
        return _invoice_status(p, s, invoice_id, "disputed", body.reason or "disputed by the customer")

    @app.post("/v1/invoices/{invoice_id}/resolve")
    def invoice_resolve(invoice_id: str, p: Principal = Depends(require(Permission.SUBSCRIPTIONS_WRITE)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        return _invoice_status(p, s, invoice_id, "open", None)

    @app.post("/v1/invoices/{invoice_id}/write-off")
    def invoice_write_off(invoice_id: str, body: InvoiceStatusIn,
                          p: Principal = Depends(require(Permission.APPROVALS_DECIDE)),
                          s: Services = Depends(services)) -> dict[str, Any]:
        """Writing money off is a finance decision (approver role), and it is audited with the reason."""
        if not (body.reason or "").strip():
            raise HTTPException(422, "a reason is required to write off an invoice")
        return _invoice_status(p, s, invoice_id, "written_off", body.reason)

    # ---------------------------------------------------------------- live voice (P11, ADR-0026)
    def _voice_speech() -> Any:
        injected = s_root.extra.get("voice_speech")
        if injected is not None:
            return injected
        from nirantar.voice.providers import SarvamSpeech

        return SarvamSpeech()

    def _voice_action(ctx: dict[str, Any], action: str) -> None:
        """The caller promised: send the payment link on WhatsApp right away (they asked for it)."""
        if action != "send_link" or not ctx.get("debit_id"):
            return
        gw: ToolGateway = s_root.extra.get("billing_gateway") or approval_executor(s_root).gateway(
            ctx["tenant_id"], "billing_agent")
        gw.call(tenant_id=ctx["tenant_id"], agent_id="billing_agent", tool_name="billing.send_payment_request",
                args={"debit_id": ctx["debit_id"], "occasion": "promise"},
                idempotency_key=f"voice-link:{ctx['call_id']}")

    def _voice_finish(ctx: dict[str, Any], record: Any, intent: str | None) -> None:
        from nirantar.voice import calls

        calls.finish(s_root.engine, ctx, record.turns, intent, datetime.now(UTC))

    if os.environ.get("SARVAM_API_KEY") or s_root.extra.get("voice_speech") is not None:
        from nirantar.voice import calls as _calls
        from nirantar.voice.gateway import build_app as _voice_gateway

        _voice_gateway(_voice_speech, _voice_action,
                       context_for=lambda token: _calls.context(s_root.engine, token),
                       on_finish=_voice_finish, app=app)

    @app.post("/webhooks/exotel/{tenant_id}")
    async def exotel_status(tenant_id: str, request: Request) -> dict[str, Any]:
        """Exotel call status (terminal events). Unsigned by Exotel, so it can only move a call WE placed in this
        business to a terminal status — it never creates calls or touches money."""
        from nirantar.voice.exotel import STATUS

        try:
            body = await request.json()
        except ValueError:
            form = await request.form()
            body = dict(form)
        sid = str(body.get("CallSid") or "")
        status = STATUS.get(str(body.get("Status") or "").lower())
        if not sid or status is None or not tenant_id.startswith("ten_"):
            raise HTTPException(400, "unrecognised callback")
        duration = body.get("ConversationDuration") or body.get("Duration")
        with tenant_tx(tenant_id, s_root.engine) as c:
            n = c.execute(text(
                "UPDATE comms.calls SET status=:s, duration_s=coalesce(:d, duration_s), ended_at=coalesce(ended_at, "
                "now()) WHERE tenant_id=:t AND provider='exotel' AND call_sid=:sid AND status NOT IN "
                "('completed','no_answer','busy','failed','canceled')"),
                {"s": status, "d": int(duration) if str(duration or "").isdigit() else None, "t": tenant_id,
                 "sid": sid}).rowcount
        return {"updated": n}

    @app.get("/v1/today")
    def today_view(c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.today(c, datetime.now(UTC))

    @app.get("/v1/payment-health")
    def payment_health(days: int = Query(30, ge=1, le=90), c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        """Issuer incidents across the platform (aggregates only) and what they meant for THIS business: debits
        failed by the bank, the honest retry message sent after recovery, and the money that came back."""
        since = datetime.now(UTC) - timedelta(days=days)
        incidents = [dict(r) for r in c.execute(text(
            "SELECT incident_id, rail, issuer, started_at, detected_at, ended_at, status, baseline_rate, peak_rate, "
            "attempts, failures, detector, recovery_done FROM core.payment_incidents WHERE started_at >= :s "
            "ORDER BY started_at DESC LIMIT 100"), {"s": since}).mappings()]
        mine = {r.incident_id: r for r in c.execute(text(
            "SELECT x.incident_id, count(*) AS affected, count(*) FILTER (WHERE x.status='executed') AS contacted, "
            "count(*) FILTER (WHERE d.status='succeeded') AS recovered, coalesce(sum(d.amount_minor) FILTER (WHERE "
            "d.status='succeeded'), 0) AS recovered_minor FROM ops.incident_recoveries x JOIN billing.debits d ON "
            "d.tenant_id=x.tenant_id AND d.debit_id=x.debit_id GROUP BY x.incident_id"))}
        for inc in incidents:
            m = mine.get(inc["incident_id"])
            inc["yours"] = {"affected": m.affected, "contacted": m.contacted, "recovered": m.recovered,
                            "recovered_minor": int(m.recovered_minor)} if m else None
            for k in ("started_at", "detected_at", "ended_at"):
                inc[k] = inc[k].isoformat() if inc[k] else None
        return {"incidents": incidents, "open": sum(i["status"] == "open" for i in incidents)}

    # ---------------------------------------------------------------- Recovery Command Centre (P8.5, ADR-0021)
    async def _temporal(s: Services) -> Any:
        from temporalio.client import Client

        client = s.extra.get("temporal")
        if client is None:
            client = s.extra["temporal"] = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
        return client

    @app.get("/v1/recovery/queue")
    def recovery_queue(min_risk: float = Query(0.3, ge=0.05, le=0.95), c: Connection = Depends(tenant_conn)
                       ) -> dict[str, Any]:
        from nirantar.recovery import queue

        return queue.build(c, datetime.now(UTC), min_risk=min_risk)

    @app.post("/v1/recovery/plan")
    def recovery_plan(body: BatchItemsIn, p: Principal = Depends(require(Permission.AGENTS_OPERATE)),
                      s: Services = Depends(services)) -> dict[str, Any]:
        """Dry run: every message and the compliance decision for it, without sending anything."""
        from nirantar.recovery import batch

        gw: ToolGateway = s.extra.get("recovery_gateway") or approval_executor(s).gateway(p.tenant_id, batch.AGENT)
        try:
            return batch.plan(s.engine, gw, p.tenant_id, body.item_ids, datetime.now(UTC))
        except batch.BatchError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/recovery/batches")
    async def recovery_launch(body: BatchLaunchIn, p: Principal = Depends(require(Permission.AGENTS_OPERATE)),
                              s: Services = Depends(services)) -> dict[str, Any]:
        import asyncio as _asyncio

        from nirantar.recovery import batch
        from nirantar.workflows import TASK_QUEUE
        from nirantar.workflows.recovery import BatchInput, RecoveryBatchWorkflow, batch_workflow_id

        try:
            out = await _asyncio.to_thread(batch.launch, s.engine, p.tenant_id, body.item_ids, name=body.name,
                                           holdout_bp=round(body.holdout_pct * 100), window_days=body.window_days,
                                           actor=_actor(p), now=datetime.now(UTC))
        except batch.BatchError as exc:
            raise HTTPException(422, str(exc)) from exc
        wid = batch_workflow_id(p.tenant_id, out["batch_id"])
        try:
            client = await _temporal(s)
            await client.start_workflow(RecoveryBatchWorkflow.run,
                                        BatchInput(p.tenant_id, out["batch_id"], out["ends_at"]), id=wid,
                                        task_queue=s.extra.get("task_queue", TASK_QUEUE))
        except Exception as exc:     # never leave a "running" batch that nothing will run
            reason = json.dumps({"error": f"workflow engine unavailable: {type(exc).__name__}"})

            def _abort() -> None:
                with tenant_tx(p.tenant_id, s.engine) as c:
                    c.execute(text("UPDATE ops.recovery_batches SET status='stopped', stopped_by='system', "
                                   "stopped_at=now(), summary = summary || CAST(:e AS jsonb) WHERE batch_id=:b"),
                              {"e": reason, "b": out["batch_id"]})
            await _asyncio.to_thread(_abort)
            raise HTTPException(503, "the workflow engine is not reachable; nothing was sent") from exc

        def _record() -> None:
            with tenant_tx(p.tenant_id, s.engine) as c:
                c.execute(text("UPDATE ops.recovery_batches SET workflow_id=:w WHERE batch_id=:b"),
                          {"w": wid, "b": out["batch_id"]})
                AuditChain(SqlAuditStore(c), FixedClock(datetime.now(UTC))).append(
                    p.tenant_id, _actor(p), "recovery.batch_launched",
                    {"batch_id": out["batch_id"], "items": len(body.item_ids), "holdout_pct": body.holdout_pct})
        await _asyncio.to_thread(_record)
        return {**out, "workflow_id": wid, "status": "running"}

    @app.get("/v1/recovery/batches")
    def recovery_batches(c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        from nirantar.recovery import batch

        return {"items": batch.batches(c)}

    @app.get("/v1/recovery/batches/{batch_id}")
    def recovery_batch(batch_id: str, p: Principal = Depends(require(Permission.READ)),
                       c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        """The proof report: verified money recovered, treatment vs holdout, every action, and the audit chain."""
        from nirantar.core.errors import AuditChainBroken
        from nirantar.recovery import batch

        try:
            report = batch.proof(c, p.tenant_id, batch_id)
        except batch.BatchError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            report["audit"] = {"valid": True, "records": AuditChain(SqlAuditStore(c)).verify(p.tenant_id)}
        except AuditChainBroken as exc:
            report["audit"] = {"valid": False, "error": str(exc)}
        return report

    @app.post("/v1/recovery/batches/{batch_id}/stop")
    async def recovery_stop(batch_id: str, p: Principal = Depends(require(Permission.AGENTS_OPERATE)),
                            s: Services = Depends(services)) -> dict[str, Any]:
        import asyncio as _asyncio

        from nirantar.recovery import batch
        from nirantar.workflows.recovery import RecoveryBatchWorkflow, batch_workflow_id

        try:
            out = await _asyncio.to_thread(batch.stop, s.engine, p.tenant_id, batch_id, _actor(p), datetime.now(UTC))
        except batch.BatchError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            client = await _temporal(s)
            await client.get_workflow_handle(batch_workflow_id(p.tenant_id, batch_id)).signal(
                RecoveryBatchWorkflow.stop)
        except Exception:            # the DB flag alone already stops every remaining item
            out["signal"] = "not delivered; remaining items stop at their next step"

        def _record() -> None:
            with tenant_tx(p.tenant_id, s.engine) as c:
                AuditChain(SqlAuditStore(c), FixedClock(datetime.now(UTC))).append(
                    p.tenant_id, _actor(p), "recovery.batch_stopped", {"batch_id": batch_id, "skipped": out["skipped"]})
        await _asyncio.to_thread(_record)
        return out

    @app.get("/v1/agents/activity")
    def activity(limit: int = Query(50, ge=1, le=MAX_LIMIT), cursor: str | None = None, agent: str | None = None,
                 c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.agent_activity(c, limit, cursor, agent)

    @app.get("/v1/approvals")
    def approvals(status: str = "pending", c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return {"items": queries.approvals(c, status)}

    @app.post("/v1/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, body: DecideIn,
                        p: Principal = Depends(require(Permission.APPROVALS_DECIDE)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            try:
                token = decide(c, p, approval_id, body.grant, now)
            except ApprovalError as exc:
                raise HTTPException(409, str(exc)) from exc
            action = c.execute(text("SELECT x.agent_id, x.tool_name, x.params, x.case_id, x.idempotency_key FROM "
                                    "ops.approvals a JOIN ops.actions x ON x.tenant_id=a.tenant_id AND "
                                    "x.action_id=a.action_id WHERE a.approval_id=:a"), {"a": approval_id}).one()
        if token is None:
            return {"approval_id": approval_id, "status": "denied"}
        # a test/demo may inject a ready gateway for Nirantar's own agents; otherwise the approval executor builds
        # the proposing agent's own tool set with the tenant's real provider account
        injected: ToolGateway | None = s.extra.get("gateway")
        internal = not action.agent_id.startswith(("mcp_client:", "a2a_partner:"))
        gateway = injected if injected is not None and internal else approval_executor(s).gateway(p.tenant_id,
                                                                                                  action.agent_id)
        result = gateway.call(tenant_id=p.tenant_id, agent_id=action.agent_id, tool_name=action.tool_name,
                              args=dict(action.params), case_id=action.case_id, approval_token=token,
                              idempotency_key=action.idempotency_key)
        return {"approval_id": approval_id, "status": "granted", "executed": result.status == "executed",
                "action_status": result.status, "output": result.output}

    @app.get("/v1/experiments/{experiment_id}")
    def experiment(experiment_id: str, p: Principal = Depends(require(Permission.READ)),
                   c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return analyze(c, p.tenant_id, experiment_id)

    @app.get("/v1/compliance")
    def compliance(days: int = Query(30, ge=1, le=365), c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return queries.compliance(c, datetime.now(UTC) - timedelta(days=days))

    @app.get("/v1/compliance/policies")
    def policies(_: Principal = Depends(require(Permission.READ))) -> dict[str, Any]:
        import yaml

        out = []
        for f in ("policies.yaml", "internal-policies.yaml"):
            data = yaml.safe_load((REPO / "docs" / "compliance" / f).read_text(encoding="utf-8"))
            recs = data["policies"] if isinstance(data, dict) else data
            out += [{"policy_id": r["policy_id"], "rule": r.get("rule"), "effective_date": r.get("effective_date"),
                     "status": r.get("verification_status", "governance"),
                     "source": r["source"].get("title") if isinstance(r.get("source"), dict) else r.get("source")}
                    for r in recs]
        return {"items": out}

    @app.get("/v1/audit")
    def audit(limit: int = Query(50, ge=1, le=MAX_LIMIT), p: Principal = Depends(require(Permission.AUDIT_READ)),
              c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return {"items": queries.audit(c, limit)}

    @app.get("/v1/audit/verify")
    def audit_verify(p: Principal = Depends(require(Permission.AUDIT_READ)),
                     c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        try:
            return {"valid": True, "records": AuditChain(SqlAuditStore(c)).verify(p.tenant_id)}
        except Exception as exc:  # a broken chain is a finding to report, not a server error
            return {"valid": False, "error": str(exc)}

    @app.get("/v1/models")
    def models(_: Principal = Depends(require(Permission.READ))) -> dict[str, Any]:
        f = REPO / "evals" / "results" / "ml_latest.json"
        rag = REPO / "evals" / "results" / "rag_latest.json"
        voice = REPO / "evals" / "results" / "voice_latest.json"
        def load(p: Path) -> Any:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

        return {"ml": load(f), "rag": load(rag), "voice": load(voice)}

    @app.post("/v1/assistant/ask")
    def ask(body: AskIn, p: Principal = Depends(require(Permission.READ)),
            s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.rag.answer import answer
        from nirantar.rag.store import retrieve

        if s.llm is None:
            raise HTTPException(503, "assistant unavailable: no LLM configured")
        with tenant_tx(p.tenant_id, s.engine) as c:
            hits = retrieve(c, body.question, k=5)
        res = answer(body.question, hits, s.llm)
        return {"status": res.status, "answer": res.answer, "citations": res.citations,
                "rejected_citations": res.rejected_citations}

    # ------------------------------------------------------------- tenant configuration (ADR-0011)
    @app.get("/v1/settings")
    def get_settings(p: Principal = Depends(require(Permission.READ)),
                     c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        rt = tenant_runtime.load(c, p.tenant_id)
        return {"namespaces": settings_service.all_settings(c, p.tenant_id),
                "effective": {"risk_threshold": rt.risk_threshold, "capacity": rt.capacity,
                              "effects": {k: dict(v) for k, v in rt.priors.effects.items()},
                              "cost_minor": dict(rt.priors.cost_minor), "sources": rt.sources,
                              "policy": _policy_dict(rt.policy)},
                "policy_platform": _policy_dict(TenantPolicyConfig()),
                "can_edit": p.can(Permission.POLICY_ADMIN)}

    @app.get("/v1/settings/{namespace}/history")
    def settings_history(namespace: str, c: Connection = Depends(tenant_conn),
                         p: Principal = Depends(require(Permission.READ))) -> dict[str, Any]:
        if namespace not in NAMESPACES:
            raise HTTPException(404, "unknown namespace")
        return {"items": settings_service.history(c, p.tenant_id, namespace)}

    @app.put("/v1/settings/{namespace}")
    def put_settings(namespace: str, body: SettingsIn, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                     s: Services = Depends(services)) -> dict[str, Any]:
        if namespace not in NAMESPACES:
            raise HTTPException(404, "unknown namespace")
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                v = settings_service.update(c, p.tenant_id, namespace, body.value, actor=_actor(p),
                                            reason=body.reason, now=datetime.now(UTC),
                                            expected_version=body.expected_version)
        except settings_service.SettingsConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)[:1000]) from exc
        return v.as_dict()

    # ------------------------------------------------------------ people and businesses (P8, ADR-0018)
    @app.get("/v1/me")
    def me(ident: Identity = Depends(current_identity)) -> dict[str, Any]:
        return {"sub": ident.sub, "email": ident.email, "name": ident.name, "businesses": list(ident.memberships)}

    @app.post("/v1/businesses")
    def create_business(body: BusinessIn, ident: Identity = Depends(current_identity),
                        s: Services = Depends(services)) -> dict[str, Any]:
        """A signed-in person starts a business on Nirantar and becomes its owner."""
        from nirantar.billing.service import create_tenant
        from nirantar.core.ids import new_id
        from nirantar.security.oidc import MAX_BUSINESSES_PER_USER, add_membership, count_owned

        if count_owned(s.engine, ident.sub) >= MAX_BUSINESSES_PER_USER:
            raise HTTPException(429, f"at most {MAX_BUSINESSES_PER_USER} businesses per person")
        tenant, now = new_id("ten"), datetime.now(UTC)
        with tenant_tx(tenant, s.engine) as c:
            create_tenant(c, tenant, body.name, {"segments": [body.segment], "onboarding": {"status": "started"}})
        add_membership(s.engine, ident.sub, tenant, ["owner"], invited_by=None)
        with tenant_tx(tenant, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(tenant, f"user:usr_{ident.sub}", "business.created",
                                                                 {"name": body.name, "segment": body.segment})
        return {"tenant_id": tenant, "name": body.name, "roles": ["owner"]}

    # ------------------------------------------------------------ onboarding and team (P8.2, ADR-0019)
    @app.get("/v1/onboarding")
    def onboarding_state(p: Principal = Depends(require(Permission.READ)),
                         s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.onboarding.service import checklist

        return checklist(s.engine, p.tenant_id)

    @app.post("/v1/onboarding/razorpay")
    def onboarding_razorpay(body: RazorpayKeysIn, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                            s: Services = Depends(services)) -> dict[str, Any]:
        """Connect the business's own Razorpay account. Keys are checked with Razorpay first, then stored encrypted;
        they are never returned. The webhook secret in the answer is shown once."""
        from nirantar.onboarding.service import OnboardingError, connect_razorpay

        try:
            out = connect_razorpay(s.engine, p.tenant_id, body.key_id, body.key_secret, _actor(p),
                                   verify=s.extra.get("razorpay_verify"))
        except OnboardingError as exc:
            raise HTTPException(422, str(exc)) from exc
        s.extra.pop("resolver", None)                      # drop any cached provider for this process
        with tenant_tx(p.tenant_id, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(datetime.now(UTC))).append(
                p.tenant_id, _actor(p), "provider.connected", {"provider": "razorpay", "mode": out["mode"]})
        return out

    @app.post("/v1/onboarding/import")
    async def onboarding_import(p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                                s: Services = Depends(services)) -> dict[str, Any]:
        """Start (or re-run) the history import: OnboardingWorkflow on the always-on worker."""
        import asyncio as _asyncio

        from temporalio.client import Client
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        from nirantar.onboarding.service import checklist, mark_import_started
        from nirantar.workflows import TASK_QUEUE
        from nirantar.workflows.platform import OnboardingInput, OnboardingWorkflow, onboarding_workflow_id

        state = await _asyncio.to_thread(checklist, s.engine, p.tenant_id)
        if state["steps"][1]["status"] != "done":
            raise HTTPException(409, "connect your payment provider first")
        client = s.extra.get("temporal")
        if client is None:
            client = s.extra["temporal"] = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
        handle = await client.start_workflow(
            OnboardingWorkflow.run, OnboardingInput(p.tenant_id), id=onboarding_workflow_id(p.tenant_id),
            task_queue=TASK_QUEUE, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        await _asyncio.to_thread(mark_import_started, s.engine, p.tenant_id)
        return {"workflow_id": handle.id, "status": "running"}

    @app.get("/v1/team")
    def team_list(p: Principal = Depends(require(Permission.READ)), s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.onboarding.service import team

        return team(s.engine, p.tenant_id)

    @app.post("/v1/team/invites")
    def team_invite(body: InviteIn, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                    s: Services = Depends(services)) -> dict[str, Any]:
        """Invite a person by email; they join when they sign in with that VERIFIED email."""
        from nirantar.onboarding.service import OnboardingError, invite

        try:
            out = invite(s.engine, p.tenant_id, body.email, body.role, _actor(p))
        except OnboardingError as exc:
            raise HTTPException(422, str(exc)) from exc
        with tenant_tx(p.tenant_id, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(datetime.now(UTC))).append(
                p.tenant_id, _actor(p), "team.invited", {"invite_id": out["invite_id"], "role": body.role})
        return out

    @app.delete("/v1/team/invites/{invite_id}")
    def team_revoke_invite(invite_id: str, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                           s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.onboarding.service import revoke_invite

        if not revoke_invite(s.engine, p.tenant_id, invite_id):
            raise HTTPException(404, "no open invite with this id")
        return {"invite_id": invite_id, "revoked": True}

    # ------------------------------------------------------------ WhatsApp webhook (P6, ADR-0017)
    @app.get("/webhooks/whatsapp")
    def whatsapp_verify(request: Request) -> Any:
        """Meta's subscription handshake: echo hub.challenge when the verify token matches."""
        from fastapi.responses import PlainTextResponse

        q = request.query_params
        expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        if q.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(q.get("hub.verify_token", ""),
                                                                                 expected):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request, s: Services = Depends(services)) -> dict[str, Any]:
        """Signed by Meta (X-Hub-Signature-256 over the raw body). Processing is idempotent on message ids, so a 5xx
        here simply makes Meta retry."""
        import asyncio
        import json as _json

        from nirantar.channels.inbound import process_whatsapp
        from nirantar.channels.whatsapp import verify_signature

        raw = await request.body()
        secret = os.environ.get("WHATSAPP_APP_SECRET", "")
        if not secret or not verify_signature(secret, raw, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        ex = approval_executor(s)
        sink = ex.comms
        res = await asyncio.to_thread(
            process_whatsapp, s.engine, _json.loads(raw), wa=getattr(sink, "whatsapp", None),
            speech=getattr(sink, "speech", None), provider_for_tenant=lambda t: ex.services(t)["provider"])
        return {"messages": res.messages, "statuses": res.statuses, "duplicates": res.duplicates,
                "unmatched": res.unmatched}

    # ------------------------------------------------------------ MCP connections (P7, ADR-0016)
    def oauth_store(s: Services) -> Any:
        from nirantar.mcp.oauth import OAuthStore

        return OAuthStore(s.engine, consent_url="")

    @app.get("/v1/mcp/requests/{request_id}")
    def mcp_request(request_id: str, _: Principal = Depends(require(Permission.READ)),
                    s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.mcp.oauth import ConsentError

        try:
            out: dict[str, Any] = oauth_store(s).describe_request(request_id)
        except ConsentError as exc:
            raise HTTPException(404, str(exc)) from exc
        return out

    @app.post("/v1/mcp/requests/{request_id}/decide")
    def mcp_decide(request_id: str, body: ConsentIn, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                   s: Services = Depends(services)) -> dict[str, Any]:
        """A tenant admin connects (or refuses) an MCP client. Returns where to send the browser back to."""
        from nirantar.mcp.oauth import ConsentError

        store = oauth_store(s)
        try:
            req = store.describe_request(request_id)
            url = store.decide(p, request_id, approve=body.approve, scopes=body.scopes)
        except ConsentError as exc:
            raise HTTPException(409, str(exc)) from exc
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(
                p.tenant_id, _actor(p), "mcp.connected" if body.approve else "mcp.refused",
                {"client_id": req["client_id"], "client_name": req["client_name"],
                 "scopes": [x for x in (body.scopes or req["scopes"]) if x in req["scopes"]] if body.approve else []})
        return {"redirect_url": url}

    @app.get("/v1/mcp/grants")
    def mcp_grants(p: Principal = Depends(require(Permission.READ)), s: Services = Depends(services)
                   ) -> dict[str, Any]:
        return {"items": oauth_store(s).list_grants(p.tenant_id)}

    @app.delete("/v1/mcp/grants/{grant_id}")
    def mcp_revoke(grant_id: str, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                   s: Services = Depends(services)) -> dict[str, Any]:
        if not oauth_store(s).revoke_grant(p.tenant_id, grant_id, f"revoked by {_actor(p)}"):
            raise HTTPException(404, "no active connection with this id")
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(p.tenant_id, _actor(p), "mcp.revoked",
                                                                 {"grant_id": grant_id})
        return {"grant_id": grant_id, "revoked": True}

    # ------------------------------------------------------------ A2A partners (P7, ADR-0016)
    @app.post("/v1/a2a/partners")
    def a2a_add_partner(body: PartnerIn, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        """Register an external agent (e.g. a customer's AI agent) that may call this tenant over A2A. The key is
        shown once."""
        from nirantar.security.keys import create_principal, issue_api_key

        pid = create_principal(s.engine, p.tenant_id, body.name, "service", ["a2a_partner"])
        key = issue_api_key(s.engine, p.tenant_id, pid)
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(p.tenant_id, _actor(p), "a2a.partner_added",
                                                                 {"principal_id": pid, "name": body.name})
        base = os.environ.get("NIRANTAR_PUBLIC_URL", "http://localhost:18080")
        return {"principal_id": pid, "key": key.plaintext, "endpoint": f"{base}/a2a",
                "agent_card": f"{base}/a2a/tenants/{p.tenant_id}/agent-card.json"}

    @app.get("/v1/a2a/partners")
    def a2a_partners(c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        rows = c.execute(text("SELECT p.principal_id, p.name, p.status, p.created_at, "
                              "max(k.last_used_at) AS last_used_at, (SELECT count(*) FROM ops.a2a_tasks t "
                              "WHERE t.counterparty='a2a_partner:' || "
                              "p.principal_id) AS tasks FROM core.principals p LEFT JOIN core.api_keys k ON "
                              "k.tenant_id=p.tenant_id AND k.principal_id=p.principal_id WHERE 'a2a_partner' = "
                              "ANY(p.roles) GROUP BY p.principal_id, p.name, p.status, p.created_at "
                              "ORDER BY p.created_at DESC")).all()
        return {"items": [dict(r._mapping) for r in rows]}

    @app.delete("/v1/a2a/partners/{principal_id}")
    def a2a_remove_partner(principal_id: str, p: Principal = Depends(require(Permission.TENANT_ADMIN)),
                           s: Services = Depends(services)) -> dict[str, Any]:
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            n = c.execute(text("UPDATE core.principals SET status='disabled' WHERE principal_id=:p AND "
                               "'a2a_partner' = ANY(roles) AND status='active'"), {"p": principal_id}).rowcount
            if n != 1:
                raise HTTPException(404, "no active A2A partner with this id")
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(p.tenant_id, _actor(p), "a2a.partner_removed",
                                                                 {"principal_id": principal_id})
        return {"principal_id": principal_id, "disabled": True}

    @app.get("/v1/events/dead-letters")
    def tenant_dead_letters(p: Principal = Depends(require(Permission.READ)),
                            c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        rows = c.execute(text("SELECT event_id, consumer, event_type, error, attempts, created_at, replayed_at FROM "
                              "events.consumer_dead_letters ORDER BY created_at DESC LIMIT 200")).all()
        return {"items": [dict(r._mapping) for r in rows]}

    @app.post("/v1/events/dead-letters/{event_id}/replay")
    def replay_dead_letter(event_id: str, body: ReplayIn, p: Principal = Depends(require(Permission.AGENTS_OPERATE)),
                           s: Services = Depends(services)) -> dict[str, Any]:
        """Re-deliver a dead-lettered event after its cause is fixed: the relay republishes the original outbox row
        and the consumers handle it again (idempotently). Audited."""
        now = datetime.now(UTC)
        with tenant_tx(p.tenant_id, s.engine) as c:
            dl = c.execute(text("SELECT consumer, event_type FROM events.consumer_dead_letters WHERE event_id=:e AND "
                                "replayed_at IS NULL"), {"e": event_id}).one_or_none()
            if dl is None:
                raise HTTPException(404, "no un-replayed dead letter with this event id")
            n = c.execute(text("UPDATE events.outbox SET published_at=NULL WHERE tenant_id=:t AND event_id=:e"),
                          {"t": p.tenant_id, "e": event_id}).rowcount
            if n != 1:
                raise HTTPException(409, "the original event is no longer in the outbox")
            c.execute(text("UPDATE events.consumer_dead_letters SET replayed_at=:n WHERE event_id=:e"),
                      {"n": now, "e": event_id})
            AuditChain(SqlAuditStore(c), FixedClock(now)).append(p.tenant_id, _actor(p), "event.replayed", {
                "event_id": event_id, "event_type": dl.event_type, "consumer": dl.consumer, "reason": body.reason})
        return {"event_id": event_id, "replayed_at": now}

    @app.get("/v1/learned")
    def get_learned(p: Principal = Depends(require(Permission.READ)),
                    c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("effects", "risk_threshold"):
            lv = learning.latest(c, p.tenant_id, name)
            out[name] = None if lv is None else {"version": lv.version, "value": lv.value, "evidence": lv.evidence,
                                                 "created_at": lv.created_at}
        return out

    @app.post("/v1/learned/refresh")
    def refresh_learned(p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        with tenant_tx(p.tenant_id, s.engine) as c:
            return learning.refresh(c, p.tenant_id, datetime.now(UTC))

    @app.get("/v1/templates")
    def list_templates(p: Principal = Depends(require(Permission.READ)),
                       c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        return {"items": templates.catalogue(c, p.tenant_id), "can_propose": p.can(Permission.POLICY_ADMIN),
                "can_review": p.can(Permission.APPROVALS_DECIDE), "me": _actor(p)}

    @app.post("/v1/templates")
    def propose_template(body: TemplateProposalIn, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                         s: Services = Depends(services)) -> dict[str, Any]:
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                return templates.propose(c, p.tenant_id, body.key, body.language, body.body, actor=_actor(p),
                                         now=datetime.now(UTC))
        except templates.TemplateRejected as exc:
            raise HTTPException(422, {"problems": exc.problems}) from exc

    @app.post("/v1/templates/{key}/{language}/{version}/decide")
    def decide_template(key: str, language: str, version: int, body: DecideIn,
                        p: Principal = Depends(require(Permission.APPROVALS_DECIDE)),
                        s: Services = Depends(services)) -> dict[str, Any]:
        try:
            with tenant_tx(p.tenant_id, s.engine) as c:
                return templates.decide(c, p.tenant_id, key, language, version, approver=_actor(p),
                                        approve=body.grant, now=datetime.now(UTC))
        except templates.TemplateReviewError as exc:
            raise HTTPException(409, str(exc)) from exc
        except templates.TemplateRejected as exc:
            raise HTTPException(422, {"problems": exc.problems}) from exc

    # ------------------------------------------------------------- data platform (ADR-0012)
    @app.get("/v1/data/health")
    def data_health(p: Principal = Depends(require(Permission.READ)),
                    c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        row = c.execute(text("SELECT report, computed_at, run_id FROM ai.data_health WHERE tenant_id=:t "
                             "ORDER BY computed_at DESC, run_id DESC LIMIT 1"), {"t": p.tenant_id}).one_or_none()
        return {"report": None if row is None else row.report, "run_id": None if row is None else row.run_id}

    @app.get("/v1/data/runs")
    def data_runs(limit: int = Query(20, ge=1, le=MAX_LIMIT), p: Principal = Depends(require(Permission.READ)),
                  c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        rows = c.execute(text("SELECT run_id, trigger, status, error, started_at, finished_at, "
                              "(SELECT coalesce(jsonb_agg(jsonb_build_object('step', s->>'step', 'ms', s->'ms')), "
                              "'[]'::jsonb) FROM jsonb_array_elements(steps) s) AS steps "
                              "FROM ingest.pipeline_runs WHERE tenant_id=:t ORDER BY started_at DESC LIMIT :l"),
                         {"t": p.tenant_id, "l": limit}).all()
        return {"items": [dict(r._mapping) for r in rows]}

    @app.post("/v1/data/sync", status_code=202)
    def data_sync(background: BackgroundTasks, p: Principal = Depends(require(Permission.INTEGRATIONS_ADMIN)),
                  s: Services = Depends(services)) -> dict[str, Any]:
        try:
            from nirantar.data.lake import Lake
            from nirantar.data.pipeline import run_tenant
        except ImportError as exc:     # the `data` extra is not installed in this deployment
            raise HTTPException(503, "data platform not installed (uv sync --extra data)") from exc
        run_id = new_id("run")

        def job() -> None:
            try:
                run_tenant(s.engine, s.extra.get("lake") or Lake.from_env(), p.tenant_id,
                           now=datetime.now(UTC), trigger="manual", run_id=run_id)
            except Exception:  # the pipeline marks the run failed with the reason; also log the traceback
                logging.getLogger("nirantar.data").exception("data sync %s failed for %s", run_id, p.tenant_id)

        background.add_task(job)
        return {"run_id": run_id, "status": "queued"}

    # ------------------------------------------------------------- per-tenant ML (ADR-0013)
    @app.get("/v1/ml/models")
    def tenant_models(p: Principal = Depends(require(Permission.READ)),
                      c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        versions = [dict(r._mapping) for r in c.execute(text(
            "SELECT version, stage, algorithm, feature_set, data_source, metrics, baseline, gates, training, "
            "canary_share, trained_at, stage_changed_at FROM ai.model_versions WHERE tenant_id=:t "
            "AND model_name='m1_debit_failure' ORDER BY trained_at DESC LIMIT 50"), {"t": p.tenant_id})]
        events = [dict(r._mapping) for r in c.execute(text(
            "SELECT version, from_stage, to_stage, reason, actor, at FROM ai.model_events WHERE tenant_id=:t "
            "ORDER BY at DESC LIMIT 50"), {"t": p.tenant_id})]
        monitoring = c.execute(text("SELECT report FROM ai.model_monitoring WHERE tenant_id=:t "
                                    "AND model_name='m1_debit_failure' ORDER BY computed_at DESC LIMIT 1"),
                               {"t": p.tenant_id}).scalar_one_or_none()
        health_row: Any = c.execute(text("SELECT report FROM ai.data_health WHERE tenant_id=:t "
                                    "ORDER BY computed_at DESC, run_id DESC LIMIT 1"), {"t": p.tenant_id}
                               ).scalar_one_or_none()
        served: dict[str, int] = dict(c.execute(text(
            "SELECT coalesce(output->>'served_by', 'unknown'), count(*) FROM ai.predictions WHERE tenant_id=:t "
            "AND model_name='m1_debit_failure' AND output->>'role'='decision' AND predicted_at > now() - "
            "interval '30 days' GROUP BY 1"), {"t": p.tenant_id}).all())
        return {"model": "m1_debit_failure", "versions": versions, "events": events, "monitoring": monitoring,
                "readiness": (health_row if isinstance(health_row, dict) else {}).get("readiness", {})
                .get("m1_debit_failure"),
                "served_30d": served, "can_manage": p.can(Permission.POLICY_ADMIN)}

    @app.post("/v1/ml/models/{version}/retire")
    def retire_model(version: str, body: RetireIn, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                     s: Services = Depends(services)) -> dict[str, Any]:
        from nirantar.ml.rollout import manual_rollback

        try:
            return manual_rollback(s.engine, p.tenant_id, version, reason=body.reason, actor=_actor(p),
                                   now=datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/ml/train", status_code=202)
    def train_now(background: BackgroundTasks, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                  s: Services = Depends(services)) -> dict[str, Any]:
        """Refresh features and train now (readiness still applies: no model on too little data)."""
        try:
            from nirantar.data.lake import Lake
            from nirantar.features.offline import build_training_set
            from nirantar.features.online import OnlineStore
            from nirantar.ml.tenant_training import _data_source, train_m1
        except ImportError as exc:
            raise HTTPException(503, "ML platform not installed (uv sync --extra data --extra ml)") from exc

        def job() -> None:
            now = datetime.now(UTC)
            try:
                lake = s.extra.get("lake") or Lake.from_env()
                build_training_set(lake, p.tenant_id, now=now, source=_data_source(s.engine, p.tenant_id))
                OnlineStore().materialize(lake, p.tenant_id, now=now)
                train_m1(s.engine, lake, p.tenant_id, now=now, actor=_actor(p))
            except Exception:  # result/failure is visible in model versions/events; keep the traceback in logs
                logging.getLogger("nirantar.ml").exception("manual training failed for %s", p.tenant_id)

        background.add_task(job)
        return {"status": "queued"}

    # ------------------------------------------------------------- retention & win-back (ADR-0014)
    @app.get("/v1/retention/overview")
    def retention_overview(p: Principal = Depends(require(Permission.READ)),
                           c: Connection = Depends(tenant_conn)) -> dict[str, Any]:
        def one(sql: str) -> Any:
            return c.execute(text(sql), {"t": p.tenant_id}).one_or_none()

        fit = one("SELECT fit_id, fitted_at, sbg, type_rule, report FROM ai.retention_fits WHERE tenant_id=:t "
                  "ORDER BY fitted_at DESC LIMIT 1")
        risk = one("SELECT computed_at, summary, items FROM ai.at_risk_snapshots WHERE tenant_id=:t "
                   "ORDER BY computed_at DESC LIMIT 1")
        health_row = one("SELECT report FROM ai.data_health WHERE tenant_id=:t ORDER BY computed_at DESC, run_id DESC "
                         "LIMIT 1")
        models = [dict(r._mapping) for r in c.execute(text(
            "SELECT model_name, version, stage, algorithm, metrics, baseline, gates, trained_at FROM ai.model_versions "
            "WHERE tenant_id=:t AND model_name IN ('m6_churn','m13_churn_type') ORDER BY trained_at DESC LIMIT 20"),
            {"t": p.tenant_id})]
        exps = c.execute(text("SELECT experiment_id, name, holdout_bp, created_at FROM experiments.experiments WHERE "
                              "tenant_id=:t AND name LIKE 'winback-%' ORDER BY created_at DESC"),
                         {"t": p.tenant_id}).all()
        winback = []
        for e in exps:
            a = analyze(c, p.tenant_id, e.experiment_id, success_outcome="reactivated")
            winback.append({"experiment_id": e.experiment_id, "stratum": e.name.split("-")[1],
                            "holdout_bp": e.holdout_bp, "created_at": e.created_at, **a})
        cases: dict[str, int] = dict(c.execute(text(
            "SELECT status, count(*) FROM ops.cases WHERE tenant_id=:t AND kind='revival' GROUP BY 1"),
            {"t": p.tenant_id}).all())
        offers: dict[str, int] = dict(c.execute(text(
            "SELECT status, count(*) FROM billing.offers WHERE tenant_id=:t GROUP BY 1"), {"t": p.tenant_id}).all())
        report = health_row.report if health_row else {}
        return {
            "subscriptions": report.get("subscriptions"),
            "fit": None if fit is None else {"fitted_at": fit.fitted_at, "sbg": fit.sbg, "type_rule": fit.type_rule,
                                             "report": fit.report},
            "at_risk": None if risk is None else {"computed_at": risk.computed_at, "summary": risk.summary,
                                                  "items": list(risk.items)[:100]},
            "models": models, "winback": winback, "cases": cases, "offers": offers,
            "can_manage": p.can(Permission.POLICY_ADMIN),
        }

    @app.post("/v1/retention/refresh", status_code=202)
    def retention_refresh(background: BackgroundTasks, p: Principal = Depends(require(Permission.POLICY_ADMIN)),
                          s: Services = Depends(services)) -> dict[str, Any]:
        """Rebuild lifecycle labels, refit sBG/CLV and re-score active subscribers now."""
        try:
            from nirantar.data.lake import Lake
            from nirantar.data.lifecycle import build_lifecycle
            from nirantar.features.offline import build_training_set
            from nirantar.features.online import OnlineStore
            from nirantar.ml.router import ModelRouter
            from nirantar.ml.tenant_training import _data_source
            from nirantar.retention.fit import fit_retention
            from nirantar.retention.launcher import store_at_risk
            from nirantar.retention.scoring import label_predictions, score_active
        except ImportError as exc:
            raise HTTPException(503, "ML platform not installed (uv sync --extra data --extra ml)") from exc

        def job() -> None:
            now = datetime.now(UTC)
            try:
                lake = s.extra.get("lake") or Lake.from_env()
                build_lifecycle(lake, p.tenant_id, now=now)
                build_training_set(lake, p.tenant_id, now=now, source=_data_source(s.engine, p.tenant_id))
                fit_retention(s.engine, lake, p.tenant_id, now=now)
                store = OnlineStore()
                store.materialize(lake, p.tenant_id, now=now)
                label_predictions(s.engine, lake, p.tenant_id, now=now)
                store_at_risk(s.engine, p.tenant_id, score_active(s.engine, lake, p.tenant_id, now=now, store=store,
                                                                  router=ModelRouter()), now)
            except Exception:  # visible as a stale snapshot; traceback in logs
                logging.getLogger("nirantar.retention").exception("retention refresh failed for %s", p.tenant_id)

        background.add_task(job)
        return {"status": "queued"}

    # ------------------------------------------------------------- webhooks (provider → ingress)
    @app.post("/webhooks/{provider_name}/{tenant_id}")
    async def webhook(provider_name: str, tenant_id: str, request: Request,
                      s: Services = Depends(services)) -> JSONResponse:
        raw = await request.body()
        provider = s.extra.get(f"provider:{provider_name}")
        if provider is None:      # the tenant's OWN connected account (never a process-wide key)
            from nirantar.payments.providers.resolver import ProviderNotConfigured, ProviderResolver

            resolver: ProviderResolver = s.extra.setdefault("resolver", ProviderResolver(s.engine))
            try:
                provider = resolver.for_tenant(tenant_id, provider_name)
            except (ProviderNotConfigured, ValueError):
                return JSONResponse({"ok": False, "error": "provider not connected"}, status_code=404)
        res = ingest_webhook(s.engine, provider, tenant_id, dict(request.headers), raw)
        return JSONResponse({"ok": res.status_code == 200, "duplicate": res.duplicate}, status_code=res.status_code)

    # ------------------------------------------------------------- platform console (cross-tenant health only)
    def platform_engine(s: Services) -> Any:
        return s.owner_engine or create_engine(os.environ.get(
            "DATABASE_OWNER_URL", "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"))

    @app.get("/platform/health", dependencies=[Depends(platform_admin)])
    def platform_health(s: Services = Depends(services)) -> dict[str, Any]:
        eng = platform_engine(s)
        with eng.connect() as c:
            def one(q: str) -> Any:
                return c.execute(text(q)).scalar_one()

            return {
                "tenants": one("SELECT count(*) FROM core.tenants"),
                "outbox_unpublished": one("SELECT count(*) FROM events.outbox WHERE published_at IS NULL"),
                "outbox_oldest_unpublished_s": one(
                    "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM events.outbox "
                    "WHERE published_at IS NULL"),
                "provider_events": dict(c.execute(text(
                    "SELECT status, count(*) FROM ingest.provider_events GROUP BY status")).all()),
                "dead_letter_events": one("SELECT count(*) FROM ingest.provider_events WHERE status='dead'"),
                "pending_approvals": one("SELECT count(*) FROM ops.approvals WHERE status='pending'"),
                "failed_actions_24h": one("SELECT count(*) FROM ops.actions WHERE status='failed' "
                                          "AND created_at > now() - interval '24 hours'"),
                "open_discrepancies": one("SELECT count(*) FROM billing.discrepancies WHERE resolved_at IS NULL"),
            }

    @app.get("/platform/tenants", dependencies=[Depends(platform_admin)])
    def platform_tenants(s: Services = Depends(services)) -> dict[str, Any]:
        with platform_engine(s).connect() as c:
            rows = c.execute(text(
                "SELECT t.tenant_id, t.name, t.status, t.created_at, "
                "(SELECT count(*) FROM billing.customers x WHERE x.tenant_id=t.tenant_id) AS customers, "
                "(SELECT count(*) FROM billing.debits x WHERE x.tenant_id=t.tenant_id) AS debits, "
                "(SELECT count(*) FROM ops.actions x WHERE x.tenant_id=t.tenant_id) AS actions "
                "FROM core.tenants t ORDER BY t.created_at DESC LIMIT 200")).all()
        return {"items": [dict(r._mapping) for r in rows]}

    @app.get("/platform/dead-letters", dependencies=[Depends(platform_admin)])
    def dead_letters(s: Services = Depends(services)) -> dict[str, Any]:
        with platform_engine(s).connect() as c:
            rows = c.execute(text("SELECT tenant_id, raw_event_id, provider, event_type, attempts, last_error, "
                                  "received_at FROM ingest.provider_events WHERE status IN ('dead','failed') "
                                  "ORDER BY received_at DESC LIMIT 100")).all()
        return {"items": [dict(r._mapping) for r in rows]}

    # ------------------------------------------------------------ A2A v1.0 server (official SDK)
    from nirantar.a2a.server import PartnerAuth, build_routes

    svc_ = app.state.services
    app.router.routes.extend(build_routes(svc_.engine, base_url=os.environ.get("NIRANTAR_PUBLIC_URL",
                                                                               "http://localhost:18080"),
                                          services=approval_executor(svc_).services,
                                          environment=os.environ.get("NIRANTAR_ENV", "local")))
    app.add_middleware(PartnerAuth, engine=svc_.engine)
    return app

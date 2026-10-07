"""Recovery batches: an operator picks items from the queue, previews every message and its compliance decision,
and launches a bounded run with its own randomised holdout. The proof is verified money, treatment vs holdout.

Bounds (the brief's "stopping rules"):
  - at most MAX_ITEMS per batch; an item can be in only one running batch
  - every action goes through the MCP ToolGateway (scope → schema → policy → approval → execute → audit) as the
    `recovery_batch` agent — the same tools the agents have, never more
  - consent, contact windows, fatigue caps and STOP are enforced by policy at execution time, not only at preview
  - a stop button: remaining items are skipped; items already sent stay sent (and are measured)
  - the batch measures for `window_days`, then closes; outcomes count only when the provider confirmed the payment
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from nirantar.agents.conversation import format_amount
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.experiments import service as experiments
from nirantar.mcp.gateway import ToolGateway
from nirantar.recovery import queue
from nirantar.settings import templates

AGENT = "recovery_batch"
MAX_ITEMS = 500
LINK_PLACEHOLDER = "{link}"


class BatchError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, Any]


def _first(name: str | None) -> str:
    return (name or "").split(" ")[0] or "there"


def _recovery_text(conn: Connection, tenant_id: str, item: dict[str, Any]) -> tuple[str, str]:
    tpl = templates.resolve(conn, tenant_id, "whatsapp.recovery", item["language"] or "en")
    body = tpl.render(name=_first(item["display_name"]), amount=format_amount(Money(item["amount_minor"], "INR")),
                      plan="subscription", link=LINK_PLACEHOLDER)
    return body, tpl.ref


def _steps(item: dict[str, Any], text_: str | None) -> list[Step]:
    if item["action"] == "payment_link_whatsapp":
        return [Step("gateway.create_payment_link", {"debit_id": item["subject_id"],
                                                     "attempt": min(10, item.get("attempts", 0) + 1)}),
                Step("comms.send_whatsapp", {"customer_id": item["customer_id"], "debit_id": item["subject_id"],
                                             "text": text_ or "", "purpose": "recovery"})]
    if item["action"] == "predebit_notice":
        return [Step("comms.send_predebit_notice", {"debit_id": item["subject_id"]})]
    if item["action"] == "mandate_repair":
        return [Step("mandate.send_repair", {"case_id": item["subject_id"]})]
    raise BatchError(f"no batch action for {item['item_id']}")


def _pick(conn: Connection, item_ids: list[str], now: datetime) -> list[dict[str, Any]]:
    if not item_ids:
        raise BatchError("select at least one item")
    if len(item_ids) > MAX_ITEMS:
        raise BatchError(f"a batch holds at most {MAX_ITEMS} items")
    live = {x["item_id"]: x for x in queue.build(conn, now)["items"]}
    missing = [i for i in item_ids if i not in live]
    if missing:
        raise BatchError(f"no longer in the queue (paid, closed or already in a batch): {missing[:5]}")
    picked = [live[i] for i in dict.fromkeys(item_ids)]
    no_action = [x["item_id"] for x in picked if not x["action"]]
    if no_action:
        raise BatchError(f"nothing to do for: {no_action[:5]}")
    return picked


def plan(engine: Engine, gateway: ToolGateway, tenant_id: str, item_ids: list[str], now: datetime) -> dict[str, Any]:
    """Dry run, no writes: the exact message each customer would get and what policy would decide right now."""
    with tenant_tx(tenant_id, engine) as c:
        picked = _pick(c, item_ids, now)
        texts = {x["item_id"]: _recovery_text(c, tenant_id, x) for x in picked
                 if x["action"] == "payment_link_whatsapp"}
    out, counts = [], {"ALLOW": 0, "REQUIRE_APPROVAL": 0, "DENY": 0, "other": 0}
    for x in picked:
        body, ref = texts.get(x["item_id"], (None, None))
        decisions = []
        for st in _steps(x, body):
            if st.tool == "gateway.create_payment_link":
                continue                      # no customer effect until the message goes out; amount from the debit
            decisions.append({"tool": st.tool, **gateway.preview(tenant_id=tenant_id, agent_id=AGENT,
                                                                 tool_name=st.tool, args=st.args)})
        worst = max((d["outcome"] for d in decisions),
                    key=lambda o: {"ALLOW": 0, "REQUIRE_MORE_INFORMATION": 1, "REQUIRE_APPROVAL": 2}.get(o, 3))
        counts[worst if worst in counts else "other"] += 1
        out.append({"item_id": x["item_id"], "kind": x["kind"], "display_name": x["display_name"],
                    "amount_minor": x["amount_minor"], "action": x["action"], "message": body,
                    "template_ref": ref, "decision": worst, "checks": decisions})
    return {"items": out, "counts": counts, "previewed_at": now.isoformat()}


def launch(engine: Engine, tenant_id: str, item_ids: list[str], *, name: str, holdout_bp: int, window_days: int,
           actor: str, now: datetime) -> dict[str, Any]:
    if not 0 <= holdout_bp <= 5000:
        raise BatchError("holdout must be between 0% and 50%")
    if not 1 <= window_days <= 30:
        raise BatchError("measurement window must be 1-30 days")
    batch_id = new_id("rb")
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"recovery-batch:{tenant_id}"})
        picked = _pick(c, item_ids, now)
        try:
            exp = experiments.create_experiment(c, tenant_id, f"Recovery batch: {name}"[:200],
                                                {"treatment": 10_000 - holdout_bp}, holdout_bp=holdout_bp)
        except experiments.ExperimentPolicyError as exc:
            raise BatchError(str(exc)) from exc
        c.execute(text("INSERT INTO ops.recovery_batches (tenant_id, batch_id, name, experiment_id, holdout_bp, "
                       "window_days, status, launched_by, launched_at, ends_at, summary) VALUES (:t, :b, :n, :e, :h, "
                       ":w, 'running', :by, :now, :end, CAST(:s AS jsonb))"),
                  {"t": tenant_id, "b": batch_id, "n": name[:200], "e": exp, "h": holdout_bp, "w": window_days,
                   "by": actor, "now": now, "end": now + timedelta(days=window_days),
                   "s": json.dumps({"items": len(picked)})})
        arms: dict[str, int] = {"treatment": 0, "holdout": 0}
        for x in picked:
            arm = experiments.assign(c, tenant_id, exp, x["customer_id"], now)
            arm = "holdout" if arm == experiments.HOLDOUT else "treatment"
            arms[arm] += 1
            c.execute(text("INSERT INTO ops.recovery_batch_items (tenant_id, batch_id, item_id, kind, subject_id, "
                           "customer_id, amount_minor, arm, action, state, detail) VALUES (:t, :b, :i, :k, :s, :c, "
                           ":a, :arm, :act, :st, CAST(:d AS jsonb))"),
                      {"t": tenant_id, "b": batch_id, "i": x["item_id"], "k": x["kind"], "s": x["subject_id"],
                       "c": x["customer_id"], "a": x["amount_minor"], "arm": arm, "act": x["action"],
                       "st": "held_out" if arm == "holdout" else "planned",
                       "d": json.dumps({"display_name": x["display_name"], "language": x["language"],
                                        "attempts": x.get("attempts", 0), "state_at_launch": x["state"],
                                        "expected_minor": x["expected_minor"]})})
    return {"batch_id": batch_id, "experiment_id": exp, "arms": arms, "ends_at": (now + timedelta(days=window_days))
            .isoformat()}


def treatment_items(engine: Engine, tenant_id: str, batch_id: str) -> list[str]:
    with tenant_tx(tenant_id, engine) as c:
        return [r[0] for r in c.execute(text(
            "SELECT item_id FROM ops.recovery_batch_items WHERE tenant_id=:t AND batch_id=:b AND arm='treatment' "
            "AND state IN ('planned','denied') ORDER BY amount_minor DESC"), {"t": tenant_id, "b": batch_id})]


def _batch_status(c: Connection, tenant_id: str, batch_id: str) -> str:
    return str(c.execute(text("SELECT status FROM ops.recovery_batches WHERE tenant_id=:t AND batch_id=:b"),
                         {"t": tenant_id, "b": batch_id}).scalar_one())


def execute_item(engine: Engine, gateway: ToolGateway, tenant_id: str, batch_id: str, item_id: str,
                 now: datetime) -> dict[str, Any]:
    """Run one treatment item through the gateway. Idempotent per (batch, item, step): a retried activity never
    sends twice. Returns the state and, for a contact-window denial, when to try again."""
    with tenant_tx(tenant_id, engine) as c:
        if _batch_status(c, tenant_id, batch_id) != "running":
            c.execute(text("UPDATE ops.recovery_batch_items SET state='stopped', updated_at=:n WHERE tenant_id=:t "
                           "AND batch_id=:b AND item_id=:i AND state IN ('planned','denied')"),
                      {"n": now, "t": tenant_id, "b": batch_id, "i": item_id})
            return {"state": "stopped"}
        it = c.execute(text("SELECT * FROM ops.recovery_batch_items WHERE tenant_id=:t AND batch_id=:b AND "
                            "item_id=:i"), {"t": tenant_id, "b": batch_id, "i": item_id}).one()
        exp: str = c.execute(text("SELECT experiment_id FROM ops.recovery_batches WHERE tenant_id=:t AND batch_id=:b"),
                        {"t": tenant_id, "b": batch_id}).scalar_one()
        paid = it.kind != "mandate" and c.execute(text(
            "SELECT status FROM billing.debits WHERE tenant_id=:t AND debit_id=:d"),
            {"t": tenant_id, "d": it.subject_id}).scalar_one() == "succeeded"
        detail = dict(it.detail or {})
        item = {"item_id": it.item_id, "kind": it.kind, "subject_id": it.subject_id, "customer_id": it.customer_id,
                "amount_minor": int(it.amount_minor), "action": it.action, "display_name": detail.get("display_name"),
                "language": detail.get("language"), "attempts": int(detail.get("attempts", 0))}
        body = _recovery_text(c, tenant_id, item)[0] if it.action == "payment_link_whatsapp" else None
    if paid:
        return _record(engine, tenant_id, batch_id, item_id, "skipped", [], {"why": "paid before we contacted"}, now)
    action_ids: list[str] = list(it.action_ids or [])
    link_url = ""
    for st in _steps(item, body):
        args = dict(st.args)
        if st.tool == "comms.send_whatsapp":
            args["text"] = args["text"].replace(LINK_PLACEHOLDER, link_url)
        r = gateway.call(tenant_id=tenant_id, agent_id=AGENT, tool_name=st.tool, args=args,
                         idempotency_key=f"rb:{batch_id}:{item_id}:{st.tool}")
        if r.action_id and r.action_id not in action_ids:
            action_ids.append(r.action_id)
        if r.status != "executed":
            messages = [h.message for h in r.decision.hits] if r.decision else []
            retry = r.decision.retry_after.isoformat() if r.decision and r.decision.retry_after else None
            state = {"pending_approval": "pending_approval", "denied": "denied"}.get(r.status, "failed")
            return _record(engine, tenant_id, batch_id, item_id, state, action_ids,
                           {"tool": st.tool, "messages": messages, "error": r.error or r.output.get("error"),
                            "retry_after": retry}, now)
        if st.tool == "gateway.create_payment_link":
            link_url = str(r.output["url"])
    with tenant_tx(tenant_id, engine) as c:
        experiments.log_exposure(c, tenant_id, exp, it.customer_id, "treatment", action_ids[-1], now)
    return _record(engine, tenant_id, batch_id, item_id, "executed", action_ids, {}, now)


def _record(engine: Engine, tenant_id: str, batch_id: str, item_id: str, state: str, action_ids: list[str],
            info: dict[str, Any], now: datetime) -> dict[str, Any]:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("UPDATE ops.recovery_batch_items SET state=:s, action_ids=:a, detail = detail || "
                       "CAST(:d AS jsonb), updated_at=:n WHERE tenant_id=:t AND batch_id=:b AND item_id=:i"),
                  {"s": state, "a": action_ids, "d": json.dumps({"last": info}), "n": now, "t": tenant_id,
                   "b": batch_id, "i": item_id})
    return {"state": state, **info}


def stop(engine: Engine, tenant_id: str, batch_id: str, actor: str, now: datetime) -> dict[str, Any]:
    with tenant_tx(tenant_id, engine) as c:
        n = c.execute(text("UPDATE ops.recovery_batches SET status='stopping', stopped_by=:by, stopped_at=:n "
                           "WHERE tenant_id=:t AND batch_id=:b AND status='running'"),
                      {"by": actor, "n": now, "t": tenant_id, "b": batch_id}).rowcount
        if not n:
            raise BatchError("only a running batch can be stopped")
        skipped = c.execute(text("UPDATE ops.recovery_batch_items SET state='stopped', updated_at=:n WHERE "
                                 "tenant_id=:t AND batch_id=:b AND state IN ('planned','denied')"),
                            {"n": now, "t": tenant_id, "b": batch_id}).rowcount
    return {"batch_id": batch_id, "status": "stopping", "skipped": skipped}


# ---------------------------------------------------------------- outcomes and proof
def _outcomes(c: Connection, batch_id: str, launched_at: datetime) -> dict[str, tuple[bool, int]]:
    """item_id → (recovered, ₹). Debits: the provider captured a payment for it after launch (webhook = provider
    truth; the debit is marked paid only after verification). Mandates: the repair case closed as repaired."""
    out: dict[str, tuple[bool, int]] = {}
    for r in c.execute(text(
            "SELECT i.item_id, i.kind, i.amount_minor, "
            "CASE WHEN i.kind='mandate' THEN (SELECT k.summary->>'outcome' = 'repaired' FROM ops.cases k WHERE "
            "  k.tenant_id=i.tenant_id AND k.case_id=i.subject_id) "
            "ELSE EXISTS (SELECT 1 FROM billing.debits d JOIN billing.payments p ON p.tenant_id=d.tenant_id AND "
            "  p.debit_id=d.debit_id WHERE d.tenant_id=i.tenant_id AND d.debit_id=i.subject_id AND "
            "  d.status='succeeded' AND p.status='captured' AND coalesce(p.provider_created_at, p.created_at) >= :l) "
            "END AS won FROM ops.recovery_batch_items i WHERE i.batch_id=:b"), {"l": launched_at, "b": batch_id}):
        won = bool(r.won)
        out[r.item_id] = (won, int(r.amount_minor) if won and r.kind != "mandate" else 0)
    return out


def proof(conn: Connection, tenant_id: str, batch_id: str) -> dict[str, Any]:
    b = conn.execute(text("SELECT * FROM ops.recovery_batches WHERE tenant_id=:t AND batch_id=:b"),
                     {"t": tenant_id, "b": batch_id}).one_or_none()
    if b is None:
        raise BatchError("no such batch")
    won = _outcomes(conn, batch_id, b.launched_at)
    items = conn.execute(text("SELECT item_id, kind, subject_id, customer_id, amount_minor, arm, action, state, "
                              "detail, action_ids, updated_at FROM ops.recovery_batch_items WHERE tenant_id=:t AND "
                              "batch_id=:b ORDER BY arm DESC, amount_minor DESC"),
                         {"t": tenant_id, "b": batch_id}).all()
    units: dict[str, dict[str, tuple[bool, int]]] = {"treatment": {}, "holdout": {}}
    states: dict[str, int] = {}
    rows = []
    for i in items:
        ok, value = won.get(i.item_id, (False, 0))
        prev = units[i.arm].get(i.customer_id, (False, 0))
        units[i.arm][i.customer_id] = (prev[0] or ok, prev[1] + value)     # unit = customer, as in experiments
        states[i.state] = states.get(i.state, 0) + 1
        d = dict(i.detail or {})
        rows.append({"item_id": i.item_id, "kind": i.kind, "subject_id": i.subject_id, "customer_id": i.customer_id,
                     "display_name": d.get("display_name"), "amount_minor": int(i.amount_minor), "arm": i.arm,
                     "action": i.action, "state": i.state, "last": d.get("last"), "action_ids": list(i.action_ids),
                     "recovered": ok, "value_minor": value, "updated_at": i.updated_at.isoformat()})
    stats = experiments.incremental({arm: list(u.values()) for arm, u in units.items() if u}, min_per_arm=10)
    ids = [a for r in rows for a in r["action_ids"]]
    actions = [dict(a) for a in conn.execute(text(
        "SELECT action_id, tool_name, status, policy_decision, policy_version, params_hash, created_at "
        "FROM ops.actions "
        "WHERE action_id = ANY(:a) ORDER BY created_at"), {"a": ids}).mappings()] if ids else []
    for a in actions:
        a["created_at"] = a["created_at"].isoformat()
    return {
        "batch": {"batch_id": b.batch_id, "name": b.name, "status": b.status, "experiment_id": b.experiment_id,
                  "holdout_pct": b.holdout_bp / 100, "window_days": b.window_days, "launched_by": b.launched_by,
                  "launched_at": b.launched_at.isoformat(), "ends_at": b.ends_at.isoformat(),
                  "stopped_by": b.stopped_by, "stopped_at": b.stopped_at.isoformat() if b.stopped_at else None},
        "states": states,
        "recovered": {arm: {"customers": len(u), "recovered": sum(ok for ok, _ in u.values()),
                            "value_minor": sum(v for _, v in u.values())} for arm, u in units.items()},
        "statistics": stats, "actions": actions, "items": rows}


def measure_and_close(engine: Engine, tenant_id: str, batch_id: str, now: datetime) -> dict[str, Any]:
    """End of the window (or after a stop): write each unit's verified outcome to the batch's experiment, so the
    platform-wide experiment analysis and the Overview see it too, and close the batch."""
    with tenant_tx(tenant_id, engine) as c:
        report = proof(c, tenant_id, batch_id)
        b = report["batch"]
        if b["status"] in ("completed", "stopped"):
            return report
        for r in report["items"]:
            c.execute(text("UPDATE ops.recovery_batch_items SET outcome=:o, value_minor=:v WHERE tenant_id=:t AND "
                           "batch_id=:b AND item_id=:i"),
                      {"o": "recovered" if r["recovered"] else "not_recovered", "v": r["value_minor"], "t": tenant_id,
                       "b": batch_id, "i": r["item_id"]})
            if r["recovered"]:
                experiments.record_outcome(c, tenant_id, b["experiment_id"], r["customer_id"],
                                           None if r["kind"] == "mandate" else r["subject_id"], "recovered",
                                           r["value_minor"], True, now)
        final = "stopped" if b["status"] == "stopping" else "completed"
        c.execute(text("UPDATE ops.recovery_batches SET status=:s, summary = summary || CAST(:sum AS jsonb) "
                       "WHERE tenant_id=:t AND batch_id=:b"),
                  {"s": final, "sum": json.dumps({"recovered": report["recovered"], "states": report["states"],
                                                  "closed_at": now.isoformat()}), "t": tenant_id, "b": batch_id})
        report["batch"]["status"] = final
    return report


def batches(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT b.batch_id, b.name, b.status, b.holdout_bp, b.window_days, b.launched_by, b.launched_at, b.ends_at, "
        "count(i.*) AS items, count(i.*) FILTER (WHERE i.state='executed') AS executed, "
        "coalesce(sum(i.amount_minor), 0) AS amount_minor FROM ops.recovery_batches b LEFT JOIN "
        "ops.recovery_batch_items i ON i.tenant_id=b.tenant_id AND i.batch_id=b.batch_id "
        "GROUP BY b.tenant_id, b.batch_id ORDER BY b.launched_at DESC LIMIT :l"), {"l": limit}).mappings().all()
    return [{**r, "launched_at": r["launched_at"].isoformat(), "ends_at": r["ends_at"].isoformat(),
             "holdout_pct": r["holdout_bp"] / 100} for r in rows]

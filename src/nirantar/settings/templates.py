"""Template registry: customer-facing wording per tenant, key and language (P1, ADR-0011).

Lifecycle: propose (pending) → a DIFFERENT person approves or rejects → the approved version replaces the previous
one (retired). Versions are immutable (DB trigger); at most one approved version per key+language (unique index);
maker ≠ checker is enforced by a table CHECK as well as here.

Every proposal must pass automated checks before it can even be stored:
  - placeholders: all required ones present, no unknown ones (facts are injected by code, never typed in)
  - no literal money amounts (₹/Rs/INR + digits) — the amount always comes from {amount}
  - conduct rules (Compliance Guardian's conduct screen: threats, pressure, third parties…)
  - script: Hindi/Telugu templates must actually be written in Devanagari/Telugu script
Resolution at runtime: tenant-approved version for the language → platform default for the language →
the same two for English. The caller records which version it used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.agents.conversation import in_expected_script
from nirantar.audit.chain import AuditChain
from nirantar.contracts.events import make_event
from nirantar.core.clock import FixedClock
from nirantar.core.errors import NirantarError
from nirantar.db.stores import Outbox, SqlAuditStore
from nirantar.policy.engine import conduct_violation_labels
from nirantar.settings.schema import DEFAULTS_DIR

LANGUAGES = ("en", "hi", "te")
_FIELD = re.compile(r"\{([A-Za-z_]*)\}")
_LITERAL_AMOUNT = re.compile(r"₹\s*\d|(?<![A-Za-z0-9])(?:rs\.?|inr)\s*\d", re.IGNORECASE)


class TemplateRejected(NirantarError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class TemplateReviewError(NirantarError):
    pass


@dataclass(frozen=True)
class Resolved:
    key: str
    language: str
    body: str
    version: int            # 0 = platform default
    source: str             # tenant | platform_default

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.language}:v{self.version}"

    def render(self, **values: str) -> str:
        return render(self.body, values)


@cache
def _defaults() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DEFAULTS_DIR / "templates.yaml").read_text(encoding="utf-8"))
    return data


def specs() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = _defaults()["specs"]
    return out


def platform_default(key: str, language: str) -> str | None:
    body: str | None = _defaults()["templates"].get(key, {}).get(language)
    return body


def render(body: str, values: dict[str, str]) -> str:
    """Substitute only declared placeholders; anything else in braces is left as-is (never evaluated)."""
    return _FIELD.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), body)


def check(key: str, language: str, body: str) -> dict[str, Any]:
    spec = specs().get(key)
    if spec is None:
        raise TemplateRejected([f"unknown template key {key!r}"])
    problems: list[str] = []
    if language not in LANGUAGES:
        problems.append(f"unsupported language {language!r}")
    if not 10 <= len(body) <= 1000:
        problems.append("body must be 10-1000 characters")
    fields = _FIELD.findall(body)
    missing = [f for f in spec["required"] if f not in fields]
    unknown = sorted({f for f in fields if f not in spec["allowed"]})
    if missing:
        problems.append(f"missing required placeholders: {missing}")
    if unknown:
        problems.append(f"unknown placeholders: {unknown}")
    if _LITERAL_AMOUNT.search(body):
        problems.append("literal money amount: use {amount}; amounts always come from the debit record")
    conduct = conduct_violation_labels(render(body, {f: "x" for f in spec["allowed"]}))
    if conduct:
        problems.append(f"not allowed in customer messages: {', '.join(conduct)}")
    stripped = _FIELD.sub("", body)
    if not in_expected_script(stripped, language):
        problems.append(f"text is not mainly in the {language} script")
    result = {"missing": missing, "unknown": unknown, "conduct": conduct, "passed": not problems}
    if problems:
        raise TemplateRejected(problems)
    return result


def resolve(conn: Connection, tenant_id: str, key: str, language: str) -> Resolved:
    for lang in dict.fromkeys((language, "en")):
        row = conn.execute(text("SELECT version, body FROM config.templates WHERE tenant_id=:t AND template_key=:k "
                                "AND language=:l AND status='approved'"),
                           {"t": tenant_id, "k": key, "l": lang}).one_or_none()
        if row is not None:
            return Resolved(key, lang, row.body, row.version, "tenant")
        body = platform_default(key, lang)
        if body is not None:
            return Resolved(key, lang, body, 0, "platform_default")
    raise KeyError(f"no template for {key}")


def propose(conn: Connection, tenant_id: str, key: str, language: str, body: str, *, actor: str,
            now: datetime) -> dict[str, Any]:
    checks = check(key, language, body)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"tpl:{tenant_id}:{key}:{language}"})
    live = resolve(conn, tenant_id, key, language)
    if live.language == language and live.body.strip() == body.strip():
        raise TemplateRejected(["identical to the live version: nothing to review"])
    if conn.execute(text("SELECT 1 FROM config.templates WHERE tenant_id=:t AND template_key=:k AND language=:l "
                         "AND status='pending' AND body=:b"),
                    {"t": tenant_id, "k": key, "l": language, "b": body}).first():
        raise TemplateRejected(["the same wording is already waiting for review"])
    version = int(conn.execute(text("SELECT coalesce(max(version), 0) + 1 FROM config.templates WHERE tenant_id=:t "
                                    "AND template_key=:k AND language=:l"),
                               {"t": tenant_id, "k": key, "l": language}).scalar_one())
    conn.execute(text("INSERT INTO config.templates (tenant_id, template_key, language, version, body, status, "
                      "created_by, created_at, checks) VALUES (:t, :k, :l, :v, :b, 'pending', :by, :at, "
                      "CAST(:c AS jsonb))"),
                 {"t": tenant_id, "k": key, "l": language, "v": version, "b": body, "by": actor, "at": now,
                  "c": json.dumps(checks)})
    AuditChain(SqlAuditStore(conn), FixedClock(now)).append(tenant_id, actor, "template.proposed", {
        "key": key, "language": language, "version": version, "body": body})
    return {"key": key, "language": language, "version": version, "status": "pending", "checks": checks}


def decide(conn: Connection, tenant_id: str, key: str, language: str, version: int, *, approver: str,
           approve: bool, now: datetime) -> dict[str, Any]:
    row = conn.execute(text("SELECT status, created_by, body FROM config.templates WHERE tenant_id=:t AND "
                            "template_key=:k AND language=:l AND version=:v FOR UPDATE"),
                       {"t": tenant_id, "k": key, "l": language, "v": version}).one_or_none()
    if row is None:
        raise TemplateReviewError("template version not found")
    if row.status != "pending":
        raise TemplateReviewError(f"template version is already {row.status}")
    if row.created_by == approver:
        raise TemplateReviewError("maker-checker: the author cannot review their own template")
    check(key, language, row.body)             # rules may have changed since it was proposed
    status = "approved" if approve else "rejected"
    if approve:
        conn.execute(text("UPDATE config.templates SET status='retired' WHERE tenant_id=:t AND template_key=:k "
                          "AND language=:l AND status='approved'"), {"t": tenant_id, "k": key, "l": language})
    conn.execute(text("UPDATE config.templates SET status=:s, decided_by=:by, decided_at=:at WHERE tenant_id=:t "
                      "AND template_key=:k AND language=:l AND version=:v"),
                 {"s": status, "by": approver, "at": now, "t": tenant_id, "k": key, "l": language, "v": version})
    AuditChain(SqlAuditStore(conn), FixedClock(now)).append(tenant_id, approver, f"template.{status}", {
        "key": key, "language": language, "version": version})
    if approve:
        Outbox(conn).add(make_event(event_type="config.template_approved", version=1, tenant_id=tenant_id,
                                    subject_id=f"{key}:{language}", payload={"key": key, "language": language,
                                                                           "version": version},
                                    source="settings", occurred_at=now))
    return {"key": key, "language": language, "version": version, "status": status}


def catalogue(conn: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Every template key × language: what is live now, plus pending proposals and history."""
    rows = conn.execute(text("SELECT template_key, language, version, body, status, created_by, created_at, "
                             "decided_by, decided_at FROM config.templates WHERE tenant_id=:t "
                             "ORDER BY template_key, language, version DESC"), {"t": tenant_id}).all()
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r.template_key, r.language), []).append(dict(r._mapping))
    out = []
    for key, spec in specs().items():
        for lang in LANGUAGES:
            versions = by.get((key, lang), [])
            live = resolve(conn, tenant_id, key, lang)
            if live.language != lang and not versions:
                continue                      # no template in this language at all (falls back to English)
            out.append({"key": key, "language": lang, "channel": spec["channel"],
                        "mandatory": bool(spec.get("mandatory")), "required": spec["required"],
                        "allowed": spec["allowed"], "live": {"version": live.version, "source": live.source,
                                                             "body": live.body, "language": live.language},
                        "versions": versions})
    return out

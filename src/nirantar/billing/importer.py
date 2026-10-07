"""Customer CSV import (P8.7): a business brings its existing customers in one step.

Columns (header row, case-insensitive; only `name` and `phone` are required):
  name, phone, language (en|hi|te), email, reference, whatsapp_consent (yes/no), consent_source, plan, start_on

Always two passes: `dry_run=True` validates every row and reports problems per row without writing anything; the
real run writes all valid rows in ONE transaction (all or nothing for the valid set) and skips invalid ones.
Consent is never assumed: `whatsapp_consent=yes` needs `consent_source` (how the customer agreed). Duplicates are
found by the keyed phone hash (no decryption) — inside the file and against existing customers.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from nirantar.billing.plans import PlanError, enroll
from nirantar.billing.service import NewCustomer, create_customer
from nirantar.core.crypto import lookup_hash
from nirantar.core.ids import new_id

MAX_ROWS = 2000
MAX_BYTES = 1_000_000
_PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
YES, NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0", ""}


class ImportError_(ValueError):
    pass


@dataclass
class Row:
    line: int
    name: str
    phone: str
    language: str
    email: str | None
    reference: str | None
    consent: bool
    source: str | None
    plan: str | None
    start_on: date | None
    problems: list[str] = field(default_factory=list)


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"[\s\-()]", "", raw or "")
    if re.fullmatch(r"[6-9]\d{9}", digits):
        return "+91" + digits
    if re.fullmatch(r"0[6-9]\d{9}", digits):
        return "+91" + digits[1:]
    if re.fullmatch(r"91[6-9]\d{9}", digits):
        return "+" + digits
    return digits


def parse(content: str) -> list[Row]:
    if len(content.encode()) > MAX_BYTES:
        raise ImportError_("file is larger than 1 MB")
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    if reader.fieldnames is None:
        raise ImportError_("the file has no header row")
    header = {h.strip().lower() for h in reader.fieldnames if h}
    if not {"name", "phone"} <= header:
        raise ImportError_("columns 'name' and 'phone' are required")
    rows: list[Row] = []
    for i, raw in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            raise ImportError_(f"at most {MAX_ROWS} customers per file")
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        consent_raw = r.get("whatsapp_consent", "").lower()
        start = r.get("start_on") or ""
        row = Row(i, r.get("name", ""), normalise_phone(r.get("phone", "")), (r.get("language") or "en").lower(),
                  r.get("email") or None, r.get("reference") or None, consent_raw in YES,
                  r.get("consent_source") or None, r.get("plan") or None, None)
        if not 2 <= len(row.name) <= 80:
            row.problems.append("name must be 2-80 characters")
        if not _PHONE.match(row.phone):
            row.problems.append("phone must be a 10-digit Indian mobile or +<country><number>")
        if row.language not in ("en", "hi", "te"):
            row.problems.append("language must be en, hi or te")
        if row.email and not _EMAIL.match(row.email):
            row.problems.append("email is not valid")
        if consent_raw not in YES | NO:
            row.problems.append("whatsapp_consent must be yes or no")
        if row.consent and not row.source:
            row.problems.append("consent_source is required when whatsapp_consent is yes")
        if start:
            try:
                row.start_on = date.fromisoformat(start)
            except ValueError:
                row.problems.append("start_on must be YYYY-MM-DD")
        if row.start_on and not row.plan:
            row.problems.append("start_on given without a plan")
        rows.append(row)
    if not rows:
        raise ImportError_("the file has no customers")
    return rows


def run(conn: Connection, tenant_id: str, content: str, *, actor: str, now: datetime,
        dry_run: bool) -> dict[str, Any]:
    rows = parse(content)
    plans = {r.name.lower(): r.plan_id
             for r in conn.execute(text("SELECT name, plan_id FROM billing.plans WHERE active"))}
    existing = {r[0] for r in conn.execute(text(
        "SELECT contact_hash FROM billing.customers WHERE contact_hash IS NOT NULL"))}
    refs = {r[0] for r in conn.execute(text("SELECT external_ref FROM billing.customers"))}
    seen: set[str] = set()
    for row in rows:                     # every check on every row: one upload shows the founder every problem
        if _PHONE.match(row.phone):
            h = lookup_hash(row.phone, tenant_id)
            if h in existing:
                row.problems.append("a customer with this phone already exists")
            elif h in seen:
                row.problems.append("duplicate phone in this file")
            seen.add(h)
        if row.reference and row.reference in refs:
            row.problems.append("a customer with this reference already exists")
        if row.plan and row.plan.lower() not in plans:
            row.problems.append(f"no active plan named '{row.plan}'")
        if row.start_on and row.start_on < now.date():
            row.problems.append("start_on is in the past")
    valid = [r for r in rows if not r.problems]
    report: dict[str, Any] = {
        "rows": len(rows), "valid": len(valid), "invalid": len(rows) - len(valid),
        "with_consent": sum(r.consent for r in valid), "to_enroll": sum(bool(r.plan) for r in valid),
        "problems": [{"line": r.line, "name": r.name, "problems": r.problems} for r in rows if r.problems][:200],
        "dry_run": dry_run,
    }
    if dry_run or not valid:
        return report
    created, enrolled, debits = 0, 0, 0
    for r in valid:
        consents: dict[str, Any] = {"whatsapp": r.consent}
        if r.consent:
            consents["evidence"] = {"whatsapp": {"source": r.source, "recorded_by": actor, "at": now.isoformat(),
                                                 "via": "csv_import"}}
        cid = create_customer(conn, tenant_id, NewCustomer(r.reference or new_id("ref"), r.name, r.phone, r.email,
                                                           r.language, consents=consents))
        created += 1
        if r.plan:
            try:
                out = enroll(conn, tenant_id, cid, plans[r.plan.lower()], r.start_on or now.date(), now)
            except PlanError as exc:              # validated above; a race (plan archived meanwhile) aborts all
                raise ImportError_(f"line {r.line}: {exc}") from exc
            enrolled += 1
            debits += len(out["debits_created"])
    return {**report, "created": created, "enrolled": enrolled, "debits_created": debits}

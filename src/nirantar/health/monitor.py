"""Payment health monitor: is an issuer failing right now — and who did it hurt?

Detect   every scan: hourly attempts and TECHNICAL failures per (rail, issuer) over the last LOOKBACK hours, summed
         across businesses (each business is read inside its own RLS scope; only counts leave it). M4's Bayesian
         online change-point detector (Beta-Binomial, Adams & MacKay) decides whether a new, materially worse regime
         started; a minimum number of attempts keeps one unlucky customer from becoming an "incident".
Explain  while an incident is open, a failure on that issuer is triaged BANK_TECHNICAL: the customer is not chased
         for the bank's fault (failure_triage).
Recover  when the incident closes, every debit it failed (technical error, inside the incident window, still unpaid)
         gets one honest message with a fresh payment link — through the gateway, per business, idempotent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import Engine, text

from nirantar.agents.failure_triage import RULES
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx

LOOKBACK = timedelta(hours=72)
MIN_ATTEMPTS = 8                 # an hour with fewer attempts cannot open an incident on its own
RECENT_HOURS = 2                 # an alarm in the last 2 complete hours opens; 2 healthy hours close
DETECTOR = "bocpd:v1"
TECHNICAL_CODES = frozenset({"GATEWAY_ERROR", "SERVER_ERROR", "BANK_TECHNICAL_ERROR", "BAD_GATEWAY", "TIMEOUT"})
TECHNICAL_REASONS = frozenset(r for r, cat in RULES.items() if cat == "BANK_TECHNICAL")


def is_technical(error_code: str | None, error_reason: str | None) -> bool:
    return (error_code or "").upper() in TECHNICAL_CODES or (error_reason or "").strip().lower() in TECHNICAL_REASONS


@dataclass
class Series:
    rail: str
    issuer: str
    hours: list[datetime]
    attempts: np.ndarray
    failures: np.ndarray


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def collect(engine: Engine, tenants: list[str], now: datetime) -> list[Series]:
    """Hourly counts per (rail, issuer), summed over businesses. Only aggregates cross a tenant boundary."""
    end = _hour(now)                                  # complete hours only
    start = end - LOOKBACK
    counts: dict[tuple[str, str], dict[datetime, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    reasons = sorted(TECHNICAL_REASONS)
    for t in tenants:
        with tenant_tx(t, engine) as c:
            rows = c.execute(text(
                "SELECT method, issuer, date_trunc('hour', coalesce(provider_created_at, created_at)) AS h, "
                "count(*) AS n, count(*) FILTER (WHERE status='failed' AND "
                "(upper(coalesce(error_code,'')) = ANY(:codes) "
                "OR lower(coalesce(error_reason,'')) = ANY(:reasons))) AS k FROM billing.payments WHERE issuer IS NOT "
                "NULL AND method IS NOT NULL AND coalesce(provider_created_at, created_at) >= :s AND "
                "coalesce(provider_created_at, created_at) < :e GROUP BY 1, 2, 3"),
                {"codes": sorted(TECHNICAL_CODES), "reasons": reasons, "s": start, "e": end}).all()
        for r in rows:
            cell = counts[(r.method, r.issuer)][r.h]
            cell[0] += int(r.n)
            cell[1] += int(r.k)
    hours = [start + timedelta(hours=i) for i in range(int(LOOKBACK.total_seconds() // 3600))]
    out = []
    for (rail, issuer), by_hour in counts.items():
        att = np.array([by_hour[h][0] if h in by_hour else 0 for h in hours], dtype=float)
        fail = np.array([by_hour[h][1] if h in by_hour else 0 for h in hours], dtype=float)
        out.append(Series(rail, issuer, hours, att, fail))
    return out


RATE_FACTOR = 4.0                # an incident hour fails ≥ 4× the issuer's normal rate…
RATE_FLOOR = 0.03                # …and at least 3 points above it (a 0.2% → 0.8% blip is not an outage)


def assess(s: Series) -> dict[str, Any]:
    """Onset = M4's change-point alarm (BOCPD); state = the trailing run of hours failing well above the issuer's
    robust baseline. A change-point detector stops alarming once the bad rate is the new normal, so it decides only
    THAT an incident began; whether it is still going on is the elevated recent rate."""
    from nirantar.ml.models.m4_bank_health import bocpd_alarms

    att, fail = s.attempts, s.failures
    traffic = att > 0
    rates = np.divide(fail, att, out=np.zeros_like(fail), where=traffic)
    baseline = float(np.median(rates[traffic])) if traffic.any() else 0.0           # robust to the incident itself
    limit = max(RATE_FACTOR * baseline, baseline + RATE_FLOOR)
    elevated = traffic & (rates >= limit)
    onsets = bocpd_alarms(att, fail) & (att >= MIN_ATTEMPTS)
    # trailing run of elevated hours (hours without traffic neither break nor extend it)
    i = len(att)
    while i > 0 and (elevated[i - 1] or not traffic[i - 1]):
        i -= 1
    run = slice(i, None)
    while i < len(att) and not traffic[i]:
        i += 1
    recent_att, recent_fail = att[-RECENT_HOURS:].sum(), fail[-RECENT_HOURS:].sum()
    recent_bad = recent_att >= MIN_ATTEMPTS and recent_fail / recent_att >= limit
    ongoing = bool(recent_bad and elevated[run].any() and onsets[run].any())
    window = run if ongoing else slice(-RECENT_HOURS, None)
    w_rates = rates[window]
    return {"alarm_now": ongoing,
            "healthy_now": bool(recent_att > 0 and not recent_bad),
            "started_at": s.hours[i] if ongoing else None,
            "baseline_rate": baseline, "peak_rate": float(w_rates.max()) if len(w_rates) else 0.0,
            "attempts": int(att[window].sum()), "failures": int(fail[window].sum())}


def scan(engine: Engine, owner: Engine, tenants: list[str], now: datetime) -> dict[str, Any]:
    """Open incidents that started; close incidents that recovered. Returns what changed."""
    opened, closed = [], []
    series = collect(engine, tenants, now)
    with owner.begin() as c:
        open_rows = {(r.rail, r.issuer): r for r in c.execute(text(
            "SELECT incident_id, rail, issuer FROM core.payment_incidents WHERE status='open'"))}
        for s in series:
            a = assess(s)
            key = (s.rail, s.issuer)
            if a["alarm_now"] and key not in open_rows:
                iid = new_id("inc")
                c.execute(text("INSERT INTO core.payment_incidents (incident_id, rail, issuer, started_at, "
                               "detected_at, "
                               "status, baseline_rate, peak_rate, attempts, failures, detector) VALUES (:i, :r, :s, "
                               ":st, :d, 'open', :b, :p, :a, :f, :det)"),
                          {"i": iid, "r": s.rail, "s": s.issuer, "st": a["started_at"], "d": now,
                           "b": a["baseline_rate"], "p": a["peak_rate"], "a": a["attempts"], "f": a["failures"],
                           "det": DETECTOR})
                opened.append({"incident_id": iid, "rail": s.rail, "issuer": s.issuer, **_jsonable(a)})
            elif key in open_rows and a["healthy_now"]:
                iid = open_rows[key].incident_id
                c.execute(text("UPDATE core.payment_incidents SET status='closed', ended_at=:e WHERE incident_id=:i"),
                          {"e": _hour(now) - timedelta(hours=RECENT_HOURS), "i": iid})
                closed.append({"incident_id": iid, "rail": s.rail, "issuer": s.issuer})
            elif key in open_rows and a["alarm_now"]:
                c.execute(text("UPDATE core.payment_incidents SET peak_rate=greatest(peak_rate, :p), "
                               "attempts=:a, failures=:f WHERE incident_id=:i"),
                          {"p": a["peak_rate"], "a": a["attempts"], "f": a["failures"],
                           "i": open_rows[key].incident_id})
    return {"series": len(series), "opened": opened, "closed": closed}


def _jsonable(a: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(a, default=str))
    return out


def degraded_for(conn: Any, method: str | None, issuer: str | None, at: datetime | None) -> str | None:
    """The incident that explains a failure on (method, issuer) at `at`, if any (used by live triage)."""
    if not method or not issuer:
        return None
    row = conn.execute(text(
        "SELECT incident_id FROM core.payment_incidents WHERE rail=:r AND issuer=:s AND started_at <= :t AND "
        "(ended_at IS NULL OR ended_at >= :t) ORDER BY started_at DESC LIMIT 1"),
        {"r": method, "s": issuer, "t": at}).first()
    return str(row[0]) if row else None


def affected_debits(engine: Engine, tenant_id: str, incident: Any) -> list[dict[str, Any]]:
    """Unpaid debits whose failure was a technical decline on this issuer inside the incident window."""
    reasons = sorted(TECHNICAL_REASONS)
    with tenant_tx(tenant_id, engine) as c:
        rows = c.execute(text(
            "SELECT DISTINCT d.debit_id, d.customer_id FROM billing.debits d JOIN billing.payments p ON "
            "p.tenant_id=d.tenant_id AND p.debit_id=d.debit_id WHERE d.status='failed' AND p.status='failed' AND "
            "p.method=:r AND p.issuer=:s AND coalesce(p.provider_created_at, p.created_at) >= :st AND "
            "coalesce(p.provider_created_at, p.created_at) < :en AND (upper(coalesce(p.error_code,'')) = ANY(:codes) "
            "OR lower(coalesce(p.error_reason,'')) = ANY(:reasons)) AND NOT EXISTS (SELECT 1 FROM "
            "ops.incident_recoveries x WHERE x.tenant_id=d.tenant_id AND x.incident_id=:i AND x.debit_id=d.debit_id)"),
            {"r": incident.rail, "s": incident.issuer, "st": incident.started_at, "en": incident.ended_at,
             "codes": sorted(TECHNICAL_CODES), "reasons": reasons, "i": incident.incident_id}).all()
    return [{"debit_id": r.debit_id, "customer_id": r.customer_id} for r in rows]


def record_recovery(engine: Engine, tenant_id: str, incident_id: str, debit_id: str, customer_id: str,
                    status: str, detail: dict[str, Any], now: datetime) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(text("INSERT INTO ops.incident_recoveries (tenant_id, incident_id, debit_id, customer_id, action, "
                       "status, detail, created_at) VALUES (:t, :i, :d, :c, 'payment_request', :s, CAST(:x AS jsonb), "
                       ":n) ON CONFLICT DO NOTHING"),
                  {"t": tenant_id, "i": incident_id, "d": debit_id, "c": customer_id, "s": status,
                   "x": json.dumps(detail, default=str), "n": now})

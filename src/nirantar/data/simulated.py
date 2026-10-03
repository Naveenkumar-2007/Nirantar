"""Simulated merchant: RecurSim history rendered as Razorpay-shaped provider records (a BackfillSource).

Purpose: exercise the REAL pipeline (backfill → bronze → silver → gold → features → per-tenant training →
rollout) at realistic volume before a design partner's data exists. It is synthetic: tenants loaded this way
must be created with settings {"synthetic": true}; model cards and the UI then label every metric as synthetic.
Nothing here is used for a real tenant.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from nirantar.ml.recursim import SimConfig, SimResult, simulate

REASON = {"INSUFFICIENT_FUNDS": "insufficient_funds", "BANK_TECHNICAL": "bank_technical_error",
          "MANDATE_REVOKED": "mandate_revoked", "CARD_EXPIRED": "card_expired",
          "LIMIT_EXCEEDED": "amount_exceeds_mandate_limit"}
METHOD = {"upi_autopay": "upi", "card": "card", "emandate": "emandate"}


class SimulatedMerchantSource:
    provider = "simulated"

    def __init__(self, sim: SimResult | None = None, *, config: SimConfig | None = None,
                 end: datetime | None = None) -> None:
        self.sim = sim or simulate(config or SimConfig(n_customers=600, months=12))
        last = pd.Timestamp(self.sim.debits.executed_at.max()).to_pydatetime().replace(tzinfo=UTC)
        self.shift = (end or datetime.now(UTC)) - timedelta(days=1) - last   # history ends yesterday
        self._payments, self._subs = self._render()

    def _ts(self, v: Any) -> int:
        return int((pd.Timestamp(v).to_pydatetime().replace(tzinfo=UTC) + self.shift).timestamp())

    def _render(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        d = self.sim.debits
        recovery = {str(r["debit_id"]): (bool(r["recovered"]), float(r["recovery_days"]) if r["recovered"] else None)
                    for r in self.sim.failures.to_dict("records")}
        payments: list[dict[str, Any]] = []
        for r in d.to_dict("records"):
            cust, did = str(r["customer_id"])[-6:], str(r["debit_id"])
            sub = f"sub_sim{cust}"
            base = {"entity": "payment", "amount": int(r["amount_minor"]), "currency": "INR",
                    "method": METHOD.get(str(r["rail"]), str(r["rail"])), "bank": f"BANK{int(r['bank_id'])}",
                    "customer_id": f"cust_sim{cust}", "notes": {"subscription_id": sub}}
            first = {**base, "id": f"pay_{did}", "created_at": self._ts(r["executed_at"])}
            if r["succeeded"]:
                payments.append({**first, "status": "captured"})
                continue
            code = str(r["failure_code"])
            payments.append({**first, "status": "failed", "error_reason": REASON.get(code, code.lower()),
                             "error_code": "GATEWAY_ERROR" if code == "BANK_TECHNICAL" else "BAD_REQUEST_ERROR"})
            rec, days = recovery.get(did, (False, None))
            if rec and days is not None:
                payments.append({**base, "id": f"pay_{did}_r", "status": "captured",
                                 "created_at": self._ts(pd.Timestamp(r["executed_at"]) + pd.Timedelta(days=days))})
        first_seen = d.groupby("customer_id").executed_at.min()
        subs = [{"id": f"sub_sim{str(c)[-6:]}", "entity": "subscription", "customer_id": f"cust_sim{str(c)[-6:]}",
                 "status": "active", "created_at": self._ts(ts), "paid_count": 0}
                for c, ts in first_seen.items()]
        return payments, subs

    def list(self, entity: str, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        lo, hi = since.timestamp(), until.timestamp()
        rows = self._payments if entity == "payments" else self._subs if entity == "subscriptions" else []
        return (r for r in rows if lo <= r["created_at"] < hi)

    def invoices_for(self, subscription_id: str) -> Iterator[dict[str, Any]]:
        return iter(())

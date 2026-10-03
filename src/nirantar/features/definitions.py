"""Feature definitions (the feature store's registry). A feature set is versioned: changing a formula means a new
version, and models record the version they were trained on (serving refuses a mismatch).

Entity: a subscription (canonical Nirantar subscription id when the provider subscription is mapped, else the
provider's id). Features are computed at `as_of` = due time − LEAD (the Debit Strategist acts at T-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

FEATURE_SET = "subscription_payment"
VERSION = "v1"
FEATURE_SET_ID = f"{FEATURE_SET}:{VERSION}"
LEAD = timedelta(days=3)                   # prediction time before the due date
HISTORY_CAP = 24                           # cycles of history kept per entity in the online store
SMOOTHING_K = 5.0                          # pseudo-cycles of tenant base rate in the smoothed failure rate
RECOVERY_HORIZON = timedelta(days=30)      # must equal gold.RECOVERY_HORIZON (asserted in tests)


def _cold_start_rate() -> float:
    from nirantar.settings.schema import platform_defaults

    return float(platform_defaults()["ml"]["cold_start_failure_rate"])


COLD_START_RATE = _cold_start_rate()       # used only while the tenant has no attempts in the last 30 days


@dataclass(frozen=True)
class Feature:
    name: str
    kind: str          # numeric | categorical
    description: str


FEATURES: tuple[Feature, ...] = (
    Feature("log_amount", "numeric", "log of the amount due (paise)"),
    Feature("amount_change_ratio", "numeric", "amount / last known amount − 1 (0 if no history)"),
    Feature("tenure_days", "numeric", "days since the entity's first attempted cycle"),
    Feature("n_prev", "numeric", "past cycles whose first attempt is known"),
    Feature("fail_rate_all", "numeric", "share of past first attempts that failed"),
    Feature("fail_rate_last3", "numeric", "same, last 3 cycles"),
    Feature("smoothed_fail_rate", "numeric", "(fails + k·tenant rate) / (n + k): shrunk towards the tenant"),
    Feature("last_failed", "numeric", "1 if the previous first attempt failed"),
    Feature("consecutive_failures", "numeric", "run of failed first attempts ending at the previous cycle"),
    Feature("days_since_last_failure", "numeric", "days since the last failed first attempt (999 if none)"),
    Feature("fails_funds_last6", "numeric", "insufficient-funds failures in the last 6 cycles"),
    Feature("fails_technical_last6", "numeric", "bank/gateway technical failures in the last 6 cycles"),
    Feature("fails_mandate_last6", "numeric", "mandate revoked / card expired / limit failures, last 6 cycles"),
    Feature("known_recovery_rate", "numeric", "recovered / failures whose outcome was known (−1 if none)"),
    Feature("mean_days_to_recover", "numeric", "mean days to recover among known recoveries (−1 if none)"),
    Feature("due_day", "numeric", "day of month of the due date"),
    Feature("due_weekday", "numeric", "weekday of the due date (0 = Monday)"),
    Feature("days_to_month_end", "numeric", "days from the due date to the month end"),
    Feature("paid_day_distance", "numeric", "circular distance (days) from due day to the usual paying day"),
    Feature("paid_day_confidence", "numeric", "concentration of past paying days (0..1)"),
    Feature("tenant_fail_rate_30d", "numeric", "tenant-wide first-attempt failure rate, last 30 days"),
    Feature("tenant_technical_rate_24h", "numeric", "tenant-wide technical failure rate, last 24h"),
    Feature("tenant_technical_rate_7d", "numeric", "tenant-wide technical failure rate, last 7 days"),
    Feature("bank_technical_rate_24h", "numeric", "technical failure rate at this bank, last 24h"),
    Feature("bank_technical_rate_7d", "numeric", "technical failure rate at this bank, last 7 days"),
    Feature("method", "categorical", "payment method of the mandate (upi/card/emandate/…)"),
    Feature("bank", "categorical", "bank code when the provider reports it, else 'unknown'"),
)
NUMERIC = [f.name for f in FEATURES if f.kind == "numeric"]
CATEGORICAL = [f.name for f in FEATURES if f.kind == "categorical"]

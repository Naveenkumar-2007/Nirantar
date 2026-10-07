from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from nirantar.health.monitor import Series, assess, is_technical
from nirantar.payments.issuer import issuer_of

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def series(rates: list[float], attempts: int = 60) -> Series:
    hours = [T0 + timedelta(hours=i) for i in range(len(rates))]
    att = np.full(len(rates), float(attempts))
    return Series("upi", "HDFC", hours, att, np.round(att * np.array(rates)))


def test_an_outage_is_detected_with_its_start_hour() -> None:
    a = assess(series([0.01] * 66 + [0.35] * 6))
    assert a["alarm_now"] and a["started_at"] == T0 + timedelta(hours=66)
    assert a["baseline_rate"] < 0.03 and a["peak_rate"] >= 0.3


def test_steady_noise_is_not_an_incident() -> None:
    rng = np.random.default_rng(7)
    a = assess(series(list(rng.uniform(0.0, 0.03, 72))))
    assert not a["alarm_now"]


def test_a_recovered_issuer_reads_healthy() -> None:
    a = assess(series([0.01] * 60 + [0.4] * 6 + [0.01] * 6))
    assert not a["alarm_now"] and a["healthy_now"]


def test_one_unlucky_customer_is_not_an_outage() -> None:
    a = assess(series([0.0] * 71 + [1.0], attempts=2))        # 2 attempts, both failed: below the volume gate
    assert not a["alarm_now"]


@pytest.mark.parametrize(("entity", "issuer"), [
    ({"method": "upi", "vpa": "asha@okhdfcbank"}, "HDFC"),
    ({"method": "upi", "vpa": "ravi@ybl"}, "YES"),
    ({"method": "upi", "vpa": "x@newpsp"}, "UPI:newpsp"),
    ({"method": "netbanking", "bank": "sbin"}, "SBIN"),
    ({"method": "emandate", "bank": "ICIC"}, "ICIC"),
    ({"method": "wallet", "wallet": "paytm"}, "WALLET:PAYTM"),
    ({"method": "card", "card": {"issuer": "hdfc"}}, "CARD:HDFC"),
    ({"method": "card"}, None),
])
def test_issuer_from_provider_fields(entity: dict, issuer: str | None) -> None:
    assert issuer_of(entity) == issuer


def test_only_technical_declines_count() -> None:
    assert is_technical("GATEWAY_ERROR", None) and is_technical(None, "bank_down")
    assert not is_technical("BAD_REQUEST_ERROR", "insufficient_funds")

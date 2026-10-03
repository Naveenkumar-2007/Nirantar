"""Live Razorpay TEST-MODE checks. Skipped unless test credentials are configured.

Refuses to run with live keys: only `rzp_test_` key ids are accepted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.payments.domain import LinkRequest, ProviderRejected, ProviderUnavailable
from nirantar.payments.providers.razorpay import RazorpayProvider

pytestmark = pytest.mark.sandbox

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")


@pytest.fixture(scope="module")
def rzp() -> RazorpayProvider:
    if not KEY_ID or not KEY_SECRET:
        pytest.skip("Razorpay test credentials not configured")
    if not KEY_ID.startswith("rzp_test_"):
        pytest.fail("refusing to run sandbox tests with non-test Razorpay credentials")
    return RazorpayProvider(KEY_ID, KEY_SECRET)


def test_list_payments_authenticates_and_maps(rzp: RazorpayProvider) -> None:
    now = datetime.now(UTC)
    payments = list(rzp.list_payments(now - timedelta(days=30), now))
    for p in payments[:5]:
        assert p.provider == "razorpay" and p.amount.currency


def test_create_payment_link_is_idempotent_on_reference(rzp: RazorpayProvider) -> None:
    ref = new_id("lnk")[:40]
    req = LinkRequest(Money.of("1"), ref, "Nirantar sandbox contract test", "Test Customer", None, None,
                      expire_by=datetime.now(UTC) + timedelta(days=1))
    try:
        link = rzp.create_payment_link(req)
    except ProviderUnavailable as exc:
        if "test mode limit" in str(exc):
            pytest.skip(f"Razorpay test-mode payment-link quota reached (observed 2026-09-28): {exc}")
        raise
    assert link.url.startswith("https://") and link.amount == Money.of("1")
    # A second create with the same reference must not produce a second link.
    try:
        again = rzp.create_payment_link(req)
    except ProviderRejected:
        pytest.fail("duplicate reference_id should resolve to the existing link")
    assert again.link_id == link.link_id


def test_unknown_payment_is_a_clean_rejection(rzp: RazorpayProvider) -> None:
    with pytest.raises(ProviderRejected):
        rzp.fetch_payment("pay_doesnotexist000")


def test_backfill_source_reads_history_and_reports_disabled_products(rzp: RazorpayProvider) -> None:
    """Live: the backfill source pages through test-mode history. Products not activated on the account
    (observed 2026-09-29: subscriptions → 401) must be reported per entity, not crash the whole backfill."""
    from nirantar.data.sources import RazorpaySource
    from nirantar.payments.domain import ProviderAuthError

    src = RazorpaySource(KEY_ID, KEY_SECRET)
    now = datetime.now(UTC)
    payments = list(src.list("payments", now - timedelta(days=90), now))
    customers = list(src.list("customers", now - timedelta(days=90), now))
    assert all("id" in p and "amount" in p for p in payments)
    assert all("id" in c for c in customers)
    try:
        list(src.list("subscriptions", now - timedelta(days=1), now))
    except ProviderAuthError:
        pass                                    # product not enabled on this test account: handled by bronze

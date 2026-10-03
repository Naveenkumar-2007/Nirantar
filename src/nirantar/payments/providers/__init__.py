"""Provider adapters. Construct them through `build_provider` so credentials come from config."""

from __future__ import annotations

import os

from nirantar.payments.domain import PaymentProvider


def build_provider(name: str) -> PaymentProvider:
    if name == "razorpay":
        from nirantar.payments.providers.razorpay import RazorpayProvider

        return RazorpayProvider(os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", ""))
    if name == "cashfree":
        from nirantar.payments.providers.cashfree import CashfreeProvider

        return CashfreeProvider(
            os.environ.get("CASHFREE_CLIENT_ID", ""),
            os.environ.get("CASHFREE_CLIENT_SECRET", ""),
            sandbox=os.environ.get("NIRANTAR_ENV", "local") != "production",
        )
    if name == "mock":
        from nirantar.payments.providers.mock import MockProvider

        return MockProvider()
    raise ValueError(f"unknown provider {name!r}")

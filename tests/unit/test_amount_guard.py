"""Regression: the ₹-amount guard must not read amounts out of URLs/ids (found in the demo tenant: a correct
₹1,499.00 message was blocked because the payment-link id contained '...NRS331...')."""

from nirantar.core.money import Money
from nirantar.evidence.service import extract_payment_fields
from nirantar.mcp.tools import amounts_in_text


def test_amounts_inside_identifiers_are_ignored() -> None:
    msg = ("Hi Aditya, your ₹1,499.00 payment for Chai Club Family didn't go through. "
           "You can pay securely here: https://mock.pay/dbt_01M3MJA4NRS331MQFM6YAQZFHN.1")
    assert amounts_in_text(msg) == [Money.of("1499")]
    assert amounts_in_text("ref XRS55 and TRINR99 only") == []


def test_real_amount_formats_are_found() -> None:
    assert amounts_in_text("Pay Rs. 999 or INR 1,000.50 or ₹ 12") == [Money.of("999"), Money.of("1000.50"), Money.of("12")]


def test_ocr_lines_still_extract_amounts() -> None:
    f = extract_payment_fields(["PaymentSuccessful", "Rs999.00", "UTR:412345678901", "id NRS331X"])
    assert f["amount_minor"] == 99900 and f["utr"] == "412345678901"

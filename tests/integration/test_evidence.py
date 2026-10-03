from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import Engine, text

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.core.money import Money
from nirantar.db.session import tenant_tx
from nirantar.evidence import objectstore
from nirantar.evidence.service import aa_statement_evidence, evidence_hash_ok, screenshot_evidence, voice_note_evidence
from nirantar.payments.providers.mock import MockProvider
from nirantar.voice.providers import StubSpeech

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)


def _s3_up() -> bool:
    try:
        objectstore.ensure_bucket()
        return True
    except Exception:
        return False


def shot(lines: list[str], software: str | None = None) -> bytes:
    img = Image.new("RGB", (560, 320), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        f = ImageFont.load_default()
    for i, line in enumerate(lines):
        d.text((30, 20 + i * 52), line, fill="black", font=f)
    buf = io.BytesIO()
    meta = PngInfo()
    if software:
        meta.add_text("Software", software)
    img.save(buf, "PNG", pnginfo=meta)
    return buf.getvalue()


@pytest.fixture
def tenant(app_engine: Engine) -> str:
    if not _s3_up():
        pytest.skip("object store not reachable")
    t = new_id("ten")
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "x")
    return t


def test_genuine_screenshot_matches_provider_record(app_engine: Engine, tenant: str) -> None:
    mock = MockProvider()
    sub = mock.add_subscription("cus_1", Money.of("999"))
    mock.charge(sub, succeed=True, at=NOW - timedelta(minutes=5))
    image = shot(["Payment Successful", "Rs 999.00", "To: Chai Club", "UTR: 412345678901"])
    with tenant_tx(tenant, app_engine) as c:
        ev = screenshot_evidence(c, tenant, image, customer_id="cus_1", case_id="cas_1", provider=mock,
                                 claimed_at=NOW, now=NOW)
    f = ev.extracted_fields
    assert f["amount_minor"] == 99900 and f["utr"] == "412345678901" and f["status_text"] == "success"
    assert f["verdict"] == "matched_provider_record" and ev.verified_against == "provider"
    assert evidence_hash_ok(ev.hash, objectstore.get(ev.source_uri))          # stored bytes are intact


def test_fake_and_edited_screenshots_are_never_accepted(app_engine: Engine, tenant: str) -> None:
    mock = MockProvider()   # provider has NO such payment
    fake = shot(["Payment Successful", "Rs 999.00", "UTR: 41234567"], software="Adobe Photoshop 25.1")
    with tenant_tx(tenant, app_engine) as c:
        ev = screenshot_evidence(c, tenant, fake, customer_id="cus_1", case_id="cas_1", provider=mock,
                                 claimed_at=NOW, now=NOW)
        stored = c.execute(text("SELECT extracted_fields FROM ai.evidence WHERE evidence_id=:e"),
                           {"e": ev.evidence_id}).scalar_one()
    sig = set(ev.extracted_fields["tamper_signals"])
    assert ev.extracted_fields["verdict"] == "not_verified"
    assert "claims_success_without_provider_record" in sig and "utr_not_12_digits" in sig
    assert any(s.startswith("edited_with:adobe photoshop") for s in sig)
    assert stored["verdict"] == "not_verified"


def test_voice_note_and_aa_statement(app_engine: Engine, tenant: str) -> None:
    with tenant_tx(tenant, app_engine) as c:
        vn = voice_note_evidence(c, tenant, b"\x00\x00" * 8000, 8000, "hi",
                                 StubSpeech(["मैं कल भुगतान करूंगा, मेरा OTP 482913 है"]),
                                 customer_id="cus_1", case_id="cas_1", now=NOW)
        aa = {"Account": {"Transactions": {"Transaction": [
            {"type": "CREDIT", "amount": "52000", "valueDate": f"2026-0{m}-07", "narration": "NEFT SALARY ACME LTD"}
            for m in range(4, 10)] + [
            {"type": "DEBIT", "amount": "999", "valueDate": "2026-09-05", "narration": "UPI AUTOPAY CHAI CLUB"}]}}}
        ev = aa_statement_evidence(c, tenant, aa, customer_id="cus_1", consent_id="cns_aa_1", now=NOW)
    assert vn.extracted_fields["intent"] == "promise_to_pay" and "482913" not in vn.extracted_fields["transcript_redacted"]
    assert ev.extracted_fields["salary_credit_dates"][0] == "2026-04-07"
    assert abs(ev.extracted_fields["cash_window_day"] - 7) < 0.5 and ev.confidence > 0.95

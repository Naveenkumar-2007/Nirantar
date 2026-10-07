"""Demo mode can never reach a real person or a real payment provider."""

from __future__ import annotations

import pytest

from nirantar.approvals.executor import default_comms
from nirantar.comms.sink import MockCommsSink
from nirantar.core.demo import REAL_CREDENTIALS, DemoModeViolation, assert_safe
from nirantar.voice.exotel import default_client


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in REAL_CREDENTIALS:
        monkeypatch.delenv(k, raising=False)


def test_demo_refuses_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIRANTAR_DEMO", "1")
    monkeypatch.setenv("NIRANTAR_ENV", "demo")
    assert_safe()
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "EAAx")
    with pytest.raises(DemoModeViolation, match="WHATSAPP_ACCESS_TOKEN"):
        assert_safe()


def test_demo_cannot_be_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIRANTAR_DEMO", "1")
    monkeypatch.setenv("NIRANTAR_ENV", "production")
    with pytest.raises(DemoModeViolation):
        assert_safe()


def test_demo_messages_go_to_the_mock_and_voice_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIRANTAR_DEMO", "1")
    monkeypatch.setenv("NIRANTAR_ENV", "demo")
    assert isinstance(default_comms(None), MockCommsSink)  # type: ignore[arg-type]
    assert default_client() is None


def test_demo_connects_only_the_mock_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from nirantar.billing.service import connect_provider

    monkeypatch.setenv("NIRANTAR_DEMO", "1")
    with pytest.raises(DemoModeViolation):
        connect_provider(None, "ten_x", "razorpay", "test", "env:X", "env:Y")  # type: ignore[arg-type]

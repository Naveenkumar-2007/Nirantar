"""Self-serve onboarding (P8.2, ADR-0019): checklist, Razorpay keys verified then stored encrypted per business,
webhooks verified with the generated secret, invites claimable only with a verified email, history import start."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.db.session import tenant_tx
from nirantar.onboarding.service import OnboardingError
from nirantar.payments.providers.resolver import ProviderResolver
from nirantar.security.oidc import OIDCVerifier

pytestmark = pytest.mark.integration
ISS = "http://idp.test/realms/nirantar"
KEY_ID, KEY_SECRET = "rzp_test_Abcdef123456", "s3cr3t-value-for-tests"


class _Stub:
    def __init__(self, public: Any) -> None:
        self.public = public

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return type("K", (), {"key": self.public})()


@pytest.fixture(scope="module")
def keys() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verify(key_id: str, key_secret: str) -> None:
    if key_secret != KEY_SECRET:
        raise OnboardingError("Razorpay rejected these keys (check the key id and secret)")


@pytest.fixture(scope="module")
def client(app_engine: Engine, owner_engine: Engine, keys: Any) -> TestClient:
    v = OIDCVerifier(ISS, "nirantar-api")
    v._jwks = _Stub(keys.public_key())  # type: ignore[assignment]
    return TestClient(create_app(Services(engine=app_engine, owner_engine=owner_engine,
                                          extra={"oidc": v, "razorpay_verify": _verify})))


def tok(keys: Any, sub: str, email: str | None = None, verified: bool = False) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISS, "aud": "nirantar-api", "sub": sub, "iat": now, "exp": now + 300, "typ": "Bearer",
              "name": "Owner One", "email": email or f"{sub[:8]}@example.com", "email_verified": verified}
    return {"Authorization": f"Bearer {jwt.encode(claims, keys, algorithm='RS256')}"}


def test_connect_razorpay_then_webhooks_verify_with_the_tenant_secret(client: TestClient, keys: Any,
                                                                      app_engine: Engine) -> None:
    owner = tok(keys, str(uuid.uuid4()))
    t = client.post("/v1/businesses", json={"name": "Chai Club"}, headers=owner).json()["tenant_id"]
    steps = {s["key"]: s["status"] for s in client.get("/v1/onboarding", headers=owner).json()["steps"]}
    assert steps["business"] == "done" and steps["payments"] == "todo" and steps["history"] == "blocked"
    assert client.post("/v1/onboarding/import", headers=owner).status_code == 409        # payments first

    bad = client.post("/v1/onboarding/razorpay", json={"key_id": "not-a-key-0000", "key_secret": KEY_SECRET},
                      headers=owner)
    assert bad.status_code == 422 and "rzp_test_" in bad.json()["detail"]
    rejected = client.post("/v1/onboarding/razorpay", json={"key_id": KEY_ID, "key_secret": "wrong-secret-xyz"},
                           headers=owner)
    assert rejected.status_code == 422 and "rejected" in rejected.json()["detail"]
    live = client.post("/v1/onboarding/razorpay", json={"key_id": "rzp_live_Abcdef123456", "key_secret": KEY_SECRET},
                       headers=owner)
    assert live.status_code == 422 and "production" in live.json()["detail"]

    ok = client.post("/v1/onboarding/razorpay", json={"key_id": KEY_ID, "key_secret": KEY_SECRET}, headers=owner)
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert out["mode"] == "test" and out["webhook_url"].endswith(f"/webhooks/razorpay/{t}")
    with tenant_tx(t, app_engine) as c:
        ref = c.execute(text("SELECT secret_ref FROM core.provider_accounts")).scalar_one()
        blobs = [bytes(r[0]) for r in c.execute(text("SELECT ciphertext FROM core.tenant_secrets"))]
    assert ref.startswith("tenant:") and ";tenant:" in ref                       # references only
    assert all(KEY_SECRET.encode() not in b for b in blobs)                       # encrypted at rest
    rp = ProviderResolver(app_engine).for_tenant(t)                               # decrypts with the tenant key
    assert rp.name == "razorpay"
    steps = {s["key"]: s["status"] for s in client.get("/v1/onboarding", headers=owner).json()["steps"]}
    assert steps["payments"] == "done" and steps["history"] == "todo"

    # a Razorpay webhook signed with the secret Nirantar generated is accepted; a wrong signature is not
    body = json.dumps({"entity": "event", "event": "payment.failed", "payload": {"payment": {"entity": {
        "id": "pay_onb1", "amount": 49900, "currency": "INR", "status": "failed"}}},
        "created_at": int(time.time())}).encode()
    sig = hmac.new(out["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    r = client.post(f"/webhooks/razorpay/{t}", content=body,
                    headers={"X-Razorpay-Signature": sig, "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:10]}"})
    assert r.status_code == 200, r.text
    r = client.post(f"/webhooks/razorpay/{t}", content=body,
                    headers={"X-Razorpay-Signature": "0" * 64, "x-razorpay-event-id": "evt_bad"})
    assert r.status_code == 401


def test_import_starts_the_onboarding_workflow(client: TestClient, keys: Any) -> None:
    import asyncio

    from temporalio.client import Client

    owner = tok(keys, str(uuid.uuid4()))
    t = client.post("/v1/businesses", json={"name": "Import Co"}, headers=owner).json()["tenant_id"]
    client.post("/v1/onboarding/razorpay", json={"key_id": KEY_ID, "key_secret": KEY_SECRET}, headers=owner)
    try:
        r = client.post("/v1/onboarding/import", headers=owner)
    except OSError:
        pytest.skip("Temporal server not reachable")
    assert r.status_code == 200 and r.json()["workflow_id"] == f"onboarding:{t}"
    steps = {s["key"]: s for s in client.get("/v1/onboarding", headers=owner).json()["steps"]}
    assert steps["history"]["status"] == "running" and steps["payments"]["status"] == "done"
    again = client.post("/v1/onboarding/import", headers=owner)                       # no second run meanwhile
    assert again.json()["workflow_id"] == r.json()["workflow_id"]

    async def stop() -> None:                       # no worker serves this test queue: don't leave it running
        c = await Client.connect("localhost:7233")
        await c.get_workflow_handle(f"onboarding:{t}").terminate("test done")
    asyncio.run(stop())


def test_invites_need_a_verified_email(client: TestClient, keys: Any) -> None:
    owner = tok(keys, str(uuid.uuid4()))
    t = client.post("/v1/businesses", json={"name": "Team Co"}, headers=owner).json()["tenant_id"]
    email = f"bob.{uuid.uuid4().hex[:6]}@example.com"
    assert client.post("/v1/team/invites", json={"email": email, "role": "superuser"},
                       headers=owner).status_code == 422
    inv = client.post("/v1/team/invites", json={"email": email, "role": "viewer"}, headers=owner).json()
    bob = str(uuid.uuid4())
    assert client.get("/v1/me", headers=tok(keys, bob, email, verified=False)).json()["businesses"] == []
    me = client.get("/v1/me", headers=tok(keys, bob, email, verified=True)).json()
    assert [b["tenant_id"] for b in me["businesses"]] == [t] and me["businesses"][0]["roles"] == ["viewer"]
    viewer = tok(keys, bob, email, verified=True)
    assert client.post("/v1/team/invites", json={"email": "x@example.com", "role": "viewer"},
                       headers=viewer).status_code == 403                                 # viewers can't invite
    mallory = str(uuid.uuid4())                                                           # invite is single use
    assert client.get("/v1/me", headers=tok(keys, mallory, email, verified=True)).json()["businesses"] == []
    team = client.get("/v1/team", headers=owner).json()
    assert {m["user_sub"] for m in team["members"]} >= {bob} and team["invites"] == []
    assert client.delete(f"/v1/team/invites/{inv['invite_id']}", headers=owner).status_code == 404   # used already

"""People sign in with OIDC (P8, ADR-0018): token verification, businesses and memberships, tenant selection.
Tokens are signed with a test RSA key whose public half the verifier is given — the same checks as against
Keycloak's JWKS (signature, iss, aud, exp)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.demo.seed import seed
from nirantar.security.oidc import OIDCVerifier

pytestmark = pytest.mark.integration
ISS = "http://idp.test/realms/nirantar"


class _Key:
    def __init__(self, k: Any) -> None:
        self.key = k


class _StubJWKS:
    def __init__(self, public: Any) -> None:
        self.public = public

    def get_signing_key_from_jwt(self, token: str) -> _Key:
        return _Key(self.public)


@pytest.fixture(scope="module")
def keys() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def client(app_engine: Engine, owner_engine: Engine, keys: Any) -> TestClient:
    v = OIDCVerifier(ISS, "nirantar-api")
    v._jwks = _StubJWKS(keys.public_key())  # type: ignore[assignment]
    return TestClient(create_app(Services(engine=app_engine, owner_engine=owner_engine, extra={"oidc": v})))


def token(keys: Any, sub: str, **over: Any) -> str:
    now = int(time.time())
    claims = {"iss": ISS, "aud": ["nirantar-api", "account"], "sub": sub, "iat": now, "exp": now + 300,
              "typ": "Bearer", "email": f"{sub[:6]}@example.com", "name": "Asha Verma", **over}
    return jwt.encode(claims, keys, algorithm="RS256")


def h(tok: str, tenant: str | None = None) -> dict[str, str]:
    out = {"Authorization": f"Bearer {tok}"}
    if tenant:
        out["X-Nirantar-Tenant"] = tenant
    return out


def test_new_person_creates_a_business_and_works_in_it(client: TestClient, keys: Any) -> None:
    sub = str(uuid.uuid4())
    t = token(keys, sub)
    me = client.get("/v1/me", headers=h(t)).json()
    assert me["businesses"] == [] and me["name"] == "Asha Verma"
    r = client.get("/v1/overview", headers=h(t))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_business"
    biz = client.post("/v1/businesses", json={"name": "Chai Club", "segment": "subscription"}, headers=h(t)).json()
    assert biz["roles"] == ["owner"]
    assert client.get("/v1/overview", headers=h(t)).status_code == 200            # their only business
    second = client.post("/v1/businesses", json={"name": "Chai Club Pro"}, headers=h(t)).json()
    r = client.get("/v1/overview", headers=h(t))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "choose_business"
    assert client.get("/v1/overview", headers=h(t, second["tenant_id"])).status_code == 200
    audit = client.get("/v1/audit?limit=5", headers=h(t, biz["tenant_id"])).json()["items"]
    assert any(a["action"] == "business.created" and a["actor"] == f"user:usr_{sub}" for a in audit)


def test_tokens_are_verified_and_businesses_isolated(client: TestClient, keys: Any) -> None:
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    a_biz = client.post("/v1/businesses", json={"name": "Alice Co"}, headers=h(token(keys, alice))).json()
    client.post("/v1/businesses", json={"name": "Bob Co"}, headers=h(token(keys, bob)))
    # bob cannot act in alice's business by naming it
    assert client.get("/v1/overview", headers=h(token(keys, bob), a_biz["tenant_id"])).status_code == 403
    # wrong audience, expired, tampered, foreign key → 401
    assert client.get("/v1/me", headers=h(token(keys, alice, aud="someone-else"))).status_code == 401
    assert client.get("/v1/me", headers=h(token(keys, alice, exp=int(time.time()) - 3600))).status_code == 401
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/v1/me", headers=h(token(other, alice))).status_code == 401
    good = token(keys, alice)
    assert client.get("/v1/me", headers=h(good[:-4] + "AAAA")).status_code == 401
    assert client.get("/v1/me", headers={}).status_code == 401


def test_api_keys_still_work_alongside_people(client: TestClient, app_engine: Engine) -> None:
    demo = seed(app_engine, n_customers=3, seed_value=31)
    r = client.get("/v1/overview", headers={"Authorization": f"Bearer {demo['api_keys']['viewer']}"})
    assert r.status_code == 200
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {demo['api_keys']['owner']}"}).status_code == 401

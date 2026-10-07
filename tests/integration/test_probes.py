"""Liveness, readiness and version probes: what the platform (Docker, Hugging Face, Caddy, CI smoke) checks."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from nirantar.api.app import _migration_head, create_app
from nirantar.api.deps import Services


def test_health_ready_version(app_engine: Engine, owner_engine: Engine) -> None:
    c = TestClient(create_app(Services(engine=app_engine, owner_engine=owner_engine)))
    assert c.get("/health").json() == {"status": "ok", "db": "ok"}
    r = c.get("/ready")
    assert r.status_code == 200 and r.json()["migration"] == _migration_head() == r.json()["expected_migration"]
    v = c.get("/version").json()
    assert v["service"] == "nirantar-api" and v["migration"] == _migration_head() and "build" in v


def test_not_ready_without_a_database() -> None:
    dead = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    c = TestClient(create_app(Services(engine=dead, owner_engine=dead)))
    r = c.get("/ready")
    assert r.status_code == 503 and r.json()["ready"] is False

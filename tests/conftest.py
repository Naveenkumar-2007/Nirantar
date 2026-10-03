from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text


def _load_dotenv() -> None:
    """Load repo-root .env without overriding real environment variables."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
# Tests never talk to the real WhatsApp account (it would message real-looking numbers). Opt in explicitly for the
# live check: NIRANTAR_LIVE_WHATSAPP=1.
if os.environ.get("NIRANTAR_LIVE_WHATSAPP") != "1":
    for _k in [k for k in os.environ if k.startswith("WHATSAPP_")]:
        os.environ.pop(_k)

APP_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nirantar_app:nirantar_app@localhost:25432/nirantar"
)
OWNER_URL = os.environ.get(
    "DATABASE_OWNER_URL", "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"
)


def _reachable(url: str) -> bool:
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as c:
            c.execute(text("select 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _unavailable(reason: str) -> None:
    """Skip locally; FAIL in CI (NIRANTAR_REQUIRE_SERVICES=1) so skipped tests can't hide breakage."""
    if os.environ.get("NIRANTAR_REQUIRE_SERVICES") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def app_engine() -> Iterator[Engine]:
    if not _reachable(APP_URL):
        _unavailable("Postgres not reachable; run `docker compose up -d` and `alembic upgrade head`")
    eng = create_engine(APP_URL)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    if not _reachable(OWNER_URL):
        _unavailable("Postgres not reachable")
    eng = create_engine(OWNER_URL)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def lake(app_engine: Engine) -> Iterator[object]:
    """Iceberg lakehouse on SeaweedFS (docker compose `objectstore`) with its catalog in Postgres."""
    try:
        from nirantar.data.lake import Lake

        lk = Lake.from_env()
        lk.catalog.list_namespaces()
    except ImportError:
        _unavailable("data extra not installed: uv sync --extra data")
    except Exception as exc:  # object store or catalog DB not running
        _unavailable(f"lakehouse not reachable: {type(exc).__name__}")
    yield lk

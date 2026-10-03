"""API server process. Approved actions execute with each tenant's own provider account and the deployment's real
messaging channel (none yet → actions fail loudly), never a simulator. Run:
    uv run uvicorn nirantar.api.serve:app --port 18080
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine

from nirantar.api.app import create_app
from nirantar.api.deps import Services
from nirantar.core.dotenv import load_dotenv as _load_dotenv
from nirantar.db.session import get_engine
from nirantar.llm.gateway import LLMGateway

_load_dotenv()


def build() -> Services:
    from nirantar.approvals.executor import default_comms

    engine = get_engine()
    llm = LLMGateway.from_env() if (os.environ.get("GROQ_API_KEY") or os.environ.get("SARVAM_API_KEY")) else None
    owner = create_engine(os.environ.get("DATABASE_OWNER_URL",
                                         "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"))
    # Only tenants whose CONNECTED account is the simulator (seeded demo tenants) get one, and never in production;
    # every other tenant resolves its own real provider account.
    provider = None
    if os.environ.get("NIRANTAR_ENV", "local") != "production":
        from nirantar.payments.providers.mock import MockProvider
        from nirantar.payments.providers.resolver import ProviderResolver

        provider = ProviderResolver(engine, injected={"mock": MockProvider()})
    return Services(engine=engine, owner_engine=owner, llm=llm, provider=provider, comms=default_comms(engine))


app = create_app(build())

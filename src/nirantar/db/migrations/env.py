from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

DEFAULT_OWNER_URL = "postgresql+psycopg://nirantar_owner:nirantar_owner@localhost:25432/nirantar"


def run_migrations_online() -> None:
    url = os.environ.get("DATABASE_OWNER_URL", DEFAULT_OWNER_URL)
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()

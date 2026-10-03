"""Cross-tenant isolation enforced by Postgres itself (BB-§7), not by application code."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx

pytestmark = pytest.mark.integration


def _seed_tenant(engine: Engine, tenant_id: str) -> None:
    with tenant_tx(tenant_id, engine) as c:
        c.execute(
            text("INSERT INTO core.tenants (tenant_id, name) VALUES (:t, :n) ON CONFLICT DO NOTHING"),
            {"t": tenant_id, "n": tenant_id},
        )


@pytest.fixture
def two_tenants(app_engine: Engine) -> tuple[str, str]:
    a, b = new_id("ten"), new_id("ten")
    _seed_tenant(app_engine, a)
    _seed_tenant(app_engine, b)
    return a, b


def test_tenant_cannot_read_another_tenants_rows(app_engine: Engine, two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    cid = new_id("cus")
    with tenant_tx(a, app_engine) as c:
        c.execute(
            text("INSERT INTO billing.customers (tenant_id, customer_id, display_name) VALUES (:t, :c, 'A')"),
            {"t": a, "c": cid},
        )
    with tenant_tx(b, app_engine) as c:
        rows = c.execute(text("SELECT count(*) FROM billing.customers WHERE customer_id = :c"), {"c": cid})
        assert rows.scalar_one() == 0
        # even without a WHERE on tenant_id, B sees only its own tenant row
        tenants = c.execute(text("SELECT tenant_id FROM core.tenants")).scalars().all()
        assert tenants == [b]
    with tenant_tx(a, app_engine) as c:
        assert c.execute(
            text("SELECT count(*) FROM billing.customers WHERE customer_id = :c"), {"c": cid}
        ).scalar_one() == 1


def test_tenant_cannot_write_rows_for_another_tenant(
    app_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, b = two_tenants
    with pytest.raises(DBAPIError), tenant_tx(b, app_engine) as c:
        c.execute(
            text("INSERT INTO billing.customers (tenant_id, customer_id) VALUES (:t, :c)"),
            {"t": a, "c": new_id("cus")},
        )


def test_no_tenant_context_sees_nothing(app_engine: Engine, two_tenants: tuple[str, str]) -> None:
    with app_engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM core.tenants")).scalar_one() == 0


def test_ledger_and_audit_are_append_only_even_for_owner(
    app_engine: Engine, owner_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    a, _ = two_tenants
    with tenant_tx(a, app_engine) as c:
        c.execute(
            text(
                "INSERT INTO audit.records (tenant_id, seq, at, actor, action, data_hash, prev_hash, hash) "
                "VALUES (:t, 1, now(), 'test', 'x', 'd', 'p', 'h')"
            ),
            {"t": a},
        )
    # app role has no UPDATE/DELETE grant
    with pytest.raises(ProgrammingError), tenant_tx(a, app_engine) as c:
        c.execute(text("UPDATE audit.records SET actor = 'evil' WHERE tenant_id = :t"), {"t": a})
    # owner is blocked by the trigger
    with pytest.raises(DBAPIError), owner_engine.begin() as c:
        c.execute(text("DELETE FROM audit.records WHERE tenant_id = :t"), {"t": a})


def test_shared_regulatory_corpus_is_readable_by_every_tenant(
    owner_engine: Engine, app_engine: Engine, two_tenants: tuple[str, str]
) -> None:
    doc_id = new_id("doc")
    with owner_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO ai.documents (tenant_id, doc_id, title, document_type, version, source_uri, sha256) "
                "VALUES ('ten_global', :d, 'RBI E-mandate Framework 2026', 'regulation', 'v1', 'https://rbi.org.in', 'x')"
            ),
            {"d": doc_id},
        )
    for tenant in two_tenants:
        with tenant_tx(tenant, app_engine) as c:
            assert c.execute(
                text("SELECT count(*) FROM ai.documents WHERE doc_id = :d"), {"d": doc_id}
            ).scalar_one() == 1
            # but a tenant cannot write into the global corpus
    with pytest.raises(DBAPIError), tenant_tx(two_tenants[0], app_engine) as c:
        c.execute(
            text(
                "INSERT INTO ai.documents (tenant_id, doc_id, title, document_type, version, source_uri, sha256) "
                "VALUES ('ten_global', :d, 'x', 'x', 'v1', 'x', 'x')"
            ),
            {"d": new_id("doc")},
        )


@pytest.mark.parametrize("table", ["ingest.pipeline_runs", "ai.data_health", "ingest.extract_watermarks",
                                   "ingest.backfill_checkpoints", "config.settings_versions", "config.templates",
                                   "config.learned_params"])
def test_platform_tables_added_in_phase_2_are_tenant_isolated(app_engine: Engine, two_tenants: tuple[str, str],
                                                              table: str) -> None:
    a, b = two_tenants
    with tenant_tx(a, app_engine) as c:
        c.execute(text("INSERT INTO ingest.pipeline_runs (tenant_id, run_id, trigger, status, started_at) "
                       "VALUES (:t, 'r1', 'test', 'running', now())"), {"t": a})
        c.execute(text("INSERT INTO ai.data_health (tenant_id, computed_at, run_id, report) "
                       "VALUES (:t, now(), 'r1', '{}'::jsonb)"), {"t": a})
    with tenant_tx(b, app_engine) as c:
        assert c.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"), {"t": a}).scalar_one() == 0  # noqa: S608
    with tenant_tx(a, app_engine) as c:
        forced = c.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE oid = CAST(:t AS regclass)"),
                           {"t": table}).scalar_one()
        assert forced is True


@pytest.mark.parametrize("table", ["core.oauth_grants", "core.oauth_secrets", "events.consumer_dead_letters",
                                   "ops.a2a_tasks", "billing.mandates", "billing.disputes"])
def test_tables_added_in_p5_p7_force_rls(app_engine: Engine, table: str) -> None:
    """MCP grants/tokens, dead letters, A2A tasks, mandates and disputes are tenant data: RLS forced."""
    with app_engine.connect() as c:
        forced, enabled = c.execute(text("SELECT relforcerowsecurity, relrowsecurity FROM pg_class "
                                         "WHERE oid = CAST(:t AS regclass)"), {"t": table}).one()
    assert forced is True and enabled is True


def test_oauth_grants_are_invisible_across_tenants(app_engine: Engine, two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    client = new_id("cli")
    with app_engine.begin() as c:
        c.execute(text("INSERT INTO core.oauth_clients (client_id, info) VALUES (:c, CAST(:i AS jsonb))"),
                  {"c": client, "i": json.dumps({"client_id": client})})
    with tenant_tx(a, app_engine) as c:
        c.execute(text("INSERT INTO core.oauth_grants (tenant_id, grant_id, client_id, principal_id, scopes) "
                       "VALUES (:t, 'grt_x', :c, 'prn_x', ARRAY['nirantar:read'])"), {"t": a, "c": client})
    with tenant_tx(b, app_engine) as c:
        assert c.execute(text("SELECT count(*) FROM core.oauth_grants")).scalar_one() == 0

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.memory.service import MemoryError_, forget_subject, recall, remember

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 2, 4, tzinfo=UTC)


def test_typed_memory_supersedes_and_isolates(app_engine: Engine) -> None:
    a, b = new_id("ten"), new_id("ten")
    for t in (a, b):
        with tenant_tx(t, app_engine) as c:
            create_tenant(c, t, "x")
    with tenant_tx(a, app_engine) as c:
        remember(c, a, "cus_1", "stated_salary_day", {"day_of_month": 7}, source="customer_message",
                 confidence=0.9, provenance={"message_id": "m1"}, now=NOW)
        remember(c, a, "cus_1", "stated_salary_day", {"day_of_month": 10}, source="customer_message",
                 confidence=0.95, provenance={"message_id": "m2"}, now=NOW + timedelta(days=30))
        mem = recall(c, a, "cus_1")
        assert mem["stated_salary_day"].value == {"day_of_month": 10}
        assert mem["stated_salary_day"].provenance == {"message_id": "m2"}
        with pytest.raises(MemoryError_):
            remember(c, a, "cus_1", "favourite_colour", {"c": "red"}, source="customer_message", confidence=1,
                     provenance={}, now=NOW)                          # undeclared key
        with pytest.raises(MemoryError_):
            remember(c, a, "cus_1", "offer_grid", {"max_discount_bp": 100}, source="customer_message",
                     confidence=1, provenance={}, now=NOW)            # wrong source for key
        with pytest.raises(MemoryError_):
            remember(c, a, "cus_1", "stated_salary_day", {"day_of_month": 45}, source="customer_message",
                     confidence=1, provenance={}, now=NOW)            # schema violation
    with tenant_tx(b, app_engine) as c:
        assert recall(c, b, "cus_1") == {}                            # other tenant sees nothing
    with tenant_tx(a, app_engine) as c:
        assert forget_subject(c, a, "cus_1", NOW) == 2
        assert all(r.value == {} for r in recall(c, a, "cus_1").values())

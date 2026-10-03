from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from nirantar.approvals.service import ApprovalError, decide, redeem, request_approval
from nirantar.billing.service import create_tenant
from nirantar.core.ids import new_id
from nirantar.db.session import tenant_tx
from nirantar.security.rbac import Principal

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 5, 6, tzinfo=UTC)


def test_maker_checker_token_lifecycle(app_engine: Engine) -> None:
    t = new_id("ten")
    approver = Principal(t, "prn_fin", "user", ("finance_approver",))
    viewer = Principal(t, "prn_view", "user", ("viewer",))
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "x")
        aid = request_approval(c, t, "act_1", "gateway.create_refund", "hash-A", "agent:dispute_defender",
                               "₹8,000 refund above threshold", NOW)
        with pytest.raises(ApprovalError):
            decide(c, viewer, aid, True, NOW)                      # lacks permission
        token = decide(c, approver, aid, True, NOW)
        assert token
        with pytest.raises(ApprovalError):
            redeem(c, token, t, "gateway.create_payment_link", "hash-A", NOW)   # wrong tool
        with pytest.raises(ApprovalError):
            redeem(c, token, t, "gateway.create_refund", "hash-B", NOW)         # params changed
        with pytest.raises(ApprovalError):
            redeem(c, token, "ten_other", "gateway.create_refund", "hash-A", NOW)
        with pytest.raises(ApprovalError):
            redeem(c, token, t, "gateway.create_refund", "hash-A", NOW + timedelta(hours=1))  # expired
        assert redeem(c, token, t, "gateway.create_refund", "hash-A", NOW) == aid
        with pytest.raises(ApprovalError):
            redeem(c, token, t, "gateway.create_refund", "hash-A", NOW)         # single use
        with pytest.raises(ApprovalError):
            redeem(c, token[:-3] + "abc", t, "gateway.create_refund", "hash-A", NOW)


def test_maker_cannot_approve_own_request(app_engine: Engine) -> None:
    t = new_id("ten")
    human = Principal(t, "prn_ops", "user", ("owner",))
    with tenant_tx(t, app_engine) as c:
        create_tenant(c, t, "x")
        aid = request_approval(c, t, "act_2", "treasury.request_credit_draw", "h", "user:prn_ops", "draw", NOW)
        with pytest.raises(ApprovalError, match="maker"):
            decide(c, human, aid, True, NOW)

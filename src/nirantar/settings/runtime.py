"""Everything an agent round needs from tenant configuration, loaded once per activity in one read."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Connection

from nirantar.agents.contact_arbiter import EffectPriors
from nirantar.policy.engine import TenantPolicyConfig
from nirantar.settings import learning, service
from nirantar.settings.schema import Channels


@dataclass(frozen=True)
class TenantRuntime:
    policy: TenantPolicyConfig
    capacity: dict[str, int]
    priors: EffectPriors
    risk_threshold: float
    sources: dict[str, str]      # where each value came from (recorded in traces)


def load(conn: Connection, tenant_id: str) -> TenantRuntime:
    ch = service.model(conn, tenant_id, "channels", Channels)
    effects, eff_src = learning.effective_effects(conn, tenant_id)
    threshold, thr_src = learning.effective_threshold(conn, tenant_id)
    return TenantRuntime(
        policy=service.policy_config(conn, tenant_id), capacity=dict(ch.capacity_per_round),
        priors=EffectPriors(effects, dict(ch.cost_minor)), risk_threshold=threshold,
        sources={"channels": f"v{service.get(conn, tenant_id, 'channels').version}", "effects": eff_src,
                 "risk_threshold": thr_src, "policy": f"v{service.get(conn, tenant_id, 'policy').version}"})

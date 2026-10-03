"""Role-based access control. Roles map to explicit permissions; code checks permissions, never roles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    READ = "tenant:read"                    # dashboards, cases, payments, models
    CUSTOMERS_WRITE = "customers:write"
    SUBSCRIPTIONS_WRITE = "subscriptions:write"
    AGENTS_OPERATE = "agents:operate"       # pause/resume agents, kill switch
    APPROVALS_DECIDE = "approvals:decide"   # maker-checker approvals
    POLICY_ADMIN = "policy:admin"           # tighten tenant policy, thresholds
    INTEGRATIONS_ADMIN = "integrations:admin"
    EXPERIMENTS_ADMIN = "experiments:admin"
    AUDIT_READ = "audit:read"
    WEBHOOK_INGEST = "webhook:ingest"       # service principals only
    WORKFLOW_EXECUTE = "workflow:execute"   # workers
    A2A_CALL = "a2a:call"                   # external agents (customer agents, partners) over A2A
    TENANT_ADMIN = "tenant:admin"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({Permission.READ}),
    "ops_analyst": frozenset({Permission.READ, Permission.CUSTOMERS_WRITE, Permission.SUBSCRIPTIONS_WRITE}),
    "finance_approver": frozenset({Permission.READ, Permission.APPROVALS_DECIDE, Permission.AUDIT_READ}),
    "compliance_officer": frozenset(
        {Permission.READ, Permission.POLICY_ADMIN, Permission.AUDIT_READ, Permission.AGENTS_OPERATE}
    ),
    "owner": frozenset(set(Permission) - {Permission.WEBHOOK_INGEST, Permission.WORKFLOW_EXECUTE,
                                          Permission.A2A_CALL}),
    "service_ingress": frozenset({Permission.WEBHOOK_INGEST}),
    "service_worker": frozenset({Permission.WORKFLOW_EXECUTE, Permission.READ}),
    "a2a_partner": frozenset({Permission.A2A_CALL}),           # can only talk A2A; no dashboard/API reads
}


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    principal_id: str
    kind: str  # user | service
    roles: tuple[str, ...]

    @property
    def permissions(self) -> frozenset[Permission]:
        perms: set[Permission] = set()
        for role in self.roles:
            perms |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(perms)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


def validate_roles(roles: list[str]) -> list[str]:
    unknown = [r for r in roles if r not in ROLE_PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown roles: {unknown}")
    return roles

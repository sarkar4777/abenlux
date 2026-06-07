"""
Role-based access control. RBAC here is not decoration on top of the data - it IS the
governance model from the README, expressed as authorization so it cannot be bypassed by a
UI bug or a curious query.

The one rule the whole product hangs on: no role can see another individual's rows. A
manager, a finance lead, even an admin can only ever see k-anonymized aggregates. Individual
spend, retries, and resent-history are visible solely to the individual, via VIEW_OWN, scoped
to their OWN pseudonym (derived from their authenticated identity - you cannot pass someone
else's id). There is deliberately NO permission that grants "view another person's detail",
it doesn't exist to be mis-granted. That absence is the works-council / GDPR guarantee in code.

Roles compose permissions, permissions gate endpoints. Everything is checked server-side.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Permission(str, Enum):
    VIEW_OWN = "view_own"               # your own spend + waste + collaboration matches
    VIEW_AGGREGATES = "view_aggregates"  # k-anonymized org rollups (never individual rows)
    VIEW_COST = "view_cost"             # monetary aggregates / export (finance)
    MANAGE = "manage"                   # knowledge graph, principals, config


class Role(str, Enum):
    DEVELOPER = "developer"
    MANAGER = "manager"
    FINANCE = "finance"
    ADMIN = "admin"


# every role gets VIEW_OWN - everyone is a developer of their own data first.
_ROLE_PERMS: dict[Role, set[Permission]] = {
    Role.DEVELOPER: {Permission.VIEW_OWN},
    Role.MANAGER: {Permission.VIEW_OWN, Permission.VIEW_AGGREGATES},
    Role.FINANCE: {Permission.VIEW_OWN, Permission.VIEW_AGGREGATES, Permission.VIEW_COST},
    Role.ADMIN: {Permission.VIEW_OWN, Permission.VIEW_AGGREGATES, Permission.VIEW_COST, Permission.MANAGE},
}


def permissions_for(role: Role) -> set[Permission]:
    return set(_ROLE_PERMS.get(role, set()))


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. `subject` is the raw identity (SSO id / username), `pseudonym`
    is its HMAC, the only id that appears in stored data. The principal can read its OWN rows by
    pseudonym and nothing else at the individual level."""

    subject: str
    display_name: str
    role: Role
    pseudonym: str

    @property
    def permissions(self) -> set[Permission]:
        return permissions_for(self.role)

    def can(self, perm: Permission) -> bool:
        return perm in self.permissions


class AuthorizationError(Exception):
    """raised when a principal lacks a required permission. maps to HTTP 403."""


def require(principal: Principal, perm: Permission) -> None:
    if not principal.can(perm):
        raise AuthorizationError(f"{principal.role.value} lacks {perm.value}")

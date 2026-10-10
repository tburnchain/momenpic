"""RBAC (§10): owner / operator / reviewer / viewer.

Publishing approval and rule changes are owner·reviewer only. LLM agents are *not* principals at
all — they have no route to these permissions (§5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from acvm.core.enums import Role
from acvm.core.errors import PolicyViolation


class Perm(StrEnum):
    VIEW = "view"
    COMMAND = "command"  # submit natural-language commands, create plans
    APPROVE_PLAN = "approve_plan"  # console approval of a plan
    REVIEW_HOLD = "review_hold"  # approve/reject HOLD items (= publishing approval)
    CHANGE_RULES = "change_rules"  # gate rule set, banned-word dictionary
    MANAGE_ACCOUNTS = "manage_accounts"
    MANAGE_ASSETS = "manage_assets"
    ADMIN = "admin"


GRANTS: dict[Role, frozenset[Perm]] = {
    Role.OWNER: frozenset(Perm),
    Role.REVIEWER: frozenset({Perm.VIEW, Perm.REVIEW_HOLD, Perm.CHANGE_RULES, Perm.APPROVE_PLAN}),
    Role.OPERATOR: frozenset({Perm.VIEW, Perm.COMMAND, Perm.MANAGE_ASSETS}),
    Role.VIEWER: frozenset({Perm.VIEW}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str  # email or service name
    role: Role
    workspace_id: str | None = None
    user_id: str | None = None  # app_user.id for logged-in members
    name: str = ""
    auth: str = "dev"  # dev | session | service | cloudflare_access

    def can(self, perm: Perm) -> bool:
        return perm in GRANTS[self.role]

    def require(self, perm: Perm) -> None:
        if not self.can(perm):
            raise PolicyViolation(f"{self.subject} ({self.role}) lacks {perm}", perm=str(perm))

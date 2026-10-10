"""Audit trail (§10): every publish, approval and rule change is recorded (1-year retention)."""

from __future__ import annotations

from typing import Any

from acvm.core.clock import Clock
from acvm.db.repos import Repos
from acvm.domain.models import AuditEntry


async def audit(repos: Repos, clock: Clock, actor: str, action: str, target: str,
                **payload: Any) -> None:
    await repos.audit.write(AuditEntry(actor=actor, action=action, target=target,
                                       payload={k: _jsonable(v) for k, v in payload.items()}, at=clock.now()))


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    return str(v)

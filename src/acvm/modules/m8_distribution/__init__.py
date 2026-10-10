"""M8 Distribution (§4 M8): platform caption variants, quota-aware scheduling, the publication state machine,
official-API publishing through ``acvm.infra.platforms`` and the Kakao manual one-click queue."""

from acvm.modules.m8_distribution.captions import compose_caption, compose_title, resolve_placement
from acvm.modules.m8_distribution.quota import QuotaGate, effective_daily_cap
from acvm.modules.m8_distribution.service import DistributionService
from acvm.modules.m8_distribution.state import ALLOWED_TRANSITIONS, IllegalTransition, can_transition, ensure_transition

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DistributionService",
    "IllegalTransition",
    "QuotaGate",
    "can_transition",
    "compose_caption",
    "compose_title",
    "effective_daily_cap",
    "ensure_transition",
    "resolve_placement",
]

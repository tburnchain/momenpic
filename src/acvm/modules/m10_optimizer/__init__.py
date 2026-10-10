"""M10 학습·최적화 (Optimizer), design §4 M10: Thompson-sampling bandit, amplification, retirement,
G3 platform admission and the playbook KB."""

from acvm.modules.m10_optimizer.optimizer import (
    PERSONAS,
    TEMPLATES,
    Optimizer,
    PerfOutcome,
    Recommendation,
    arm_key_for,
)
from acvm.modules.m10_optimizer.posterior import Observation, apply_delta

__all__ = [
    "PERSONAS",
    "TEMPLATES",
    "Observation",
    "Optimizer",
    "PerfOutcome",
    "Recommendation",
    "apply_delta",
    "arm_key_for",
]

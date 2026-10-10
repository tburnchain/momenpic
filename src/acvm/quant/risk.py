"""Account-health decisions (§7.1 #3: failure rate > 10 %, reach −60 %, warning → 48 h cooldown).

A raw "failure rate > 10 %" rule fires on 1 failure in 3 attempts (33 %) and ignores 9 in 100 —
both wrong. We instead ask the Bayesian question the rule is really asking:

    P(true failure rate > 10 % | data) > confidence      with prior Beta(1, 9)  (mean 10 %)

so a cooldown needs evidence, and the evidence threshold is explicit (default 80 %).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from acvm.quant.anomaly import reach_drop
from acvm.quant.bayes import BetaPosterior


@dataclass(frozen=True, slots=True)
class HealthDecision:
    cooldown: bool
    reasons: tuple[str, ...]
    p_failure_exceeds: float
    reach_change: float


def assess_account(*, attempts: int, failures: int, current_reach: float | None,
                   trailing_reach: Sequence[float], warnings: int, threshold: float = 0.10,
                   confidence: float = 0.80, reach_threshold: float = -0.60,
                   prior: tuple[float, float] = (1.0, 9.0), min_reach_history: int = 3) -> HealthDecision:
    post = BetaPosterior(*prior).update(failures, max(attempts - failures, 0))
    p_exceeds = post.prob_greater_than(threshold)
    reasons: list[str] = []
    if attempts > 0 and p_exceeds > confidence:
        reasons.append(f"failure-rate P(f>{threshold:.0%})={p_exceeds:.2f} ({failures}/{attempts})")
    change = 0.0
    if current_reach is not None and len(trailing_reach) >= min_reach_history:
        change = reach_drop(current_reach, trailing_reach)
        if change <= reach_threshold:
            reasons.append(f"reach {change:+.0%} vs 7-day median")
    if warnings > 0:
        reasons.append(f"{warnings} platform warning response(s)")
    return HealthDecision(bool(reasons), tuple(reasons), p_exceeds, change)

"""Pure, clock-explicit rate-limit algorithms. The Redis implementations in
`acvm.infra.quota` run the *same* arithmetic in Lua so tests of these functions certify production.

* ``TokenBucket``      — continuous refill; used for call budgets (Coupang search 8/h, §7).
* ``SlidingWindowLog`` — exact rolling-window counter; platform limits are "24-hour moving
  window" (Threads/Instagram, §7), so a fixed daily reset would under- or over-count.
* ``CircuitBreaker``   — closed → open → half-open; Coupang 403 policy (§9.5 #2): stop at once,
  wait 1 h, then allow exactly one probe call.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from enum import StrEnum


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    tokens: float = -1.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            self.tokens = self.capacity

    def _refill(self, now: float) -> None:
        if now > self.updated_at:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.refill_per_second)
            self.updated_at = now

    def try_consume(self, now: float, n: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens + 1e-9 >= n:
            self.tokens -= n
            return True
        return False

    def wait_time(self, now: float, n: float = 1.0) -> float:
        self._refill(now)
        deficit = n - self.tokens
        if deficit <= 0:
            return 0.0
        return math.inf if self.refill_per_second <= 0 else deficit / self.refill_per_second


@dataclass
class SlidingWindowLog:
    limit: int
    window_seconds: float
    events: list[float] = field(default_factory=list)  # sorted timestamps

    def _evict(self, now: float) -> None:
        cut = bisect.bisect_right(self.events, now - self.window_seconds)
        if cut:
            del self.events[:cut]

    def count(self, now: float) -> int:
        self._evict(now)
        return len(self.events)

    def allow(self, now: float) -> bool:
        return self.count(now) < self.limit

    def record(self, now: float) -> None:
        bisect.insort(self.events, now)

    def try_acquire(self, now: float) -> bool:
        if self.allow(now):
            self.record(now)
            return True
        return False

    def next_available(self, now: float) -> float:
        """Earliest time ≥ now at which one more event fits."""
        self._evict(now)
        if len(self.events) < self.limit:
            return now
        # the (len − limit + 1)-th oldest event must leave the window
        return self.events[len(self.events) - self.limit] + self.window_seconds


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 1
    open_seconds: float = 3600.0
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False

    def allow(self, now: float) -> bool:
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN and now - self.opened_at >= self.open_seconds:
            self.state = BreakerState.HALF_OPEN
            self.probe_in_flight = False
        if self.state is BreakerState.HALF_OPEN and not self.probe_in_flight:
            self.probe_in_flight = True  # exactly one probe ("1시간 후 단건 재시도")
            return True
        return False

    def retry_after(self, now: float) -> float:
        if self.state is BreakerState.OPEN:
            return max(0.0, self.open_seconds - (now - self.opened_at))
        return 0.0 if self.state is BreakerState.CLOSED else self.open_seconds

    def record_success(self) -> None:
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.probe_in_flight = False

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.state is BreakerState.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self.opened_at = now
            self.probe_in_flight = False


def operating_cap(api_limit_24h: int, design_cap: int, max_fraction: float = 0.30) -> int:
    """§7.1 double enforcement: the effective cap never exceeds max_fraction of the API limit."""
    return max(0, min(design_cap, math.floor(api_limit_24h * max_fraction)))

"""Publish-time slots (§4 M8 시간대 분산: window 09–23 KST, ±7-minute jitter, top-of-hour avoidance,
20-minute minimum gap; §7 daily operating caps; §7.1 #4 no simultaneous posting of one content).

Each account gets a deterministic *daily grid*: ``spread_schedule(cap, …)`` over that day's window with an RNG
seeded by ``(account, day)``, i.e. ``cap`` stratified-jittered times evenly covering 09:00–23:00 KST. A new
publication takes the earliest grid slot that is ≥ ``not_before`` and at least ``min_gap`` away from every
time already booked on the account (and from the same content's other targets). When the account's cap for
the day is full, or the remaining grid slots are taken/past, planning rolls over to the next day. Deferred
publications are pushed back into the window with :func:`fit_to_window`.

Booking from a fixed grid (rather than "as soon as possible") is what keeps a 5/day Instagram account at
~2.8-hour intervals instead of five posts in the first hour — the clumping that makes automation look like
automation (§7: "진짜 병목은 스팸 판정").
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta

from acvm.config import QuotaPolicy
from acvm.core.clock import KST
from acvm.quant.scheduling import avoid_top_of_hour, spread_schedule


def kst_day_bounds(at: datetime) -> tuple[datetime, datetime]:
    """[00:00, 24:00) KST of the day containing ``at``, as UTC datetimes."""
    local = at.astimezone(KST)
    start = datetime.combine(local.date(), time(0, 0), KST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def window_bounds(at: datetime, policy: QuotaPolicy) -> tuple[datetime, datetime]:
    local = at.astimezone(KST)
    start = datetime.combine(local.date(), time(policy.window_start_hour, 0), KST)
    end = (datetime.combine(local.date(), time(policy.window_end_hour, 0), KST) if policy.window_end_hour < 24
           else datetime.combine(local.date() + timedelta(days=1), time(0, 0), KST))
    return start.astimezone(UTC), end.astimezone(UTC)


def day_grid(day: datetime, cap: int, policy: QuotaPolicy, rng: random.Random) -> list[datetime]:
    """``cap`` deterministic slots spread across the day's posting window (UTC, sorted)."""
    if cap <= 0:
        return []
    w_start, _ = window_bounds(day, policy)
    return spread_schedule(cap, not_before=w_start, start_hour=policy.window_start_hour,
                           end_hour=policy.window_end_hour, jitter_minutes=policy.jitter_minutes,
                           avoid_minutes=policy.avoid_top_of_hour_minutes, min_gap_minutes=policy.min_gap_minutes,
                           rng=rng, max_days=1)


def first_free(grid: Sequence[datetime], occupied: Sequence[datetime], not_before: datetime,
               gap: timedelta) -> datetime | None:
    """Earliest grid slot ≥ not_before that keeps ``gap`` to every occupied time."""
    for slot in sorted(grid):
        if slot < not_before:
            continue
        if all(abs((slot - b).total_seconds()) >= gap.total_seconds() for b in occupied):
            return slot
    return None


def fit_to_window(at: datetime, policy: QuotaPolicy, rng: random.Random, *, jitter: bool = True) -> datetime:
    """Shift a deferral target (``at`` + U(0, jitter)) into the posting window, off the top of the hour."""
    t = at + timedelta(minutes=rng.uniform(0, policy.jitter_minutes)) if jitter else at
    w_start, w_end = window_bounds(t, policy)
    if t < w_start:
        t = w_start + timedelta(minutes=rng.uniform(0, policy.jitter_minutes))
    elif t >= w_end:
        next_start, _ = window_bounds(t + timedelta(days=1), policy)
        t = next_start + timedelta(minutes=rng.uniform(0, policy.jitter_minutes))
    return avoid_top_of_hour(t, policy.avoid_top_of_hour_minutes).astimezone(UTC)

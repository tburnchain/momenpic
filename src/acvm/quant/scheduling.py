"""Publish-time planning (§4 M8: time-of-day spread, top-of-hour avoidance, ±7 min jitter).

Stratified jitter: split the window into n equal strata and place one post per stratum at the
stratum centre plus U(−j, +j). Compared with i.i.d. uniform placement this keeps the expected gap
at W/n while preventing the clumping that makes automated accounts look automated — and unlike a
fixed grid it never lands on the same minute two days running.

Constraints applied in order: (1) window clamp, (2) top-of-hour exclusion (no posts in
[hh:60−k, hh:00+k]), (3) minimum gap against already-booked slots and each other.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _window_bounds(day: datetime, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    local = day.astimezone(KST)
    start = datetime.combine(local.date(), time(start_hour, 0), KST)
    end = datetime.combine(local.date(), time(end_hour, 0), KST) if end_hour < 24 else \
        datetime.combine(local.date() + timedelta(days=1), time(0, 0), KST)
    return start, end


def avoid_top_of_hour(t: datetime, k_minutes: int) -> datetime:
    """Push t out of the ±k-minute band around every full hour (in KST)."""
    if k_minutes <= 0:
        return t
    local = t.astimezone(KST)
    m = local.minute + local.second / 60
    if m < k_minutes:
        return t + timedelta(minutes=k_minutes - m)
    if m > 60 - k_minutes:
        return t - timedelta(minutes=m - (60 - k_minutes))
    return t


def spread_schedule(n: int, *, not_before: datetime, start_hour: int = 9, end_hour: int = 23,
                    jitter_minutes: int = 7, avoid_minutes: int = 3, min_gap_minutes: int = 20,
                    booked: Sequence[datetime] = (), rng: random.Random | None = None,
                    max_days: int = 14) -> list[datetime]:
    """Return n UTC-aware publish times ≥ not_before, spread over successive daily windows.

    Capacity per day is ⌊window / min_gap⌋ minus already-booked slots; overflow rolls to the next
    day — which is exactly the behaviour an account's daily cap needs.
    """
    if n <= 0:
        return []
    rng = rng or random.Random()
    gap = timedelta(minutes=min_gap_minutes)
    taken = sorted(booked)
    out: list[datetime] = []
    day = not_before
    for _ in range(max_days):
        if len(out) >= n:
            break
        w_start, w_end = _window_bounds(day, start_hour, end_hour)
        lo = max(w_start, not_before.astimezone(KST))
        if lo >= w_end:
            day = w_start + timedelta(days=1)
            continue
        span = (w_end - lo).total_seconds()
        capacity = max(1, int(span // gap.total_seconds()))
        booked_today = [b for b in taken if lo <= b.astimezone(KST) < w_end]
        k = min(n - len(out), max(0, capacity - len(booked_today)))
        if k > 0:
            stratum = span / k
            placed: list[datetime] = []
            for i in range(k):
                centre = lo + timedelta(seconds=stratum * (i + 0.5))
                t = centre + timedelta(minutes=rng.uniform(-jitter_minutes, jitter_minutes))
                t = min(max(t, lo), w_end - timedelta(minutes=1))
                t = avoid_top_of_hour(t, avoid_minutes)
                placed.append(t)
            placed = _enforce_gap(sorted(placed), taken, gap, lo, w_end, avoid_minutes)
            out.extend(placed)
            taken = sorted(taken + placed)
        day = w_start + timedelta(days=1)
    return sorted(t.astimezone(UTC) for t in out[:n])


def _enforce_gap(slots: list[datetime], booked: Sequence[datetime], gap: timedelta, lo: datetime,
                 hi: datetime, avoid_minutes: int) -> list[datetime]:
    result: list[datetime] = []
    for t in slots:
        cand = t
        for _ in range(64):  # bounded search forward in gap-sized steps
            conflicts = [b for b in list(booked) + result if abs((b - cand).total_seconds()) < gap.total_seconds()]
            if not conflicts:
                break
            cand = avoid_top_of_hour(max(conflicts) + gap, avoid_minutes)
            if cand >= hi:
                cand = None  # type: ignore[assignment]
                break
        if cand is not None and lo <= cand < hi:
            result.append(cand)
    return result


def time_slot(t: datetime) -> str:
    """Bandit context bucket for the posting hour (KST)."""
    h = t.astimezone(KST).hour
    if 6 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "lunch"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "night"

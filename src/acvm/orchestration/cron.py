"""Minimal 5-field cron (minute hour day-of-month month day-of-week) evaluated in KST — enough for
recurring commands like "매일 오전 9시에 차량용품 10개" (§5.2 예약 실행)."""

from __future__ import annotations

from datetime import datetime, timedelta

from acvm.core.clock import KST

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron field out of range: {spec}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("cron needs 5 fields")
        self.expr = expr
        self.minute, self.hour, self.dom, self.month, dow = (_field(p, *r) for p, r in zip(parts, _RANGES, strict=True))
        self.dow = {d % 7 for d in dow}
        self._dom_star, self._dow_star = parts[2] == "*", parts[4] == "*"

    def matches(self, when: datetime) -> bool:
        t = when.astimezone(KST)
        cron_dow = (t.weekday() + 1) % 7  # cron: 0 = Sunday
        day_ok = (t.day in self.dom) if self._dow_star else (cron_dow in self.dow) if self._dom_star else \
            (t.day in self.dom or cron_dow in self.dow)
        return t.minute in self.minute and t.hour in self.hour and t.month in self.month and day_ok

    def next_after(self, when: datetime) -> datetime:
        t = when.astimezone(KST).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 366):
            if self.matches(t):
                return t
            t += timedelta(minutes=1)
        raise ValueError(f"cron {self.expr} never fires")

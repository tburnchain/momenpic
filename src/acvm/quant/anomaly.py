"""Invalid-click and reach anomaly detection (§4 M9, §7.1 #3).

* ``ctr_spike_pvalue`` — under the link's historical CTR posterior Beta(α, β), today's clicks
  follow a Beta-Binomial predictive. The p-value P(K ≥ k) is exact (no normal approximation, which
  badly understates tail risk at CTR ≈ 1 %). Default alarm at p < 1e-4 (≈ one false alarm per
  10 000 link-days).
* ``repeated_click_bursts`` — same hashed IP clicking the same link more than ``max_clicks`` times
  within ``window_seconds`` (the hub redirect logs these; raw IPs are never stored).
* ``reach_drop`` — current reach vs. trailing 7-day median; ≤ −60 % triggers cooldown (§7.1 #3).
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from acvm.quant.distributions import median
from acvm.quant.special import beta_binom_sf


def ctr_spike_pvalue(clicks: int, impressions: int, prior_alpha: float, prior_beta: float) -> float:
    if impressions <= 0 or clicks <= 0:
        return 1.0
    clicks = min(clicks, impressions)
    return beta_binom_sf(clicks, impressions, prior_alpha, prior_beta)


@dataclass(frozen=True, slots=True)
class ClickBurst:
    ip_hash: str
    link_key: str
    clicks: int
    first_ts: float
    last_ts: float


def repeated_click_bursts(events: Iterable[tuple[float, str, str]], *, window_seconds: float = 60.0,
                          max_clicks: int = 3) -> list[ClickBurst]:
    """events: (unix_ts, ip_hash, link_key), any order. Returns one burst per (ip, link) maximal run."""
    by_pair: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts, ip, link in events:
        by_pair[(ip, link)].append(ts)
    bursts: list[ClickBurst] = []
    for (ip, link), stamps in by_pair.items():
        stamps.sort()
        win: deque[float] = deque()
        peak, first, last = 0, 0.0, 0.0
        for ts in stamps:
            win.append(ts)
            while win and ts - win[0] > window_seconds:
                win.popleft()
            if len(win) > peak:
                peak, first, last = len(win), win[0], ts
        if peak > max_clicks:
            bursts.append(ClickBurst(ip, link, peak, first, last))
    return bursts


def reach_drop(current: float, trailing: Sequence[float]) -> float:
    """Relative change vs. the trailing median: (current − med) / med. 0 when no history."""
    med = median(list(trailing))
    if med <= 0:
        return 0.0
    return (current - med) / med

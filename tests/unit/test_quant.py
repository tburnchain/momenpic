"""The design document's numbers, verified. If a refactor changes any of these the build fails."""

from __future__ import annotations

import itertools
import math
import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from acvm.quant.anomaly import ctr_spike_pvalue, reach_drop, repeated_click_bursts
from acvm.quant.bayes import (
    ArmCandidate,
    BetaPosterior,
    GammaPosterior,
    RevenuePosterior,
    fit_beta_prior,
    shrink_epc,
    thompson_allocate,
)
from acvm.quant.distributions import LogNormal, percentile_rank, wilson_interval
from acvm.quant.economics import (
    Funnel,
    MoneyView,
    breakeven_impressions,
    channel_admission,
    content_economics,
    prob_at_least_one_tail,
    trials_for_tail,
)
from acvm.quant.features import price_affinity, saturating_log, trend_momentum
from acvm.quant.ratelimit import CircuitBreaker, SlidingWindowLog, TokenBucket, operating_cap
from acvm.quant.risk import assess_account
from acvm.quant.scheduling import KST, spread_schedule
from acvm.quant.similarity import (
    HashingEmbedder,
    LSHIndex,
    MinHasher,
    char_ngrams,
    cosine,
    jaccard,
    ngram_overlap,
    optimal_bands,
)
from acvm.quant.special import beta_binom_sf, beta_ppf, betainc, gammainc_lower

DESIGN_FUNNEL = Funnel(ctr=0.008, cvr=0.03, aov_krw=30_000, commission_rate=0.03)


# ───────── §2 revenue engine ─────────


def test_breakeven_matches_design_section_2_5() -> None:
    assert DESIGN_FUNNEL.commission_per_order == pytest.approx(900)  # "주문 1건당 수수료 900원"
    i_star = breakeven_impressions(150, DESIGN_FUNNEL)
    assert i_star == pytest.approx(694.44, rel=1e-4)  # "손익분기 조회수 I* 약 700회"


def test_lognormal_mean_is_7_4x_median_at_sigma_2() -> None:
    d = LogNormal.from_median(1_000, 2.0)
    assert d.mean_to_median == pytest.approx(math.e**2)  # 7.389 — "약 7.4배"
    assert d.mean == pytest.approx(7_389.06, rel=1e-5)


def test_tail_probability_claims_section_2_2() -> None:
    assert prob_at_least_one_tail(0.01, 100) == pytest.approx(0.634, abs=1e-3)  # "63%"
    assert prob_at_least_one_tail(0.01, 300) == pytest.approx(0.951, abs=1e-3)  # "95%"
    assert trials_for_tail(0.01, 0.95) == 299


def test_top_share_quantifies_heavy_tail() -> None:
    d = LogNormal(0.0, 2.0)
    assert d.top_share(0.01) == pytest.approx(0.372, abs=2e-3)
    assert d.top_share(0.05) == pytest.approx(0.639, abs=2e-3)
    # Monte-Carlo cross-check of the closed form
    rng = random.Random(7)
    xs = sorted((rng.lognormvariate(0, 2) for _ in range(200_000)), reverse=True)
    mc = sum(xs[:10_000]) / sum(xs)
    assert mc == pytest.approx(d.top_share(0.05), abs=0.03)


def test_conditional_mean_and_exceedance() -> None:
    d = LogNormal.from_median(500, 1.5)
    rng = random.Random(3)
    xs = [rng.lognormvariate(d.mu, d.sigma) for _ in range(200_000)]
    above = [x for x in xs if x > 700]
    assert len(above) / len(xs) == pytest.approx(d.sf(700), abs=0.01)
    assert sum(above) / len(above) == pytest.approx(d.conditional_mean_above(700), rel=0.05)


def test_content_economics_profit_sign() -> None:
    ce = content_economics(LogNormal.from_median(300, 2.0), DESIGN_FUNNEL, 150)
    # median below I* but mean (≈2 217) above → positive expected profit carried by the tail
    assert ce.expected_impressions > ce.breakeven_views > 300
    assert ce.expected_profit > 0
    assert 0 < ce.p_breakeven < 0.5


def test_channel_admission_g3() -> None:
    rng = random.Random(11)
    good = [rng.lognormvariate(math.log(1_500), 1.8) for _ in range(60)]
    bad = [rng.lognormvariate(math.log(250), 1.8) for _ in range(60)]
    assert channel_admission(good, DESIGN_FUNNEL, 150).admitted
    assert not channel_admission(bad, DESIGN_FUNNEL, 150).admitted
    assert not channel_admission(good[:5], DESIGN_FUNNEL, 150).admitted  # insufficient data


def test_money_view_never_conflates() -> None:
    m = MoneyView(gmv_krw=40_000_000_000, commission_krw=1_200_000_000, cancelled_krw=60_000_000,
                  cost_krw=100_000_000)
    assert m.effective_rate == pytest.approx(0.0285)  # "400억 × 3% ≈ 12억" minus cancellations
    assert m.net_profit_krw == 1_040_000_000


# ───────── special functions ─────────


def _binomial_upper(a: int, b: int, x: float) -> float:
    """Exact identity for integer a, b: I_x(a, b) = P(Binomial(a+b−1, x) ≥ a)."""
    n = a + b - 1
    return sum(math.comb(n, k) * x**k * (1 - x) ** (n - k) for k in range(a, n + 1))


@pytest.mark.parametrize(("a", "b", "x"), [(1, 1, 0.3), (2, 3, 0.4), (10, 90, 0.1), (3, 200, 0.02)])
def test_betainc_matches_binomial_identity(a: int, b: int, x: float) -> None:
    assert betainc(a, b, x) == pytest.approx(_binomial_upper(a, b, x), abs=1e-12)


def test_betainc_arcsine_symmetry() -> None:
    assert betainc(0.5, 0.5, 0.5) == pytest.approx(0.5, abs=1e-12)


@given(st.floats(0.5, 50), st.floats(0.5, 50), st.floats(0.01, 0.99))
@settings(max_examples=60, deadline=None)
def test_beta_ppf_inverts_cdf(a: float, b: float, q: float) -> None:
    assert betainc(a, b, beta_ppf(q, a, b)) == pytest.approx(q, abs=1e-6)


def test_gammainc_against_exponential() -> None:
    assert gammainc_lower(1.0, 2.0) == pytest.approx(1 - math.exp(-2.0), abs=1e-10)


def test_beta_binom_sf_monotone_and_bounded() -> None:
    vals = [beta_binom_sf(k, 1000, 8, 992) for k in range(0, 40, 4)]
    assert vals[0] == 1.0
    assert all(x >= y for x, y in itertools.pairwise(vals))
    assert vals[-1] < 1e-3


# ───────── Bayesian layer ─────────


def test_epc_shrinkage_resists_lucky_newcomer() -> None:
    lucky = shrink_epc(clicks=3, revenue_krw=2_700, prior_epc=27, kappa=50)
    proven = shrink_epc(clicks=2_000, revenue_krw=80_000, prior_epc=27, kappa=50)
    assert lucky < 80  # raw EPC 900 would have ranked it first
    assert proven == pytest.approx(39.68, abs=0.01)


def test_beta_and_gamma_conjugacy() -> None:
    b = BetaPosterior(1, 1).update(8, 992)
    assert b.mean == pytest.approx(9 / 1002)
    g = GammaPosterior(1, 1).update(30, 1_000)
    assert g.mean == pytest.approx(31 / 1001)
    lo, hi = b.interval(0.9)
    assert lo < b.mean < hi


def test_fit_beta_prior_recovers_mean() -> None:
    rng = random.Random(5)
    rates = [rng.betavariate(8, 992) for _ in range(500)]
    prior = fit_beta_prior(rates)
    assert prior.mean == pytest.approx(0.008, rel=0.1)


def test_revenue_posterior_prior_matches_funnel() -> None:
    post = RevenuePosterior.prior(0.008, 0.03, 900, strength=5)
    assert post.mean_rpi == pytest.approx(DESIGN_FUNNEL.revenue_per_impression, rel=0.01)
    assert post.mean_epc == pytest.approx(DESIGN_FUNNEL.epc, rel=0.01)


def test_thompson_concentrates_on_best_arm_and_respects_budget() -> None:
    rng = random.Random(42)
    base = RevenuePosterior.prior(0.008, 0.03, 900, strength=1)
    good = base
    bad = base
    for _ in range(40):
        good = good.observe(1000, 15, 0.6, 540)
        bad = bad.observe(1000, 4, 0.08, 72)
    young = base
    arms = [ArmCandidate("good", good), ArmCandidate("bad", bad), ArmCandidate("young", young),
            ArmCandidate("dead", good, retired=True)]
    alloc = thompson_allocate(arms, 100, rng, exploration_budget=0.2)
    explore = [a for a in alloc if a.mode == "explore"]
    exploit = [a for a in alloc if a.mode == "exploit"]
    assert len(explore) == 20 and all(a.key == "young" for a in explore)
    assert sum(a.key == "good" for a in exploit) > 60
    assert all(a.key != "dead" for a in alloc)


# ───────── similarity ─────────


def test_ngram_overlap_detects_copy() -> None:
    ref = "이 클립 하나면 과자 봉지가 눅눅해질 걱정이 사라집니다 진짜 신세계"
    copy = "이 클립 하나면 과자 봉지가 눅눅해질 걱정이 사라져요"
    fresh = "주방 서랍이 매일 엉망이라면 이 정리함부터 보세요"
    assert ngram_overlap(copy, ref) > 0.15
    assert ngram_overlap(fresh, ref) < 0.05


def test_minhash_estimates_jaccard() -> None:
    mh = MinHasher(num_perm=256, shingle=4)
    a = "오늘은 주방 정리 꿀템을 소개해요 서랍 속 숟가락이 한눈에 보이는 정리함입니다"
    b = "오늘은 주방 정리 꿀템을 소개해요 서랍 속 젓가락이 한눈에 보이는 수납함입니다"
    true_j = jaccard(char_ngrams(a, 4), char_ngrams(b, 4))
    assert mh.similarity(a, b) == pytest.approx(true_j, abs=0.08)
    assert mh.similarity(a, a) == 1.0


def test_lsh_finds_near_duplicates_only() -> None:
    idx = LSHIndex(threshold=0.6)
    base = "차량용 틈새 수납함 하나로 운전석 옆 공간이 깔끔해졌어요 동전 영수증 다 들어가요"
    idx.add("dup-src", base)
    idx.add("other", "아이 장난감 정리는 투명 수납박스가 정답 뚜껑까지 있어서 먼지 걱정 없음")
    hits = idx.query(base + " 진짜로")
    assert hits and hits[0][0] == "dup-src"
    assert not idx.query("완전히 다른 문장으로 작성된 새로운 대본입니다 겹치는 표현이 없어요")


def test_optimal_bands_threshold_close_to_target() -> None:
    b, r = optimal_bands(0.6, 128, fp_weight=0.25)
    assert b * r <= 128
    assert 0.35 < (1 / b) ** (1 / r) < 0.75


def test_hashing_embedder_cosine_ordering() -> None:
    e = HashingEmbedder()
    a = e.embed("과자 밀봉 클립으로 눅눅함 끝")
    b = e.embed("과자 밀봉 클립 하나로 눅눅함 해결")
    c = e.embed("자동차 타이어 공기압 체크 방법")
    assert cosine(a, b) > cosine(a, c)
    assert len(a) == 1024


# ───────── rate limits & scheduling ─────────


def test_token_bucket_8_per_hour() -> None:
    tb = TokenBucket(capacity=8, refill_per_second=8 / 3600)
    assert all(tb.try_consume(0) for _ in range(8))
    assert not tb.try_consume(0)
    assert tb.wait_time(0) == pytest.approx(450)
    assert tb.try_consume(450)


def test_sliding_window_rolls() -> None:
    sw = SlidingWindowLog(limit=3, window_seconds=86_400)
    for t in (0, 100, 200):
        assert sw.try_acquire(t)
    assert not sw.try_acquire(300)
    assert sw.next_available(300) == 86_400
    assert sw.try_acquire(86_401)


def test_breaker_single_probe_after_hour() -> None:
    br = CircuitBreaker(failure_threshold=1, open_seconds=3600)
    br.record_failure(0)
    assert not br.allow(10)
    assert br.allow(3600)  # one probe
    assert not br.allow(3601)  # no second concurrent probe
    br.record_failure(3602)
    assert not br.allow(3700)


def test_operating_cap_never_exceeds_30_percent() -> None:
    assert operating_cap(250, 20) == 20
    assert operating_cap(15, 10) == 4
    assert operating_cap(100, 5) == 5


def test_spread_schedule_constraints() -> None:
    start = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)  # 09:00 KST
    slots = spread_schedule(12, not_before=start, rng=random.Random(1), min_gap_minutes=20)
    assert len(slots) == 12
    for s in slots:
        local = s.astimezone(KST)
        assert 9 <= local.hour < 23
        assert 3 <= local.minute <= 57
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(slots)]
    assert min(gaps) >= 20 * 60 - 1


def test_spread_schedule_rolls_to_next_day_when_full() -> None:
    start = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)  # 22:00 KST — 1 h left
    slots = spread_schedule(5, not_before=start, rng=random.Random(2), min_gap_minutes=20)
    assert len(slots) == 5
    assert slots[-1] - start > timedelta(hours=8)


# ───────── anomaly & risk ─────────


def test_ctr_spike_detection() -> None:
    assert ctr_spike_pvalue(9, 1_000, 8, 992) > 0.1
    assert ctr_spike_pvalue(60, 1_000, 8, 992) < 1e-10


def test_click_bursts() -> None:
    events = [(float(t), "ip1", "L1") for t in range(0, 50, 10)] + [(0.0, "ip2", "L1"), (500.0, "ip2", "L1")]
    bursts = repeated_click_bursts(events, window_seconds=60, max_clicks=3)
    assert len(bursts) == 1 and bursts[0].ip_hash == "ip1" and bursts[0].clicks == 5


def test_cooldown_requires_evidence() -> None:
    assert not assess_account(attempts=3, failures=1, current_reach=None, trailing_reach=[],
                              warnings=0).cooldown
    assert assess_account(attempts=40, failures=10, current_reach=None, trailing_reach=[],
                          warnings=0).cooldown
    assert assess_account(attempts=10, failures=0, current_reach=300,
                          trailing_reach=[1000, 1100, 900, 1000], warnings=0).cooldown
    assert assess_account(attempts=0, failures=0, current_reach=None, trailing_reach=[],
                          warnings=1).cooldown
    assert reach_drop(400, [1000] * 7) == pytest.approx(-0.6)


# ───────── features ─────────


def test_price_affinity_band() -> None:
    assert price_affinity(25_000) == 1.0
    assert price_affinity(5_000) == pytest.approx(0.51, abs=0.02)
    assert price_affinity(200_000) < price_affinity(80_000) < 1.0
    assert saturating_log(10_000, 10_000) == 1.0
    assert trend_momentum(0.0) == 0.5


def test_percentile_and_wilson() -> None:
    assert percentile_rank(5, [1, 2, 3, 4, 5]) == pytest.approx(0.9)
    lo, hi = wilson_interval(10, 100)
    assert lo < 0.1 < hi


def test_revenue_prior_strength_is_in_contents() -> None:
    """κ = 20 contents ⇒ the CTR Beta carries 20 × 1 000 pseudo-impressions (regression: was ≈ 162)."""
    post = RevenuePosterior.prior(0.008, 0.03, 900, strength=20)
    assert post.ctr.alpha + post.ctr.beta == pytest.approx(20_000)


def test_exploration_budget_holds_against_many_untried_arms() -> None:
    """300 untried grid arms must not steal exploit slots from a proven winner (max-of-draws effect)."""
    rng = random.Random(9)
    prior = RevenuePosterior.prior(0.008, 0.03, 900, strength=20)
    proven = prior
    for _ in range(40):  # 40 contents, CTR 1.2 %, 4 orders / 100 clicks, 900 KRW per order
        proven = proven.observe(1000, 12, 0.48, 432)
    arms = [ArmCandidate("proven", proven), *(ArmCandidate(f"young{i}", prior) for i in range(300))]
    alloc = thompson_allocate(arms, 100, rng, exploration_budget=0.2)
    assert sum(a.mode == "explore" for a in alloc) == 20
    assert all(a.key == "proven" for a in alloc if a.mode == "exploit")
    cold = thompson_allocate([ArmCandidate(f"y{i}", prior) for i in range(5)], 10, rng)
    assert len(cold) == 10 and all(a.mode == "explore" for a in cold)  # nothing mature yet → all exploration

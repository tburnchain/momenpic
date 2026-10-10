"""M10 학습·최적화 (Optimizer), design §4 M10, §2.2, §7.2, §11 G3.

* **Bandit** — arm = (카테고리, 훅 유형, 템플릿, 페르소나, 플랫폼, 시간대); reward = commission within the
  7-day horizon after publishing. Thompson sampling with Beta-Bernoulli CTR and Gamma-Poisson
  orders/commission (`acvm.quant.bayes`), exploration budget 20 % (``thompson_allocate``).
* **Updates** — :meth:`Optimizer.on_perf` consumes ``perf.updated``. For each PUBLISHED publication of the
  content still inside the reward horizon it rebuilds the cumulative observation
  (views, clicks — Coupang report clicks when present, else platform link clicks —, orders, commission)
  and applies the exact delta against the previously recorded observation (`posterior.apply_delta`), so
  redelivery is a no-op and late data revises rather than double-counts.
* **Amplification** (§2.2 원칙 2) — a publication ≤ 48 h old whose views rank ≥ 98th percentile among the
  workspace's same-platform publications (30 days) triggers ``amplify.requested`` once per content:
  5 variants + cross-posting to platforms the content is not yet on (and where the workspace has a usable
  account). The orchestrator intersects the list with :meth:`Optimizer.platform_admission` (§7.2).
* **Retirement** — among mature arms (≥ 30 trials; at least :data:`MIN_MATURE_ARMS` of them so a decile
  exists) those with posterior-mean EPC ≤ the bottom-10 % threshold are retired (sticky; excluded from
  allocation).
* **G3 admission** (§11, §2.3) — a platform is expanded only when its median views clear
  I* = C_unit / (CTR·CVR·AOV·r).
* **Playbook** — factor-level RPI lifts with Monte-Carlo confidence become Korean rules for M4 (see
  `playbook`).
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from acvm.context import AppContext
from acvm.core.clock import KST, ensure_utc
from acvm.core.enums import AccountStatus, ContentKind, HookType, Platform, PublicationState
from acvm.core.events import AmplifyRequested, ArmUpdated, Event, PerfUpdated
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.db import tables as t
from acvm.db.repos import Repos
from acvm.domain.models import ArmKey, ArmPosteriorState, Content, PlaybookRule, Publication
from acvm.modules.m10_optimizer.playbook import (
    CONTEXT_FACTORS,
    FACTORS,
    MIN_RULE_PROBABILITY,
    MIN_RULE_TRIALS,
    keeps_numbers,
    lift_probability,
    rule_text,
)
from acvm.modules.m10_optimizer.posterior import (
    Observation,
    apply_delta,
    centred_prior,
    economics_prior,
    pooled_posterior,
    state_from_prior,
    summary,
    to_posterior,
)
from acvm.quant.bayes import ArmCandidate, RevenuePosterior, retirement_threshold, thompson_allocate
from acvm.quant.distributions import LogNormal, percentile_rank
from acvm.quant.economics import Funnel, channel_admission
from acvm.quant.scheduling import time_slot
from acvm.security.prompt_guard import DATA_POLICY, data_block

log = get_logger(__name__)

TEMPLATES: tuple[str, ...] = ("problem-solution-v3", "before-after-v1", "price-twist-v1", "listicle-v1")
PERSONAS: tuple[str, ...] = ("30대 여성 직장인", "20대 자취생", "40대 주부", "30대 남성 직장인")
DEFAULT_SLOT = "evening"
FALLBACK_EXPECTED_VIEWS = 600.0
POPULATION_WINDOW = timedelta(days=30)
MIN_MATURE_ARMS = 10
_DEAD_STATES = {PublicationState.FAILED, PublicationState.CANCELLED, PublicationState.EXPIRED,
                PublicationState.TAKEN_DOWN}
_PLAYBOOK_SYSTEM = (
    "You rewrite one Korean marketing-playbook rule so it reads naturally for a script writer. Keep every "
    "number, percentage, probability and sample count exactly as written; do not add claims. Reply with the "
    "rewritten sentence only. " + DATA_POLICY
)


class Recommendation(BaseModel):
    """One production slot chosen by Thompson sampling (consumed by the orchestrator / M4)."""

    model_config = ConfigDict(frozen=True)

    arm_key: str
    hook_type: str
    template_id: str
    persona: str
    platform: Platform
    slot: str
    mode: Literal["explore", "exploit"]
    sampled_value: float


@dataclass(slots=True)
class PerfOutcome:
    """What one ``perf.updated`` changed (returned for tests / workflow logging)."""

    arm_updates: list[ArmUpdated] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    amplify: AmplifyRequested | None = None


@dataclass(frozen=True, slots=True)
class _PubObservation:
    publication: Publication
    arm_key: str
    observation: Observation


def arm_key_for(content: Content, category_id: str, publication: Publication) -> ArmKey:
    """§4 M10 arm = (category, hook type, template, persona, platform, time slot of publishing)."""
    at = publication.published_at or publication.scheduled_at
    return ArmKey(category=category_id, hook_type=content.script.hook_type.value, template=content.template_id,
                  persona=content.persona, platform=publication.platform.value, slot=time_slot(at))


def _parse(key: str) -> ArmKey | None:
    try:
        return ArmKey.parse(key)
    except ValueError:
        return None


class Optimizer:
    """M10 service bound to one :class:`AppContext`."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.policy = ctx.settings.optimizer

    def subscribe(self) -> None:
        """Consume ``perf.updated`` (§6.3: M9 → M10)."""
        self.ctx.bus.subscribe(PerfUpdated.TYPE, "m10_optimizer", self._handle)

    async def _handle(self, event: Event) -> None:
        await self.on_perf(event)

    # ───────────────────────────── priors ─────────────────────────────

    def _economics_prior(self) -> RevenuePosterior:
        return economics_prior(self.ctx.settings.economics, self.policy.prior_strength)

    def _parent_prior(self, states: Sequence[ArmPosteriorState], category: str, platform: str) -> RevenuePosterior:
        """Prior for a new arm: centred on the pooled category × platform posterior (else economics)."""
        parents = [s for s in states if (k := _parse(s.arm_key)) and k.category == category and k.platform == platform]
        centre = pooled_posterior(self._economics_prior(), parents) if parents else self._economics_prior()
        return centred_prior(centre, self.policy.prior_strength)

    async def _new_state(self, r: Repos, workspace_id: uuid.UUID, key: str, at: datetime) -> ArmPosteriorState:
        k = ArmKey.parse(key)
        prior = self._parent_prior(await r.arms.all(workspace_id), k.category, k.platform)
        return state_from_prior(key, prior, at)

    async def epc_prior(self, workspace_id: uuid.UUID, category_id: str) -> float:
        """Posterior-mean EPC of the category (pooled arms over the economics prior) — M1's EPC_bayes prior."""
        async with self.ctx.uow() as r:
            states = await r.arms.all(workspace_id)
        mine = [s for s in states if (k := _parse(s.arm_key)) and k.category == category_id]
        return pooled_posterior(self._economics_prior(), mine).mean_epc

    # ───────────────────────────── perf.updated → posterior ─────────────────────────────

    async def _observe(self, publication: Publication, sub_id: str) -> tuple[Observation, dict[str, float]]:
        metrics = await self.ctx.perf.latest_metrics(publication.id)
        published = ensure_utc(publication.published_at or publication.scheduled_at)
        report = (await self.ctx.perf.report_totals([sub_id], since=published.astimezone(KST).date())
                  ).get(sub_id, {}) if sub_id else {}
        views = metrics.get("views", metrics.get("impressions", 0.0))
        report_clicks = float(report.get("clicks", 0))
        clicks = report_clicks if report_clicks > 0 else metrics.get("link_clicks", 0.0)
        return Observation(views, clicks, float(report.get("orders", 0)),
                           float(report.get("commission_krw", 0))), metrics

    async def _views_population(self, platform: str, ids: Sequence[uuid.UUID], since: datetime) -> list[float]:
        if not ids:
            return []
        return await self.ctx.perf.metric_population(platform, "views", since, ids)

    async def on_perf(self, event: Event | PerfUpdated) -> PerfOutcome:
        """``perf.updated`` handler: delta posterior updates, retirement, amplification (idempotent)."""
        payload = event if isinstance(event, PerfUpdated) else event.parsed()
        outcome = PerfOutcome()
        if not isinstance(payload, PerfUpdated):
            return outcome
        now = ensure_utc(self.ctx.clock.now())
        horizon = timedelta(days=self.policy.reward_horizon_days)
        async with self.ctx.uow() as r:
            content = await r.contents.get(payload.content_id)
            if content is None:
                return outcome
            product = await r.products.get(content.product_id)
            all_pubs = await r.publications.by_content(content.id)
            pubs = [p for p in all_pubs if p.state is PublicationState.PUBLISHED and p.published_at is not None]
            sub_ids = {p.id: (link.sub_id if (link := await r.links.get(p.link_id)) else "") for p in pubs}
            ws_pubs = await r.publications.published_since(now - POPULATION_WINDOW, content.workspace_id)
        category = product.category_id if product else "unknown"
        ws = content.workspace_id

        observed: list[_PubObservation] = []
        amplify_hit: Publication | None = None
        for pub in pubs:
            age = now - ensure_utc(pub.published_at)  # type: ignore[arg-type]
            if age < timedelta(0) or age > horizon:
                continue
            obs, metrics = await self._observe(pub, sub_ids.get(pub.id, ""))
            if not obs.empty:
                observed.append(_PubObservation(pub, arm_key_for(content, category, pub).key(), obs))
            if amplify_hit is None and age <= timedelta(hours=self.policy.amplify_window_hours):
                views = metrics.get("views", 0.0)
                ids = [p.id for p in ws_pubs if p.platform == pub.platform]
                population = await self._views_population(pub.platform.value, ids, now - POPULATION_WINDOW)
                if views > 0 and population and percentile_rank(views, population) >= self.policy.amplify_percentile:
                    amplify_hit = pub

        async with self.ctx.uow() as r:
            changed: dict[str, tuple[ArmPosteriorState, str]] = {}
            for po in observed:
                o = po.observation
                prev_row = await r.arms.record_observation(ws, po.publication.id, po.arm_key, o.impressions,
                                                           o.clicks, o.orders, o.commission_krw, now)
                prev = (Observation(prev_row.impressions, prev_row.clicks, prev_row.orders, prev_row.commission_krw)
                        if prev_row is not None else None)
                if prev_row is not None and prev_row.arm_key == po.arm_key and prev == o:
                    continue  # redelivery / unchanged data — posterior must not move
                if prev_row is not None and prev_row.arm_key != po.arm_key:
                    old = await r.arms.get(ws, prev_row.arm_key)
                    if old is not None:
                        old = apply_delta(old, None, prev, now)
                        await r.arms.put(ws, old)
                        changed[old.arm_key] = (old, f"move:{po.publication.id}")
                    prev = None
                state = await r.arms.get(ws, po.arm_key) or await self._new_state(r, ws, po.arm_key, now)
                state = apply_delta(state, o, prev, now)
                await r.arms.put(ws, state)
                changed[state.arm_key] = (state, f"{po.publication.id}:{stable_hash(o.sufficient(), length=12)}")

            for key in await self._retire(r, ws, now):
                st = await r.arms.get(ws, key)
                if st is not None:
                    changed[key] = (st, "retired")
                    outcome.retired.append(key)

            for key, (st, suffix) in sorted(changed.items()):
                upd = ArmUpdated(arm_key=key, posterior=summary(st))
                await self.ctx.emit(r, Event.of(upd, workspace_id=ws, idempotency_key=f"arm:{ws}:{key}:{suffix}"))
                outcome.arm_updates.append(upd)

            if amplify_hit is not None:
                outcome.amplify = await self._amplify(r, content, all_pubs, now)
        if outcome.arm_updates or outcome.amplify:
            log.info("m10_perf_applied", content_id=str(content.id), arms=len(outcome.arm_updates),
                     retired=len(outcome.retired), amplify=outcome.amplify is not None)
        return outcome

    async def _amplify(self, r: Repos, content: Content, pubs: Sequence[Publication],
                       now: datetime) -> AmplifyRequested | None:
        marker = f"m10:amplify:{content.id}"
        if await r.activities.get(marker) is not None:
            return None  # once per content
        live = {p.platform for p in pubs if p.state not in _DEAD_STATES}
        usable = {a.platform for a in await r.accounts.list(content.workspace_id)
                  if a.status is not AccountStatus.RESTRICTED}
        cross = [p for p in Platform if p in usable and p not in live]
        req = AmplifyRequested(content_id=content.id, variants=self.policy.amplify_variants, cross_platforms=cross)
        await self.ctx.emit(r, Event.of(req, workspace_id=content.workspace_id,
                                        idempotency_key=f"amplify:{content.id}"))
        await r.activities.put(marker, "m10_amplify", req.model_dump(mode="json"), now)
        return req

    # ───────────────────────────── retirement ─────────────────────────────

    async def _retire(self, r: Repos, workspace_id: uuid.UUID, now: datetime) -> list[str]:
        states = await r.arms.all(workspace_id)
        mature = [s for s in states if s.trials >= self.policy.retire_min_trials]
        if len(mature) < MIN_MATURE_ARMS:
            return []
        epcs = [to_posterior(s).mean_epc for s in mature]
        threshold = retirement_threshold(epcs, self.policy.retire_percentile)
        newly: list[str] = []
        for s, epc in zip(mature, epcs, strict=True):
            if not s.retired and epc <= threshold:
                await r.arms.put(workspace_id, s.model_copy(update={"retired": True, "updated_at": now}))
                newly.append(s.arm_key)
        if newly:
            log.info("m10_arms_retired", workspace_id=str(workspace_id), keys=newly, threshold_epc=threshold)
        return newly

    async def apply_retirement(self, workspace_id: uuid.UUID) -> list[str]:
        """Run the §4 M10 retirement rule now; returns newly retired arm keys."""
        now = ensure_utc(self.ctx.clock.now())
        async with self.ctx.uow() as r:
            keys = await self._retire(r, workspace_id, now)
            for key in keys:
                st = await r.arms.get(workspace_id, key)
                if st is not None:
                    await self.ctx.emit(r, Event.of(ArmUpdated(arm_key=key, posterior=summary(st)),
                                                    workspace_id=workspace_id,
                                                    idempotency_key=f"arm:{workspace_id}:{key}:retired"))
        return keys

    # ───────────────────────────── allocation ─────────────────────────────

    async def _expected_views(self, workspace_id: uuid.UUID, platforms: Iterable[Platform],
                              pubs: Sequence[Publication], now: datetime) -> dict[str, float]:
        """E[I] = exp(μ + σ²/2) of the LogNormal fitted to recent views (§2.2); 600 without data."""
        out: dict[str, float] = {}
        for p in platforms:
            ids = [x.id for x in pubs if x.platform == p]
            views = [v for v in await self._views_population(p.value, ids, now - POPULATION_WINDOW) if v > 0]
            try:
                out[p.value] = LogNormal.fit(views).mean if len(views) >= 2 else FALLBACK_EXPECTED_VIEWS
            except ValueError:
                out[p.value] = FALLBACK_EXPECTED_VIEWS
        return out

    async def recommend(self, workspace_id: uuid.UUID, category_id: str, n: int, platforms: list[Platform], *,
                        seed_key: str) -> list[Recommendation]:
        """Thompson allocation of ``n`` slots (⌈20 %·n⌉ explore on young arms, rest exploit).

        Candidates = known arms of the category on ``platforms`` ∪ the grid hook types × TEMPLATES ×
        PERSONAS × platforms × "evening"; unknown arms carry the hierarchical prior. Retired arms get
        nothing. Deterministic for a given ``seed_key`` and posterior state (``ctx.rng(seed_key)``,
        candidates in key order).
        """
        now = ensure_utc(self.ctx.clock.now())
        plats = list(dict.fromkeys(Platform(p) for p in platforms))
        if n <= 0 or not plats:
            return []
        async with self.ctx.uow() as r:
            states = await r.arms.all(workspace_id)
            pubs = await r.publications.published_since(now - POPULATION_WINDOW, workspace_id)
        expected = await self._expected_views(workspace_id, plats, pubs, now)
        wanted = {p.value for p in plats}
        known = {s.arm_key: s for s in states
                 if (k := _parse(s.arm_key)) and k.category == category_id and k.platform in wanted}
        grid = {ArmKey(category=category_id, hook_type=h.value, template=tpl, persona=per, platform=p.value,
                       slot=DEFAULT_SLOT).key()
                for h in HookType for tpl in TEMPLATES for per in PERSONAS for p in plats}
        priors = {p.value: self._parent_prior(states, category_id, p.value) for p in plats}
        candidates: list[ArmCandidate] = []
        for key in sorted(set(known) | grid):
            platform = key.rsplit("|", 2)[1]
            st = known.get(key)
            post = to_posterior(st) if st is not None else priors[platform]
            candidates.append(ArmCandidate(key=key, posterior=post, expected_views=expected[platform],
                                           retired=bool(st and st.retired)))
        allocations = thompson_allocate(candidates, n, self.ctx.rng(seed_key),
                                        exploration_budget=self.policy.exploration_budget,
                                        young_trials=self.policy.retire_min_trials)
        recs: list[Recommendation] = []
        for a in allocations:
            k = ArmKey.parse(a.key)
            recs.append(Recommendation(arm_key=a.key, hook_type=k.hook_type, template_id=k.template, persona=k.persona,
                                       platform=Platform(k.platform), slot=k.slot,
                                       mode="explore" if a.mode == "explore" else "exploit",
                                       sampled_value=round(a.sampled_value, 6)))
        modes = Counter(rec.mode for rec in recs)
        async with self.ctx.uow() as r:  # learning-view record (§9.3 탐색/활용 비율); first write wins
            await r.activities.put(f"m10:recommend:{workspace_id}:{seed_key}", "m10_recommend",
                                   {"workspace_id": str(workspace_id), "category_id": category_id,
                                    "modes": dict(modes), "arms": [rec.arm_key for rec in recs]}, now)
        return recs

    # ───────────────────────────── G3 platform admission ─────────────────────────────

    async def platform_admission(self, workspace_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        """§11 G3 / §7.2 per platform: admitted iff median views (30 d, LogNormal fit) ≥ I*."""
        now = ensure_utc(self.ctx.clock.now())
        since = now - POPULATION_WINDOW
        async with self.ctx.uow() as r:
            accounts = await r.accounts.list(workspace_id)
            pubs = await r.publications.published_since(since, workspace_id)
            kinds: dict[uuid.UUID, str] = {}
            if pubs:
                rows = (await r.session.execute(select(t.ContentRow.id, t.ContentRow.kind).where(
                    t.ContentRow.id.in_(list({p.content_id for p in pubs}))))).all()
                kinds = {cid: kind for cid, kind in rows}
        e = self.ctx.settings.economics
        funnel = Funnel(ctr=e.ctr, cvr=e.cvr, aov_krw=e.aov_krw, commission_rate=e.commission_rate)
        platforms = {a.platform for a in accounts} | {p.platform for p in pubs}
        out: dict[str, dict[str, Any]] = {}
        for platform in (p for p in Platform if p in platforms):
            mine = [p for p in pubs if p.platform == platform]
            samples = await self._views_population(platform.value, [p.id for p in mine], since)
            unit = [e.c_unit_text_krw if kinds.get(p.content_id) == ContentKind.TEXT.value else e.c_unit_short_krw
                    for p in mine]
            c_unit = statistics.fmean(unit) if unit else e.c_unit_short_krw
            d = channel_admission(samples, funnel, c_unit)
            out[platform.value] = {
                "admitted": d.admitted, "n": d.n, "median_views": _finite(d.median_views),
                "median_ci": [_finite(d.median_ci[0]), _finite(d.median_ci[1])],
                "breakeven_views": _finite(d.breakeven_views), "ratio": _finite(d.ratio),
                "c_unit_krw": c_unit, "reason": d.reason,
            }
        return out

    # ───────────────────────────── playbook ─────────────────────────────

    async def refresh_playbook(self, workspace_id: uuid.UUID) -> list[PlaybookRule]:
        """Mine factor lifts into Korean rules (P(lift>0) ≥ 0.9, ≥ 10 trials); returns the active M10 rules."""
        now = ensure_utc(self.ctx.clock.now())
        strength = self.policy.prior_strength
        async with self.ctx.uow() as r:
            states = [s for s in await r.arms.all(workspace_id) if _parse(s.arm_key) is not None]
            categories = sorted({ArmKey.parse(s.arm_key).category for s in states})
            names: dict[str, str] = {}
            if categories:
                rows = (await r.session.execute(select(t.ProductRow.category_id, t.ProductRow.category_name)
                                                .where(t.ProductRow.category_id.in_(categories)))).all()
                names = {cid: name for cid, name in rows if name}
        keyed = [(ArmKey.parse(s.arm_key), s) for s in states]

        contexts: list[tuple[str | None, str | None, tuple[str, ...], list[tuple[ArmKey, ArmPosteriorState]]]] = [
            (None, None, FACTORS, keyed)]
        by_cp: dict[tuple[str, str], list[tuple[ArmKey, ArmPosteriorState]]] = defaultdict(list)
        for k, s in keyed:
            by_cp[(k.category, k.platform)].append((k, s))
        contexts += [(c, p, CONTEXT_FACTORS, members) for (c, p), members in sorted(by_cp.items())]

        candidates: list[PlaybookRule] = []
        for category, platform, factors, members in contexts:
            base_trials = sum(s.trials for _, s in members)
            if base_trials < MIN_RULE_TRIALS:
                continue
            baseline = pooled_posterior(self._economics_prior(), [s for _, s in members])
            for factor in factors:
                groups: dict[str, list[ArmPosteriorState]] = defaultdict(list)
                for k, s in members:
                    groups[getattr(k, factor)].append(s)
                for value in sorted(groups):
                    group = groups[value]
                    trials = sum(s.trials for s in group)
                    if trials < MIN_RULE_TRIALS or trials >= base_trials:
                        continue  # too little evidence, or the factor *is* the whole context
                    post = pooled_posterior(centred_prior(baseline, strength), group)
                    scope_key = f"m10:{category or '*'}:{platform or '*'}:{factor}={value}"
                    seed = self.ctx.rng("playbook", workspace_id, scope_key).getrandbits(63)
                    lift, prob = lift_probability(post, baseline, seed)
                    if prob < MIN_RULE_PROBABILITY or lift <= 0:
                        continue
                    text = rule_text(factor, value, category, platform, lift, prob, trials, names)
                    candidates.append(PlaybookRule(
                        workspace_id=workspace_id, text=text,
                        scope={"key": scope_key, "source": "m10", "factor": factor, "value": value,
                               "category": category or "", "platform": platform or ""},
                        evidence={"trials": trials, "impressions": post.impressions, "clicks": post.clicks,
                                  "orders": post.orders, "commission_krw": post.commission_krw,
                                  "rpi_factor": post.mean_rpi, "rpi_baseline": baseline.mean_rpi,
                                  "baseline_trials": base_trials, "p_lift_positive": prob},
                        lift=round(lift, 4), confidence=round(prob, 4), created_at=now))

        async with self.ctx.uow() as r:
            existing = {(rule.scope or {}).get("key", ""): rule
                        for rule in await r.playbook.active(workspace_id, limit=100_000)
                        if rule.workspace_id == workspace_id and (rule.scope or {}).get("source") == "m10"}
        fresh: list[PlaybookRule] = []
        kept: list[PlaybookRule] = []
        for rule in candidates:
            old = existing.get(rule.scope["key"])
            if old is not None and old.evidence.get("rule_numbers") == rule_text_numbers(rule):
                kept.append(old)  # same numbers ⇒ same rule; keep the row (and its rephrasing) untouched
            else:
                fresh.append(rule)
        fresh = [await self._rephrase(rule) for rule in fresh]
        embeddings = await self._embed([rule.text for rule in fresh])
        new_keys = {rule.scope["key"] for rule in candidates}
        async with self.ctx.uow() as r:
            for key in sorted(set(existing) - new_keys):
                await r.playbook.deactivate_scope(workspace_id, key)  # no longer supported by the data
            for rule, emb in zip(fresh, embeddings, strict=True):
                await r.playbook.deactivate_scope(workspace_id, rule.scope["key"])  # superseded
                await r.playbook.add(rule, emb)
        if fresh or set(existing) - new_keys:
            log.info("m10_playbook_refreshed", workspace_id=str(workspace_id), new=len(fresh), kept=len(kept),
                     retired=len(set(existing) - new_keys))
        return sorted(kept + fresh, key=lambda x: (-x.confidence, x.scope["key"]))

    async def _rephrase(self, rule: PlaybookRule) -> PlaybookRule:
        """Optional LLM polish; rejected unless every number survives verbatim (``keeps_numbers``)."""
        evidence = {**rule.evidence, "rule_numbers": rule_text_numbers(rule), "template_text": rule.text}
        rule = rule.model_copy(update={"evidence": evidence})
        if self.ctx.llm is None:
            return rule
        try:
            text = (await self.ctx.llm.text(tier="small", system=_PLAYBOOK_SYSTEM,
                                            user=data_block("rule", rule.text, 500),
                                            purpose="m10_playbook_rephrase")).strip()
        except Exception as exc:
            log.warning("m10_playbook_rephrase_failed", error=type(exc).__name__)
            return rule
        if text and len(text) <= 300 and keeps_numbers(rule.text, text):
            return rule.model_copy(update={"text": text})
        return rule

    async def _embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        if not texts:
            return []
        vectors = await self.ctx.embedder.embed(list(texts))
        return [v if len(v) == 1024 else None for v in vectors]


def rule_text_numbers(rule: PlaybookRule) -> str:
    """Canonical fingerprint of a rule's statistics (lift %, probability, trials) for change detection."""
    ev = rule.evidence
    return f"{rule.lift * 100:+.0f}|{rule.confidence:.2f}|{int(ev.get('trials', 0))}"


def _finite(x: float) -> float | None:
    return round(float(x), 4) if math.isfinite(x) else None

"""M8 Distribution engine (§4 M8, §5.2, §7, §7.1, §8.1 S-02/A-01, §9.5, §10).

Responsibilities, in the order a publication meets them:

* **plan** — platform caption variants (captions.py), publish times on the account's daily grid (slots.py),
  QUEUED publications keyed by ``publication_key(content, platform, account)`` (idempotent, §4 M8).
* **publish** — preconditions, each enforced here and never delegated:
  content ``approved`` (the M7 gate is the only path, §3.1 — anything else raises ``PolicyViolation``);
  account active (cooldown → reschedule after ``cooldown_until``, S-02); affiliate-link integrity and health
  (§10, §9.5 #4); per-adapter circuit breaker (§9.5 #1); local quota window + real-time platform quota
  (§7.1 #1–#2); a valid token (§10). Then QUEUED → UPLOADING → PROCESSING → PUBLISHED through the adapter.
* **poll** — advances PROCESSING (container/upload status); Instagram/Threads containers expire after 24 h.
* **take_down** — saga compensation (§5.2); where the official API cannot delete, the post stays PUBLISHED
  with ``manual_takedown_required`` and an audit entry — success is never faked.
* **manual queue** — Kakao has no official publishing API: items wait in MANUAL_PENDING for a human.
* **account health** — health events per attempt; ``quant.risk.assess_account`` over 7 days; a platform
  warning (sanction signal) cools the account down for 48 h, cancels its whole schedule and isolates the
  triggering publication (§7.1 #3, §9.5 #3); failure-rate/reach cooldowns only reschedule.

Error policy: retryable upstream errors propagate (Temporal retries the activity); non-retryable ones end the
publication in FAILED with the error JSON. Every transition emits ``publication.state_changed`` through the
transactional outbox with idempotency key ``{publication_id}:{state}``.

M8 never calls an LLM and has no access to gate rules; it reads only ``GatePolicy.standard_disclosure``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from acvm.core.clock import ensure_utc
from acvm.core.enums import AccountStatus, ContentKind, ContentStatus, LinkHealth, Platform
from acvm.core.enums import PublicationState as S
from acvm.core.errors import (
    ACVMError,
    ConfigError,
    IntegrityViolation,
    NotFound,
    PolicyViolation,
    QuotaExhausted,
    UpstreamError,
    ValidationFailed,
)
from acvm.core.events import AccountStatusChanged, Event, PublicationStateChanged
from acvm.core.ids import publication_key
from acvm.core.logging import get_logger
from acvm.core.metrics import ACCOUNT_STATUS, CIRCUIT_STATE, GATE_RULE_REGRESSIONS, PUBLISH_ATTEMPTS
from acvm.db import tables as t
from acvm.db.repos import Repos
from acvm.domain.models import AffiliateLink, AuditEntry, ChannelAccount, Content, Publication, PublishTarget
from acvm.infra.platforms.base import PlatformClient, PlatformWarning, PostRequest, PostResult, RemoteStatus
from acvm.infra.platforms.registry import get_platform_clients
from acvm.infra.platforms.tokens import PlatformPolicyWarning, TokenManager, TokenRefreshFailed
from acvm.modules.m8_distribution.captions import (
    compose_caption,
    compose_title,
    hashtag_words,
    resolve_placement,
)
from acvm.modules.m8_distribution.quota import QuotaGate, effective_daily_cap
from acvm.modules.m8_distribution.slots import day_grid, first_free, fit_to_window, kst_day_bounds
from acvm.modules.m8_distribution.state import DEAD_STATES, route
from acvm.quant.ratelimit import BreakerState, CircuitBreaker
from acvm.quant.risk import HealthDecision, assess_account
from acvm.security.linksign import verify_partner_tag, verify_short_url, verify_signature

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

ACTOR = "M8"
CONTAINER_TTL = timedelta(hours=24)  # §4 M8 / §7: Instagram (and Threads) containers expire after 24 h
EXPIRING_CONTAINERS = frozenset({Platform.THREADS, Platform.INSTAGRAM})
VIDEO_ONLY = frozenset({Platform.INSTAGRAM, Platform.FACEBOOK, Platform.YOUTUBE, Platform.TIKTOK})
HEALTH_LOOKBACK = timedelta(days=7)
LINK_RECHECK = timedelta(hours=6)  # §4 M3 link health checker interval
MAX_PLAN_DAYS = 30
BREAKER_THRESHOLD = 5  # consecutive non-retryable 4xx responses that open an adapter (§9.5 #1)
BREAKER_OPEN_SECONDS = 1800.0
BREAKERS_KEY = "m8_circuit_breakers"
MIN_DEFER = timedelta(minutes=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class _Loaded:
    pub: Publication
    content: Content | None
    account: ChannelAccount
    link: AffiliateLink | None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def _error_json(exc: BaseException, **extra: Any) -> dict[str, Any]:
    """Publication.error payload — messages only, never request data (tokens stay out, §10)."""
    out: dict[str, Any] = {
        "code": getattr(exc, "code", type(exc).__name__),
        "type": type(exc).__name__,
        "message": str(exc)[:500],
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    if isinstance(exc, UpstreamError):
        out["status"] = exc.status
        for key in ("platform", "code", "subcode"):
            if exc.context.get(key) is not None:
                out[f"platform_{key}" if key != "platform" else key] = exc.context[key]
    out.update(_jsonable(extra))
    return out


class DistributionService:
    """M8 engine. Stateless apart from per-adapter circuit breakers kept in ``ctx.extras``."""

    def __init__(self, ctx: AppContext, clients: Mapping[Platform, PlatformClient] | None = None, *,
                 verify_links: bool = True, breaker_threshold: int = BREAKER_THRESHOLD,
                 breaker_open_seconds: float = BREAKER_OPEN_SECONDS) -> None:
        self.ctx = ctx
        self.clients: Mapping[Platform, PlatformClient] = clients if clients is not None else get_platform_clients(ctx)
        self.tokens = TokenManager(ctx, self.clients)
        self.quota = QuotaGate(ctx.quota, ctx.settings.quota)
        self.policy = ctx.settings.quota
        self.verify_links = verify_links
        self.breaker_threshold = breaker_threshold
        self.breaker_open_seconds = breaker_open_seconds

    # ───────────────────────────── helpers ─────────────────────────────

    def _now(self) -> datetime:
        return ensure_utc(self.ctx.clock.now())

    def _client(self, platform: Platform) -> PlatformClient:
        client = self.clients.get(platform)
        if client is None:
            raise ConfigError(f"no platform client registered for {platform.value}")
        return client

    def _breaker(self, platform: Platform) -> CircuitBreaker:
        breakers: dict[Platform, CircuitBreaker] = self.ctx.extras.setdefault(BREAKERS_KEY, {})
        breaker = breakers.get(platform)
        if breaker is None:
            breaker = breakers[platform] = CircuitBreaker(failure_threshold=self.breaker_threshold,
                                                          open_seconds=self.breaker_open_seconds)
        return breaker

    @staticmethod
    def _metric(platform: Platform, outcome: str) -> None:
        PUBLISH_ATTEMPTS.labels(platform=platform.value, outcome=outcome).inc()

    async def _audit(self, repos: Repos, action: str, target: str, **payload: Any) -> None:
        await repos.audit.write(AuditEntry(actor=ACTOR, action=action, target=target, payload=_jsonable(payload),
                                           at=self._now()))

    def _default_hub_url(self, target: PublishTarget) -> str:
        return f"{self.ctx.settings.link_hub_base_url.rstrip('/')}/hub/{target.account_handle}"

    async def _load(self, publication_id: uuid.UUID) -> _Loaded:
        async with self.ctx.uow() as repos:
            pub = await repos.publications.get(publication_id)
            if pub is None:
                raise NotFound("publication not found", publication_id=str(publication_id))
            account = await repos.accounts.get(pub.channel_account_id)
            if account is None:
                raise NotFound("channel account not found", account_id=str(pub.channel_account_id))
            content = await repos.contents.get(pub.content_id)
            link = await repos.links.get(pub.link_id)
        return _Loaded(pub, content, account, link)

    async def _transition_in(self, repos: Repos, pub: Publication, to: S, *, workspace_id: uuid.UUID,
                             run_id: uuid.UUID | None = None, **fields: Any) -> Publication:
        """Validated transition (with canonical intermediate states) + one outbox event per state entered."""
        current = await repos.publications.get(pub.id)
        if current is None:
            raise NotFound("publication not found", publication_id=str(pub.id))
        now = self._now()
        hops = route(current.state, to)
        if not hops:
            return await repos.publications.transition(pub.id, to, now, **fields) if fields else current
        for i, state in enumerate(hops):
            current = await repos.publications.transition(pub.id, state, now,
                                                          **(fields if i == len(hops) - 1 else {}))
            await self.ctx.emit(repos, Event.of(
                PublicationStateChanged(publication_id=pub.id, state=state, platform=current.platform,
                                        content_id=current.content_id),
                workspace_id=workspace_id, run_id=run_id, idempotency_key=f"{pub.id}:{state.value}"))
        return current

    async def _move(self, b: _Loaded, to: S, **fields: Any) -> Publication:
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, to, workspace_id=b.account.workspace_id, **fields)
        return b.pub

    async def _update_gauge(self, repos: Repos, account: ChannelAccount) -> None:
        accounts = await repos.accounts.list(account.workspace_id, platform=account.platform.value)
        for status in AccountStatus:
            ACCOUNT_STATUS.labels(platform=account.platform.value, status=status.value).set(
                sum(1 for a in accounts if a.status is status))

    # ───────────────────────────── plan ─────────────────────────────

    async def plan(self, content: Content, targets: list[PublishTarget], links: dict[Platform, AffiliateLink], *,
                   not_before: datetime, run_id: uuid.UUID | None = None,
                   hub_url_for: Callable[[PublishTarget], str] | None = None) -> list[Publication]:
        """Create (or return the existing) QUEUED publications for one content (§4 M8, idempotent).

        Links must already exist (M3 runs before planning): each publication is bound 1:1 to the link whose
        ``publication_key`` equals its own idempotency key, which keeps subId attribution exact (§6).
        """
        if content.status is ContentStatus.BLOCKED:
            raise PolicyViolation("blocked content cannot be distributed", content_id=str(content.id))
        platforms = [tg.platform for tg in targets]
        if len(platforms) != len(set(platforms)):
            raise PolicyViolation("one content may target only one account per platform (§7.1 #4)",
                                  content_id=str(content.id))
        not_before = ensure_utc(not_before)
        hub_for = hub_url_for or self._default_hub_url
        disclosure = self.ctx.settings.gate.standard_disclosure
        gap = timedelta(minutes=self.policy.min_gap_minutes)
        out: list[Publication] = []
        async with self.ctx.uow() as repos:
            siblings = await repos.publications.by_content(content.id)
            content_times = [p.scheduled_at for p in siblings if p.state not in DEAD_STATES]
            for target in targets:
                account = await repos.accounts.get(target.account_id)
                if account is None:
                    raise NotFound("target account not found", account_id=str(target.account_id))
                if account.platform is not target.platform:
                    raise ValidationFailed("target platform does not match the account",
                                           account_id=str(account.id), platform=target.platform.value)
                key = publication_key(content.id, target.platform.value, account.id)
                existing = await repos.publications.get_by_key(key)
                if existing is not None:
                    out.append(existing)
                    continue
                if any(p.platform is target.platform and p.channel_account_id != account.id
                       and p.state not in DEAD_STATES for p in siblings):
                    raise PolicyViolation("content already scheduled on another account of this platform (§7.1 #4)",
                                          content_id=str(content.id), platform=target.platform.value)
                if account.status is AccountStatus.RESTRICTED:
                    raise PolicyViolation("account is restricted", account_id=str(account.id))
                self._check_format(content, target.platform)
                link = await self._stored_link(repos, links, target.platform, key, content)
                placement = resolve_placement(target.platform, target.link_placement)
                caption = compose_caption(content, target.platform, link, placement, disclosure, hub_for(target))
                start = not_before
                if (account.status is AccountStatus.COOLDOWN and account.cooldown_until is not None
                        and account.cooldown_until > start):
                    start = account.cooldown_until
                when = await self._pick_slot(repos, account, start, content_times, gap)
                pub = Publication(content_id=content.id, channel_account_id=account.id, platform=target.platform,
                                  link_id=link.id, caption=caption, link_placement=placement, scheduled_at=when,
                                  idempotency_key=key, ai_label=True)
                saved = await repos.publications.add(pub)
                if saved.id == pub.id:
                    await self.ctx.emit(repos, Event.of(
                        PublicationStateChanged(publication_id=saved.id, state=S.QUEUED, platform=saved.platform,
                                                content_id=content.id),
                        workspace_id=account.workspace_id, run_id=run_id,
                        idempotency_key=f"{saved.id}:{S.QUEUED.value}"))
                    await self._audit(repos, "publication_planned", f"publication:{saved.id}",
                                      platform=saved.platform, account=account.handle, scheduled_at=when,
                                      placement=placement, run_id=run_id)
                content_times.append(saved.scheduled_at)
                out.append(saved)
        return out

    @staticmethod
    def _check_format(content: Content, platform: Platform) -> None:
        if platform in VIDEO_ONLY and content.kind is not ContentKind.SHORT:
            raise ValidationFailed(f"{platform.value} publishes video only", content_id=str(content.id))
        if content.kind is ContentKind.SHORT and platform is not Platform.KAKAO and content.render is None:
            raise ValidationFailed("short content has no render yet", content_id=str(content.id))

    @staticmethod
    async def _stored_link(repos: Repos, links: Mapping[Platform, AffiliateLink], platform: Platform, key: str,
                           content: Content) -> AffiliateLink:
        given = links.get(platform)
        if given is None:
            raise ValidationFailed(f"no affiliate link for {platform.value}: links are created before plan()")
        stored = await repos.links.get(given.id)
        if stored is None:
            raise ValidationFailed("affiliate link is not persisted (M3 must create it first)", link_id=str(given.id))
        if stored.publication_key != key:
            raise ValidationFailed("affiliate link belongs to another publication (subId must be 1:1)",
                                   link_id=str(stored.id))
        if stored.product_id != content.product_id:
            raise ValidationFailed("affiliate link is for a different product", link_id=str(stored.id))
        return stored

    async def _pick_slot(self, repos: Repos, account: ChannelAccount, not_before: datetime,
                         content_times: list[datetime], gap: timedelta) -> datetime:
        cap = effective_daily_cap(account, self.policy)
        if cap <= 0:
            raise PolicyViolation("account has no publishing capacity", account_id=str(account.id))
        day = not_before
        for _ in range(MAX_PLAN_DAYS):
            d_start, d_end = kst_day_bounds(day)
            booked = await repos.publications.booked_times(account.id, d_start, d_end)
            if len(booked) < cap:
                rng = self.ctx.rng("m8-grid", account.id, d_start.date().isoformat())
                slot = first_free(day_grid(d_start, cap, self.policy, rng), [*booked, *content_times],
                                  not_before, gap)
                if slot is not None:
                    return slot
            day = d_end
        raise QuotaExhausted("no free publishing slot in the planning horizon",
                             retry_after_seconds=86_400.0, account_id=str(account.id))

    # ───────────────────────────── publish ─────────────────────────────

    async def publish(self, publication_id: uuid.UUID) -> Publication:
        """Publish one QUEUED publication now (idempotent: non-QUEUED publications are returned or polled)."""
        b = await self._load(publication_id)
        if b.pub.state is S.PROCESSING:
            return await self.poll(publication_id)
        if b.pub.state not in (S.QUEUED, S.UPLOADING):
            return b.pub
        await self._require_approved(b)
        if b.link is None:
            raise NotFound("affiliate link missing", link_id=str(b.pub.link_id))
        blocked = await self._account_gate(b)
        if blocked is not None:
            return blocked
        held = await self._link_gate(b)
        if held is not None:
            return held
        client = self._client(b.pub.platform)
        now = self._now()
        breaker = self._breaker(b.pub.platform)
        if not breaker.allow(now.timestamp()):
            wait = timedelta(seconds=breaker.retry_after(now.timestamp()))
            return await self._defer(b, now + max(wait, MIN_DEFER), "circuit_open", "circuit_open")
        decision = await self.quota.acquire(b.account, b.pub, now)
        if not decision.allowed:
            return await self._defer(b, now + max(timedelta(seconds=decision.retry_after_seconds), MIN_DEFER),
                                     "quota_local", "deferred_quota", used=decision.used, limit=decision.limit)
        if b.pub.platform is Platform.KAKAO:
            return await self._enqueue_manual(b, client)
        try:
            token = await self.tokens.access_token(b.account)
        except TokenRefreshFailed as exc:
            await self.quota.release(b.account, b.pub)
            return await self._token_failure(b, exc)
        except BaseException:
            await self.quota.release(b.account, b.pub)
            raise
        try:
            check = await self.quota.platform_check(client, b.account, token)
        except PlatformWarning as exc:
            await self.quota.release(b.account, b.pub)
            return await self._on_warning(b, exc)
        except UpstreamError as exc:
            await self.quota.release(b.account, b.pub)
            if exc.retryable:
                raise
            self._record_breaker_failure(b.pub.platform, exc)
            return await self._fail(b, _error_json(exc, stage="publishing_limit"))
        if not check.allowed:
            await self.quota.release(b.account, b.pub)
            return await self._defer(b, now + timedelta(seconds=check.retry_after_seconds), "quota_platform",
                                     "deferred_platform_quota", used=check.used, limit=check.limit,
                                     threshold=check.threshold)
        try:
            req = await self._request(b, token, with_media=True)
        except ValidationFailed as exc:
            return await self._fail(b, _error_json(exc, stage="request"), health=False)
        await self._move(b, S.UPLOADING, attempts=b.pub.attempts + 1)
        return await self._drive(b, client, req, lambda: client.create(req), expires_at=None)

    async def _require_approved(self, b: _Loaded) -> None:
        if b.content is not None and b.content.status is ContentStatus.APPROVED:
            return
        status = b.content.status.value if b.content is not None else "missing"
        GATE_RULE_REGRESSIONS.inc()
        self._metric(b.pub.platform, "refused_not_approved")
        async with self.ctx.uow() as repos:
            await self._audit(repos, "publish_refused_not_approved", f"publication:{b.pub.id}",
                              content_id=b.pub.content_id, content_status=status)
        raise PolicyViolation("content is not approved — the M7 gate is the only path to distribution (§3.1)",
                              content_id=str(b.pub.content_id), status=status)

    async def _account_gate(self, b: _Loaded) -> Publication | None:
        now = self._now()
        acc = b.account
        if acc.status is AccountStatus.RESTRICTED:
            if b.pub.state is S.QUEUED:
                self._metric(b.pub.platform, "cancelled_restricted")
                return await self._move(b, S.CANCELLED, error={"code": "account_restricted"})
            return await self._fail(b, {"code": "account_restricted", "retryable": False}, health=False)
        if acc.status is AccountStatus.COOLDOWN:
            if acc.cooldown_until is not None and acc.cooldown_until > now:
                return await self._defer(b, acc.cooldown_until, "account_cooldown", "deferred_cooldown")
            await self.evaluate_account(acc.id)
            async with self.ctx.uow() as repos:
                refreshed = await repos.accounts.get(acc.id)
            if refreshed is None:
                raise NotFound("channel account not found", account_id=str(acc.id))
            b.account = refreshed
            if refreshed.status is not AccountStatus.ACTIVE:
                until = refreshed.cooldown_until or now + timedelta(hours=self.policy.cooldown_hours)
                return await self._defer(b, until, "account_cooldown", "deferred_cooldown")
        return None

    async def _verify_link(self, link: AffiliateLink) -> None:
        """§10: signature over (sub_id, deeplink, short_url, partner_id) + partner tag/host checks."""
        sec = self.ctx.settings.security
        try:
            key = (await self.ctx.secrets.get(sec.link_signing_key_ref)).encode()
        except KeyError:
            raise ConfigError("link signing key unavailable — refusing to publish unverifiable links") from None
        partner_id = self.ctx.settings.coupang.partner_id
        verify_signature(key, link.signature, link.sub_id, link.deeplink, link.short_url, partner_id)
        if partner_id:
            verify_partner_tag(link.deeplink, partner_id, link.sub_id)
            verify_short_url(link.short_url)

    async def _link_gate(self, b: _Loaded) -> Publication | None:
        assert b.link is not None
        if self.verify_links:
            try:
                await self._verify_link(b.link)
            except IntegrityViolation as exc:
                log.error("link_integrity_violation", publication_id=str(b.pub.id), sub_id=b.link.sub_id)
                return await self._fail(b, _error_json(exc, sub_id=b.link.sub_id), outcome="integrity_violation",
                                        health=False)
        if b.link.health in (LinkHealth.SOLD_OUT, LinkHealth.DISCONTINUED):
            return await self._defer(b, self._now() + LINK_RECHECK, f"link_{b.link.health.value}",
                                     "deferred_link_health")
        return None

    async def _request(self, b: _Loaded, token: str, *, with_media: bool) -> PostRequest:
        """Build the adapter request (§4 M8 media hosting: 2-hour presigned URLs for pull platforms)."""
        content, platform, pub = b.content, b.pub.platform, b.pub
        assert content is not None and b.link is not None
        video_url = video_path = thumbnail_url = None
        if with_media and content.kind is ContentKind.SHORT and platform is not Platform.KAKAO:
            if content.render is None:
                raise ValidationFailed("short content has no render", content_id=str(content.id))
            uri = content.render.variants.get(platform.value) or content.render.media_uri
            ttl = self.ctx.settings.objects.signed_url_ttl_seconds
            if platform is Platform.YOUTUBE:
                video_path = str(await self.ctx.objects.local_path(uri))
            else:
                video_url = await self.ctx.objects.signed_url(uri, ttl)
            if platform is Platform.INSTAGRAM and content.render.thumbnail_uri:
                thumbnail_url = await self.ctx.objects.signed_url(content.render.thumbnail_uri, ttl)
        direct_link = pub.link_placement in ("body", "first_comment") and platform is Platform.THREADS
        return PostRequest(
            account_platform_user_id=b.account.platform_user_id, access_token=token, text=pub.caption,
            video_url=video_url, video_path=video_path, thumbnail_url=thumbnail_url,
            link_url=b.link.short_url if direct_link else None,
            link_in_first_comment=pub.link_placement == "first_comment",
            title=compose_title(content, platform) if platform is Platform.YOUTUBE else "",
            tags=hashtag_words(content), ai_generated=True, commercial_disclosure=True, privacy="public",
            idempotency_key=pub.idempotency_key)

    async def _enqueue_manual(self, b: _Loaded, client: PlatformClient) -> Publication:
        result = await client.create(await self._request(b, "", with_media=False))
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, S.MANUAL_PENDING, workspace_id=b.account.workspace_id,
                                              platform_container_id=result.container_id, error=None)
            await self._audit(repos, "manual_publication_queued", f"publication:{b.pub.id}",
                              account=b.account.handle)
        self._metric(b.pub.platform, "manual_queued")
        return b.pub

    # ───────────────────────────── adapter driving ─────────────────────────────

    async def _drive(self, b: _Loaded, client: PlatformClient, req: PostRequest,
                     call: Callable[[], Awaitable[PostResult]], *, expires_at: datetime | None) -> Publication:
        """Run one adapter step and follow it (FINISHED → publish) until the publication settles."""
        breaker = self._breaker(b.pub.platform)
        try:
            result = await call()
            self._record_breaker_success(b.pub.platform, breaker)
            for _ in range(3):
                pending = result.status in (RemoteStatus.IN_PROGRESS, RemoteStatus.FINISHED)
                if pending and expires_at is not None and self._now() >= expires_at:
                    return await self._expire(b, "container older than 24 h")
                if result.status is RemoteStatus.IN_PROGRESS:
                    return await self._processing(b, result)
                if result.status is RemoteStatus.FINISHED:
                    await self._processing(b, result)
                    result = await client.publish(req, b.pub.platform_container_id)
                    self._record_breaker_success(b.pub.platform, breaker)
                    continue
                if result.status is RemoteStatus.PUBLISHED:
                    return await self._published(b, result)
                if result.status is RemoteStatus.EXPIRED:
                    return await self._expire(b, "platform reported the container expired")
                return await self._fail(b, {"code": "remote_error", "retryable": False,
                                            "detail": _jsonable(result.raw)})
            raise UpstreamError("platform did not settle after publish", status=None, retryable=True)
        except PlatformWarning as exc:
            return await self._on_warning(b, exc)
        except UpstreamError as exc:
            if exc.retryable:
                self._metric(b.pub.platform, "retryable_error")
                raise
            self._record_breaker_failure(b.pub.platform, exc)
            return await self._fail(b, _error_json(exc))
        except ACVMError as exc:
            if exc.retryable:
                raise
            return await self._fail(b, _error_json(exc), health=False)

    def _record_breaker_success(self, platform: Platform, breaker: CircuitBreaker) -> None:
        if breaker.state is not BreakerState.CLOSED or breaker.failures:
            breaker.record_success()
            CIRCUIT_STATE.labels(name=f"platform:{platform.value}").set(0)

    def _record_breaker_failure(self, platform: Platform, exc: UpstreamError) -> None:
        if exc.status is None or not 400 <= exc.status < 500 or exc.status == 429:
            return
        breaker = self._breaker(platform)
        was_open = breaker.state is BreakerState.OPEN
        breaker.record_failure(self._now().timestamp())
        if breaker.state is BreakerState.OPEN and not was_open:
            CIRCUIT_STATE.labels(name=f"platform:{platform.value}").set(1)
            # §9.5 #1 alert: adapter opened, queue preserved (publish() defers instead of failing)
            log.error("platform_circuit_opened", platform=platform.value, failures=breaker.failures,
                      open_seconds=breaker.open_seconds)

    async def _processing(self, b: _Loaded, result: PostResult) -> Publication:
        fields: dict[str, Any] = {"error": None}
        if result.container_id:
            fields["platform_container_id"] = result.container_id
        if result.post_id:
            fields["platform_post_id"] = result.post_id
        entering = b.pub.state is not S.PROCESSING
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, S.PROCESSING, workspace_id=b.account.workspace_id,
                                              **fields)
            if entering:  # keep what the platform said at submission (e.g. TikTok unaudited → private)
                await self._audit(repos, "publication_submitted", f"publication:{b.pub.id}",
                                  platform=b.pub.platform, container_id=b.pub.platform_container_id,
                                  remote=result.raw)
        if entering:
            self._metric(b.pub.platform, "processing")
        return b.pub

    async def _published(self, b: _Loaded, result: PostResult) -> Publication:
        now = self._now()
        post_id = (result.post_id or b.pub.platform_post_id or result.container_id
                   or b.pub.platform_container_id)
        error: dict[str, Any] | None = None
        if result.raw.get("first_comment_error"):
            error = {"first_comment_failed": True, "message": str(result.raw["first_comment_error"])[:500]}
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(
                repos, b.pub, S.PUBLISHED, workspace_id=b.account.workspace_id, platform_post_id=post_id,
                platform_container_id=result.container_id or b.pub.platform_container_id, published_at=now,
                error=error)
            await repos.accounts.record_health(b.account.id, "attempt_ok", now,
                                               detail={"publication_id": str(b.pub.id)})
            await self._audit(repos, "publication_published", f"publication:{b.pub.id}",
                              platform=b.pub.platform, post_id=post_id, permalink=result.permalink,
                              unaudited=result.raw.get("unaudited"))
        self._metric(b.pub.platform, "published")
        warning = result.raw.get("platform_warning")
        if warning:
            return await self._on_warning(b, PlatformPolicyWarning(str(warning), platform=b.pub.platform))
        return b.pub

    async def _expire(self, b: _Loaded, reason: str) -> Publication:
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, S.EXPIRED, workspace_id=b.account.workspace_id,
                                              error={"code": "container_expired", "reason": reason,
                                                     "ttl_hours": CONTAINER_TTL.total_seconds() / 3600})
            await self._audit(repos, "publication_expired", f"publication:{b.pub.id}", reason=reason)
        await self.quota.release(b.account, b.pub)
        self._metric(b.pub.platform, "expired")
        return b.pub

    async def _fail(self, b: _Loaded, error: dict[str, Any], *, outcome: str = "failed",
                    health: bool = True) -> Publication:
        now = self._now()
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, S.FAILED, workspace_id=b.account.workspace_id,
                                              error=_jsonable(error))
            await self._audit(repos, "publication_failed", f"publication:{b.pub.id}", error=error)
            if health:
                await repos.accounts.record_health(b.account.id, "attempt_fail", now,
                                                   detail={"publication_id": str(b.pub.id),
                                                           "code": str(error.get("code", ""))})
                account = await repos.accounts.get(b.account.id)
                if account is not None and account.status is AccountStatus.ACTIVE:
                    decision = await self._assess(repos, account, now)
                    if decision.cooldown:
                        await self._enter_cooldown(repos, account, reason="; ".join(decision.reasons),
                                                   sanction=False)
        await self.quota.release(b.account, b.pub)
        self._metric(b.pub.platform, outcome)
        return b.pub

    async def _on_warning(self, b: _Loaded, warning: PlatformWarning) -> Publication:
        """§7.1 #3 / §9.5 #3: isolate the triggering publication, cool down, cancel the account's schedule."""
        now = self._now()
        detail = warning.as_dict() if isinstance(warning, PlatformPolicyWarning) else {"message": str(warning)}
        released = False
        async with self.ctx.uow() as repos:
            current = await repos.publications.get(b.pub.id)
            assert current is not None
            if current.state in (S.QUEUED, S.UPLOADING, S.PROCESSING):
                b.pub = await self._transition_in(repos, current, S.FAILED, workspace_id=b.account.workspace_id,
                                                  error={"code": "platform_warning", "isolated": True, **detail})
                released = True
            else:  # already live (e.g. warning on the Threads first comment): keep it, flag it
                b.pub = await repos.publications.transition(current.id, current.state, now,
                                                            error={"platform_warning": detail})
            await repos.accounts.record_health(b.account.id, "warning", now,
                                               detail={"publication_id": str(b.pub.id), **detail})
            account = await repos.accounts.get(b.account.id)
            assert account is not None
            await self._enter_cooldown(repos, account, reason=f"platform warning: {warning}", sanction=True,
                                       trigger=b.pub.id)
        if released:
            await self.quota.release(b.account, b.pub)
        self._metric(b.pub.platform, "policy_reject")  # excluded from the publish-success SLO (§9.2)
        return b.pub

    async def _enter_cooldown(self, repos: Repos, account: ChannelAccount, *, reason: str, sanction: bool,
                              trigger: uuid.UUID | None = None) -> datetime:
        now = self._now()
        until = now + timedelta(hours=self.policy.cooldown_hours)
        await repos.accounts.set_status(account.id, AccountStatus.COOLDOWN, until)
        await self.ctx.emit(repos, Event.of(
            AccountStatusChanged(account_id=account.id, status=AccountStatus.COOLDOWN.value, reason=reason[:500],
                                 until=until),
            workspace_id=account.workspace_id, idempotency_key=f"{account.id}:cooldown:{until.isoformat()}"))
        cancelled: list[str] = []
        if sanction:
            for pending in await repos.publications.pending_for_account(account.id):
                await self._transition_in(repos, pending, S.CANCELLED, workspace_id=account.workspace_id,
                                          error={"code": "account_sanction", "reason": reason[:500]})
                cancelled.append(str(pending.id))
        await self._audit(repos, "account_cooldown", f"account:{account.id}", reason=reason, until=until,
                          sanction=sanction, trigger=trigger, cancelled=cancelled)
        await self._update_gauge(repos, account)
        log.warning("account_cooldown", account_id=str(account.id), platform=account.platform.value,
                    reason=reason, until=until.isoformat(), sanction=sanction, cancelled=len(cancelled))
        return until

    async def _defer(self, b: _Loaded, until: datetime, reason: str, outcome: str, **detail: Any) -> Publication:
        """Reschedule a QUEUED publication (stays QUEUED). In-flight retries cannot go back to QUEUED, so they
        raise a retryable ``QuotaExhausted`` carrying the wait instead."""
        now = self._now()
        if b.pub.state is not S.QUEUED:
            raise QuotaExhausted(f"publication deferred: {reason}",
                                 retry_after_seconds=max(1.0, (until - now).total_seconds()),
                                 publication_id=str(b.pub.id), reason=reason)
        rng = self.ctx.rng("m8-defer", b.pub.id, reason, until.isoformat())
        at = fit_to_window(max(until, now), self.policy, rng)
        b.pub = await self._move(b, S.QUEUED, scheduled_at=at,
                                 error={"deferred": reason, "retry_at": at.isoformat(), **_jsonable(detail)})
        self._metric(b.pub.platform, outcome)
        log.info("publication_deferred", publication_id=str(b.pub.id), reason=reason, retry_at=at.isoformat())
        return b.pub

    async def _token_failure(self, b: _Loaded, exc: TokenRefreshFailed) -> Publication:
        """§10: refresh failure → account cooldown + alert; the publication waits for the cooldown."""
        async with self.ctx.uow() as repos:
            account = await repos.accounts.get(b.account.id)
            assert account is not None
            until = await self._enter_cooldown(repos, account, reason=f"token refresh failed ({exc.reason})",
                                               sanction=False)
            await self._audit(repos, "token_refresh_failed", f"account:{account.id}", reason=exc.reason,
                              message=str(exc))
        self._metric(b.pub.platform, "token_error")
        return await self._defer(b, until, "token_refresh_failed", "deferred_token")

    # ───────────────────────────── poll ─────────────────────────────

    async def poll(self, publication_id: uuid.UUID) -> Publication:
        """Advance a PROCESSING publication (container / upload / publish status)."""
        b = await self._load(publication_id)
        if b.pub.state is not S.PROCESSING:
            return b.pub
        client = self._client(b.pub.platform)
        try:
            token = await self.tokens.access_token(b.account)
        except TokenRefreshFailed as exc:
            async with self.ctx.uow() as repos:
                account = await repos.accounts.get(b.account.id)
                assert account is not None
                await self._enter_cooldown(repos, account, reason=f"token refresh failed ({exc.reason})",
                                           sanction=False)
            return b.pub
        req = await self._request(b, token, with_media=False)
        expires_at: datetime | None = None
        if b.pub.platform in EXPIRING_CONTAINERS:
            entered = await self._entered(b.pub.id, S.PROCESSING)
            if entered is not None:
                expires_at = entered + CONTAINER_TTL
        container = b.pub.platform_container_id
        return await self._drive(b, client, req, lambda: client.status(req, container), expires_at=expires_at)

    async def _entered(self, publication_id: uuid.UUID, state: S) -> datetime | None:
        q = (select(t.PublicationTransitionRow.at)
             .where(t.PublicationTransitionRow.publication_id == publication_id,
                    t.PublicationTransitionRow.to_state == state.value)
             .order_by(t.PublicationTransitionRow.at.desc()).limit(1))
        async with self.ctx.uow() as repos:
            at = (await repos.session.execute(q)).scalar_one_or_none()
        return ensure_utc(at) if at is not None else None

    # ───────────────────────────── saga compensation ─────────────────────────────

    async def take_down(self, publication_id: uuid.UUID, reason: str) -> Publication:
        """§5.2 compensation: hide/delete via the official API, or flag a manual takedown — never fake it."""
        b = await self._load(publication_id)
        pub = b.pub
        if pub.state is S.TAKEN_DOWN:
            return pub
        if pub.state is S.QUEUED:
            async with self.ctx.uow() as repos:
                b.pub = await self._transition_in(repos, pub, S.CANCELLED, workspace_id=b.account.workspace_id,
                                                  error={"code": "saga_compensation", "reason": reason})
                await self._audit(repos, "publication_cancelled", f"publication:{pub.id}", reason=reason)
            await self.quota.release(b.account, b.pub)
            return b.pub
        if pub.state is not S.PUBLISHED:
            # nothing live to remove yet (or a human holds it): flag it so neither poll nor operator proceeds blindly
            async with self.ctx.uow() as repos:
                b.pub = await repos.publications.transition(pub.id, pub.state, self._now(), error={
                    **(pub.error or {}), "takedown_requested": reason})
                await self._audit(repos, "takedown_requested", f"publication:{pub.id}", reason=reason,
                                  state=pub.state)
            return b.pub
        detail: dict[str, Any] = {}
        ok = False
        if pub.platform is not Platform.KAKAO:
            client = self._client(pub.platform)
            try:
                token = await self.tokens.access_token(b.account)
                ok = await client.hide_or_delete(pub.platform_post_id, token)
            except UpstreamError as exc:
                if exc.retryable:
                    raise
                detail = _error_json(exc)
            except (TokenRefreshFailed, PlatformWarning) as exc:
                detail = _error_json(exc)
        async with self.ctx.uow() as repos:
            if ok:
                b.pub = await self._transition_in(repos, pub, S.TAKEN_DOWN, workspace_id=b.account.workspace_id,
                                                  error={"takedown_reason": reason})
                await self._audit(repos, "publication_taken_down", f"publication:{pub.id}", reason=reason,
                                  post_id=pub.platform_post_id)
            else:
                b.pub = await repos.publications.transition(pub.id, S.PUBLISHED, self._now(), error={
                    "manual_takedown_required": True, "reason": reason, **({"detail": detail} if detail else {})})
                await self._audit(repos, "manual_takedown_required", f"publication:{pub.id}", reason=reason,
                                  platform=pub.platform, post_id=pub.platform_post_id, detail=detail)
        self._metric(pub.platform, "taken_down" if ok else "manual_takedown_required")
        return b.pub

    # ───────────────────────────── manual (Kakao) queue ─────────────────────────────

    async def manual_queue(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        """MANUAL_PENDING items for the console: caption, link and a fresh signed media URL to copy."""
        ttl = self.ctx.settings.objects.signed_url_ttl_seconds
        items: list[dict[str, Any]] = []
        async with self.ctx.uow() as repos:
            for account in await repos.accounts.list(workspace_id, platform=Platform.KAKAO.value):
                pubs = [p for p in await repos.publications.by_account(account.id, _EPOCH)
                        if p.state is S.MANUAL_PENDING]
                for p in sorted(pubs, key=lambda x: x.scheduled_at):
                    content = await repos.contents.get(p.content_id)
                    link = await repos.links.get(p.link_id)
                    media_url = None
                    if content is not None and content.render is not None:
                        uri = content.render.variants.get(p.platform.value) or content.render.media_uri
                        media_url = await self.ctx.objects.signed_url(uri, ttl)
                    items.append({
                        "publication_id": str(p.id), "content_id": str(p.content_id), "account_id": str(account.id),
                        "account_handle": account.handle, "platform": p.platform.value, "caption": p.caption,
                        "media_url": media_url, "link_url": link.short_url if link else None,
                        "scheduled_at": p.scheduled_at.isoformat(), "idempotency_key": p.idempotency_key,
                        "container_id": p.platform_container_id,
                    })
        return items

    async def complete_manual(self, publication_id: uuid.UUID, permalink: str, by: str) -> Publication:
        """Record that an operator posted a MANUAL_PENDING item by hand (idempotent)."""
        if not permalink.startswith(("https://", "http://")):
            raise ValidationFailed("permalink must be an http(s) URL")
        b = await self._load(publication_id)
        if b.pub.state is S.PUBLISHED:
            return b.pub
        now = self._now()
        async with self.ctx.uow() as repos:
            b.pub = await self._transition_in(repos, b.pub, S.PUBLISHED, workspace_id=b.account.workspace_id,
                                              platform_post_id=permalink, published_at=now, error=None)
            await repos.accounts.record_health(b.account.id, "attempt_ok", now,
                                               detail={"publication_id": str(b.pub.id), "manual": True})
            await repos.audit.write(AuditEntry(actor=by, action="manual_publication_completed",
                                               target=f"publication:{b.pub.id}",
                                               payload={"permalink": permalink}, at=now))
        self._metric(b.pub.platform, "manual_published")
        return b.pub

    # ───────────────────────────── scheduling & health ─────────────────────────────

    async def due(self, limit: int = 100) -> list[Publication]:
        """QUEUED publications whose ``scheduled_at`` has passed (oldest first)."""
        async with self.ctx.uow() as repos:
            return await repos.publications.due(self._now(), limit)

    async def evaluate_account(self, account_id: uuid.UUID) -> AccountStatus:
        """Lift an expired cooldown, or apply one when the 7-day health assessment says so (§7.1 #3)."""
        now = self._now()
        async with self.ctx.uow() as repos:
            account = await repos.accounts.get(account_id)
            if account is None:
                raise NotFound("channel account not found", account_id=str(account_id))
            if account.status is AccountStatus.COOLDOWN:
                if account.cooldown_until is not None and account.cooldown_until > now:
                    return AccountStatus.COOLDOWN
                await repos.accounts.set_status(account.id, AccountStatus.ACTIVE, None)
                await repos.accounts.record_health(account.id, "cooldown_lifted", now)
                await self.ctx.emit(repos, Event.of(
                    AccountStatusChanged(account_id=account.id, status=AccountStatus.ACTIVE.value,
                                         reason="cooldown expired"),
                    workspace_id=account.workspace_id, idempotency_key=f"{account.id}:active:{now.isoformat()}"))
                await self._audit(repos, "account_reactivated", f"account:{account.id}")
                await self._update_gauge(repos, account)
                return AccountStatus.ACTIVE
            if account.status is AccountStatus.RESTRICTED:
                return AccountStatus.RESTRICTED
            decision = await self._assess(repos, account, now)
            if decision.cooldown:
                await self._enter_cooldown(repos, account, reason="; ".join(decision.reasons), sanction=False)
                return AccountStatus.COOLDOWN
            return AccountStatus.ACTIVE

    async def _assess(self, repos: Repos, account: ChannelAccount, now: datetime) -> HealthDecision:
        """``quant.risk.assess_account`` over the last 7 days; attempt/warning counts restart at the last lift."""
        events = await repos.accounts.health_events(account.id, now - HEALTH_LOOKBACK)
        lifts = [ensure_utc(e.at) for e in events if e.kind == "cooldown_lifted"]
        since = max(lifts) if lifts else None
        counted = [e for e in events if since is None or ensure_utc(e.at) >= since]
        attempts = sum(1 for e in counted if e.kind in ("attempt_ok", "attempt_fail"))
        failures = sum(1 for e in counted if e.kind == "attempt_fail")
        warnings = sum(1 for e in counted if e.kind == "warning")
        reach = [float(e.value) for e in events if e.kind == "reach"]
        return assess_account(attempts=attempts, failures=failures, current_reach=reach[-1] if reach else None,
                              trailing_reach=reach[:-1], warnings=warnings,
                              threshold=self.policy.failure_rate_threshold,
                              confidence=self.policy.failure_confidence,
                              reach_threshold=self.policy.reach_drop_threshold)

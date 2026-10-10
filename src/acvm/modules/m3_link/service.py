"""M3 Link Service (§4 M3, §9.5 #4, §10 링크 무결성).

* ``create_link`` — one tracked link per publication: key = content_id + platform + account (§4 M8), subId
  derived from that key (§6 "게시물 1건 = subId 1개"), deeplink API call, partner-tag check *before*
  anything is stored, HMAC signature over (sub_id | landing | short | partner_id), append-only insert.
  Re-running with the same inputs returns the stored row without a second API call.
* ``verify`` — §10 "게시 직전 딥링크의 파트너 ID를 서명 검증해 바꿔치기 차단": signature, Coupang hosts and
  partner tag/subId; every violation is audited and raised as ``IntegrityViolation`` (never retried).
* ``health_sweep`` — §4 M3 링크 헬스체커 (every 6 h): sold-out / discontinued detection, health update and
  ``link.health_changed`` with a same-category replacement suggestion (§9.5 #4 대체 상품 제안); stops
  cleanly when the Coupang budget or circuit blocks, leaving the rest for the next sweep.
* ``build_hub_page`` — the account's link hub with the disclosure fixed on top (D-04), stored in the object
  store and indexed in ``kv_cache`` (``hub:{account_id}``) so the M7 gate can verify it without importing M3.
* ``resolve_click`` / ``on_invalid_click`` — the ``/r/{code}`` redirect with salted-hash click logging
  (§4 M9 무효 클릭 감시 input) and the 24-hour pause that M9's ``invalid_click.detected`` triggers: a paused
  link redirects to the untagged product page, so suspicious traffic earns nothing (§8.2 무효클릭 0 정책).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import IntegrityError

from acvm.core.clock import to_kst
from acvm.core.enums import LinkHealth, Platform
from acvm.core.errors import (
    CircuitOpen,
    IntegrityViolation,
    NotFound,
    QuotaExhausted,
    UpstreamError,
    ValidationFailed,
)
from acvm.core.events import Event, EventBus, InvalidClickDetected, LinkCreated, LinkHealthChanged
from acvm.core.ids import publication_key, short_code, sub_id_for
from acvm.core.logging import get_logger
from acvm.domain.models import AffiliateLink, Product
from acvm.infra.coupang import CoupangClient, canonical_product_url
from acvm.infra.coupang_sandbox import coupang_client_for
from acvm.modules.m3_link import queries
from acvm.modules.m3_link.hub import HubCard, render_hub
from acvm.ports import ClickRow
from acvm.security.audit import audit
from acvm.security.linksign import hash_ip, sign_link, verify_partner_tag, verify_short_url, verify_signature

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

HEALTH_INTERVAL: Final = timedelta(hours=6)
PAUSE_TTL: Final = timedelta(hours=24)
HUB_RECORD_TTL: Final = timedelta(days=30)
MASS_STOCKOUT_MIN: Final = 5
MASS_STOCKOUT_SHARE: Final = 0.20
_HUB_CODE: Final = re.compile(r"^[a-z2-7]{4,16}$")
_CTRL: Final = re.compile(r"[\x00-\x1f\x7f]")


def pause_key(link_id: uuid.UUID) -> str:
    """kv_cache key holding a link's invalid-click pause (present ⇒ paused)."""
    return f"link:paused:{link_id}"


def hub_record_key(account_id: uuid.UUID) -> str:
    """kv_cache key with the latest hub build {uri, sha256, built_at, …} for gate rule D-04."""
    return f"hub:{account_id}"


class LinkService:
    """M3 façade. ``client`` defaults to ``ctx.extras["coupang"]`` (built on first use)."""

    actor: Final = "M3"

    def __init__(self, ctx: AppContext, *, client: CoupangClient | None = None) -> None:
        self.ctx = ctx
        self._client = client

    @property
    def client(self) -> CoupangClient:
        return self._client if self._client is not None else coupang_client_for(self.ctx)

    def register(self, bus: EventBus) -> None:
        """Subscribe M3's consumers (``invalid_click.detected`` → pause)."""
        bus.subscribe(InvalidClickDetected.TYPE, "M3", self.on_invalid_click)

    # ───────────────────────── creation & integrity ─────────────────────────

    async def create_link(self, *, product: Product, content_id: uuid.UUID, platform: Platform,
                          account_id: uuid.UUID) -> AffiliateLink:
        """Create (or return the existing) tracked link for one publication."""
        key = publication_key(content_id, platform.value, account_id)
        async with self.ctx.uow() as repos:
            existing = await repos.links.get_by_publication_key(key)
        if existing is not None:
            return existing

        sub_id = sub_id_for(key)
        results = await self.client.deeplink([canonical_product_url(product.coupang_product_id)], sub_id)
        if not results:
            raise UpstreamError("deeplink API returned no link", retryable=True, sub_id=sub_id)
        res = results[0]
        partner_id = self._partner_id()
        try:
            verify_short_url(res.shorten_url)
            verify_partner_tag(res.landing_url, partner_id, sub_id)
        except IntegrityViolation as exc:
            await self._audit_violation(None, sub_id, f"deeplink API response rejected: {exc}")
            raise

        now = self.ctx.clock.now()
        link = AffiliateLink(
            product_id=product.id, sub_id=sub_id, deeplink=res.landing_url, short_url=res.shorten_url,
            hub_code=short_code(key), health=LinkHealth.OK, checked_at=now,
            signature=sign_link(await self._signing_key(), sub_id, res.landing_url, res.shorten_url, partner_id),
            publication_key=key, created_at=now)
        try:
            async with self.ctx.uow() as repos:
                stored = await repos.links.add(link)
                if stored.id == link.id:
                    account = await repos.accounts.get(account_id)
                    await audit(repos, self.ctx.clock, self.actor, "link.created", f"link:{link.id}",
                                sub_id=sub_id, product_id=product.id, platform=platform.value,
                                content_id=str(content_id))
                    ev = Event.of(LinkCreated(link_id=link.id, sub_id=sub_id, product_id=product.id),
                                  workspace_id=account.workspace_id if account else None,
                                  idempotency_key=f"{key}:link.created")
                    ev.occurred_at = now
                    await self.ctx.emit(repos, ev)
        except IntegrityError:
            # a concurrent worker inserted the same publication's link first — converge on it
            async with self.ctx.uow() as repos:
                stored_again = await repos.links.get_by_publication_key(key)
            if stored_again is None:
                raise
            return stored_again
        if stored.id == link.id:
            log.info("m3_link_created", link_id=str(link.id), sub_id=sub_id, product_id=product.id)
        return stored

    async def verify(self, link: AffiliateLink) -> None:
        """Raise ``IntegrityViolation`` unless the stored link is untampered and carries our tags (§10)."""
        try:
            partner_id = self._partner_id()
            verify_signature(await self._signing_key(), link.signature, link.sub_id, link.deeplink,
                             link.short_url, partner_id)
            verify_short_url(link.short_url)
            verify_partner_tag(link.deeplink, partner_id, link.sub_id)
        except IntegrityViolation as exc:
            await self._audit_violation(link.id, link.sub_id, str(exc))
            raise

    def _partner_id(self) -> str:
        """Configured partner ID. Empty is tolerated only in the sandbox (no real tag to compare)."""
        cfg = self.ctx.settings.coupang
        if not cfg.partner_id and not cfg.sandbox:
            raise IntegrityViolation("coupang.partner_id is not configured; links cannot be verified")
        return cfg.partner_id

    async def _signing_key(self) -> bytes:
        return (await self.ctx.secrets.get(self.ctx.settings.security.link_signing_key_ref)).encode("utf-8")

    async def _audit_violation(self, link_id: uuid.UUID | None, sub_id: str, reason: str) -> None:
        log.error("m3_link_integrity_violation", link_id=str(link_id) if link_id else None, sub_id=sub_id,
                  reason=reason)
        async with self.ctx.uow() as repos:
            await audit(repos, self.ctx.clock, self.actor, "link.integrity_violation",
                        f"link:{link_id}" if link_id else f"sub_id:{sub_id}", sub_id=sub_id, reason=reason)

    # ───────────────────────── health checker ─────────────────────────

    async def health_sweep(self, limit: int = 500) -> list[LinkHealthChanged]:
        """Check links not checked for 6 h; emit ``link.health_changed`` for every transition."""
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            due = await repos.links.due_for_check(now - HEALTH_INTERVAL, limit)
            by_product: dict[int, list[AffiliateLink]] = {}
            for link in due:
                by_product.setdefault(link.product_id, []).append(link)
            products = {pid: await repos.products.get(pid) for pid in by_product}

        observed: dict[int, LinkHealth] = {}
        for pid in sorted(by_product):
            product = products.get(pid)
            try:
                observed[pid] = await self.client.availability(pid, product.name if product else "")
            except (QuotaExhausted, CircuitOpen) as exc:
                log.warning("m3_health_sweep_paused", reason=exc.code, checked=len(observed),
                            remaining=len(by_product) - len(observed), retry_after=exc.retry_after_seconds)
                break
            except (UpstreamError, ValidationFailed) as exc:
                log.warning("m3_health_check_failed", product_id=pid, reason=exc.code)

        changes: list[LinkHealthChanged] = []
        newly_bad = {pid for pid, h in observed.items() if h is not LinkHealth.OK}
        async with self.ctx.uow() as repos:
            for pid, health in observed.items():
                for link in by_product[pid]:
                    await repos.links.set_health(link.id, health.value, now)
            replacements: dict[int, int | None] = {}
            for pid, health in sorted(observed.items()):
                transitions = [link for link in by_product[pid] if link.health is not health]
                if not transitions:
                    continue
                product = products.get(pid)
                if health is not LinkHealth.OK and product is not None and pid not in replacements:
                    replacements[pid] = await queries.best_replacement(repos, product.category_id,
                                                                       exclude=newly_bad | {pid})
                for link in transitions:
                    payload = LinkHealthChanged(link_id=link.id, health=health.value,
                                                replacement_product_id=replacements.get(pid)
                                                if health is not LinkHealth.OK else None)
                    ev = Event.of(payload, workspace_id=await queries.workspace_for_link(repos, link.id),
                                  idempotency_key=f"{link.id}:health:{health.value}:{now.isoformat()}")
                    ev.occurred_at = now
                    await self.ctx.emit(repos, ev)
                    changes.append(payload)
            if changes:
                await audit(repos, self.ctx.clock, self.actor, "link.health_sweep", "affiliate_link",
                            checked_products=len(observed), changed_links=len(changes),
                            transitions=[{"link_id": str(c.link_id), "health": c.health,
                                          "replacement_product_id": c.replacement_product_id} for c in changes])
        bad = sum(1 for c in changes if c.health != LinkHealth.OK.value)
        if bad >= MASS_STOCKOUT_MIN and bad >= MASS_STOCKOUT_SHARE * max(1, sum(map(len, by_product.values()))):
            log.warning("m3_mass_stockout", unhealthy_links=bad, checked_links=len(due))  # §9.5 #4
        log.info("m3_health_sweep_done", due=len(due), checked_products=len(observed), changed=len(changes))
        return changes

    # ───────────────────────── link hub ─────────────────────────

    async def build_hub_page(self, account_id: uuid.UUID) -> str:
        """Render, store and index the account's link-hub page; returns the HTML."""
        settings = self.ctx.settings
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            account = await repos.accounts.get(account_id)
            if account is None:
                raise NotFound("channel account not found", account_id=str(account_id))
            entries = await queries.hub_entries(repos, account_id)
        base = settings.link_hub_base_url.rstrip("/")
        cards = [HubCard(name=e.product.name, image_url=e.product.image_url, price_text=f"{e.product.price:,}원",
                         as_of=to_kst(e.product.price_checked_at or e.product.last_seen_at).date().isoformat(),
                         href=f"{base}/r/{e.link.hub_code}") for e in entries]
        html = render_hub(handle=account.handle, disclosure=settings.gate.standard_disclosure, cards=cards)
        data = html.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        uri = await self.ctx.objects.put_bytes(data, f"hub/{_safe_handle(account.handle, account_id)}.html",
                                               "text/html; charset=utf-8")
        async with self.ctx.uow() as repos:
            await repos.kv.put(hub_record_key(account_id), {
                "uri": uri, "sha256": digest, "built_at": now.isoformat(), "account_handle": account.handle,
                "link_count": len(cards), "hub_codes": [e.link.hub_code for e in entries],
                "disclosure": settings.gate.standard_disclosure,
            }, HUB_RECORD_TTL, now)
        log.info("m3_hub_built", account_id=str(account_id), cards=len(cards), sha256=digest)
        return html

    # ───────────────────────── redirect & invalid clicks ─────────────────────────

    async def resolve_click(self, code: str, ip: str, user_agent: str, referer: str = "") -> str:
        """Redirect target for ``/r/{code}``; every click is logged with salted hashes, never raw IP/UA."""
        normalized = code.strip().lower()
        if not _HUB_CODE.match(normalized):
            raise NotFound("unknown link code")
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            link = await repos.links.by_hub_code(normalized)
            if link is None:
                raise NotFound("unknown link code", code=normalized)
            paused = await repos.kv.get(pause_key(link.id), now) is not None
            product = await repos.products.get(link.product_id)
        salt = (await self.ctx.secrets.get(self.ctx.settings.security.click_ip_salt_ref)).encode("utf-8")
        await self.ctx.perf.insert_clicks([ClickRow(
            ts=now, hub_code=normalized, sub_id=link.sub_id, ip_hash=hash_ip(ip, salt),
            ua_hash=hash_ip(user_agent, salt), referer=_CTRL.sub("", referer)[:512])])
        plain = canonical_product_url(product.coupang_product_id if product else link.product_id)
        if paused:
            return plain
        try:
            await self.verify(link)
        except IntegrityViolation:
            return plain  # never send a visitor through a link that failed verification
        return link.short_url

    async def on_invalid_click(self, event: Event) -> None:
        """``invalid_click.detected`` (M9) → pause the link for 24 h and audit it (§4 M9, §8.2)."""
        payload = event.parsed()
        if not isinstance(payload, InvalidClickDetected):
            raise ValidationFailed("on_invalid_click expects invalid_click.detected", type=event.type)
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            link = (await repos.links.get(payload.link_id) if payload.link_id is not None
                    else await repos.links.by_sub_id(payload.sub_id))
            if link is None:
                log.warning("m3_invalid_click_unknown_link", sub_id=payload.sub_id)
                return
            until = now + PAUSE_TTL
            await repos.kv.put(pause_key(link.id), {
                "reason": payload.reason, "score": payload.score, "sub_id": link.sub_id,
                "paused_at": now.isoformat(), "until": until.isoformat(), "event_id": str(event.id),
            }, PAUSE_TTL, now)
            await audit(repos, self.ctx.clock, self.actor, "link.paused", f"link:{link.id}", sub_id=link.sub_id,
                        reason=payload.reason, score=payload.score, until=until.isoformat())
        log.warning("m3_link_paused", link_id=str(link.id), reason=payload.reason, until=until.isoformat())


def _safe_handle(handle: str, account_id: uuid.UUID) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", handle).strip("._")
    return safe or str(account_id)


"""M3 link creation, idempotency and integrity verification (§4 M3, §10 링크 무결성)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from acvm.context import AppContext
from acvm.core.enums import LinkHealth, Platform
from acvm.core.errors import IntegrityViolation
from acvm.core.events import LinkCreated
from acvm.core.ids import publication_key, short_code, sub_id_for
from acvm.domain.models import AffiliateLink, ChannelAccount, Product
from acvm.infra.coupang import API_PREFIX, CoupangPartnersClient
from acvm.infra.coupang_sandbox import SandboxCoupangClient, coupang_client_for
from acvm.modules.m3_link import LinkService, placement_for
from acvm.security.linksign import sign_link


@pytest.fixture
async def stored_product(ctx: AppContext, product_factory) -> Product:  # type: ignore[no-untyped-def]
    product: Product = product_factory(7101300001)
    async with ctx.uow() as repos:
        await repos.products.upsert(product)
    return product


def test_placement_policy_matches_design_table() -> None:
    assert placement_for(Platform.THREADS) == "body"
    assert placement_for(Platform.THREADS, threads_first_comment=True) == "first_comment"
    assert placement_for(Platform.INSTAGRAM) == "hub"
    assert placement_for(Platform.TIKTOK) == "hub"
    assert placement_for(Platform.YOUTUBE) == "hub"
    assert placement_for(Platform.FACEBOOK) == "caption_first_line"
    assert placement_for(Platform.FACEBOOK, threads_first_comment=True) == "caption_first_line"


async def test_create_link_is_idempotent_and_calls_deeplink_once(
        ctx: AppContext, stored_product: Product, accounts: dict[Platform, ChannelAccount]) -> None:
    svc = LinkService(ctx)
    content_id = uuid.uuid4()
    acc = accounts[Platform.THREADS]
    link = await svc.create_link(product=stored_product, content_id=content_id, platform=Platform.THREADS,
                                 account_id=acc.id)
    again = await svc.create_link(product=stored_product, content_id=content_id, platform=Platform.THREADS,
                                  account_id=acc.id)
    sandbox = coupang_client_for(ctx)
    assert isinstance(sandbox, SandboxCoupangClient)
    assert sandbox.transport_calls["deeplink"] == 1
    assert again == link

    key = publication_key(content_id, "threads", acc.id)
    assert link.publication_key == key and link.sub_id == sub_id_for(key) and link.hub_code == short_code(key)
    assert link.health is LinkHealth.OK and link.checked_at == ctx.clock.now()
    assert link.short_url.startswith("https://link.coupang.com/a/")
    assert f"subid={link.sub_id}" in link.deeplink and "lptag=AF0000000" in link.deeplink
    await svc.verify(link)  # untampered sandbox link verifies (partner equality skipped: sandbox, no ID)

    async with ctx.uow() as repos:
        events = [e for e in await repos.outbox.pending(100) if e.type == LinkCreated.TYPE]
        audit = await repos.audit.recent(action="link.created")
    assert len(events) == 1 and events[0].idempotency_key == f"{key}:link.created"
    assert events[0].workspace_id == acc.workspace_id
    assert events[0].parsed() == LinkCreated(link_id=link.id, sub_id=link.sub_id, product_id=stored_product.id)
    assert len(audit) == 1

    other = await svc.create_link(product=stored_product, content_id=content_id, platform=Platform.INSTAGRAM,
                                  account_id=accounts[Platform.INSTAGRAM].id)
    assert other.sub_id != link.sub_id  # one subId per publication (§6)
    assert sandbox.transport_calls["deeplink"] == 2


async def test_tampered_link_fails_verification_and_is_audited(
        ctx: AppContext, stored_product: Product, accounts: dict[Platform, ChannelAccount]) -> None:
    svc = LinkService(ctx)
    link = await svc.create_link(product=stored_product, content_id=uuid.uuid4(), platform=Platform.THREADS,
                                 account_id=accounts[Platform.THREADS].id)
    swapped = link.model_copy(update={"deeplink": link.deeplink.replace("AF0000000", "AF6666666")})
    with pytest.raises(IntegrityViolation):
        await svc.verify(swapped)
    with pytest.raises(IntegrityViolation):
        await svc.verify(link.model_copy(update={"short_url": "https://link.coupang.com/a/attacker1"}))
    with pytest.raises(IntegrityViolation):
        await svc.verify(link.model_copy(update={"sub_id": "aother000000000"}))
    async with ctx.uow() as repos:
        violations = await repos.audit.recent(action="link.integrity_violation")
    assert len(violations) == 3


async def test_partner_tag_mismatch_is_detected_even_with_a_valid_signature(ctx: AppContext) -> None:
    ctx.settings.coupang.partner_id = "AF1234567"
    key = (await ctx.secrets.get(ctx.settings.security.link_signing_key_ref)).encode()
    landing = "https://link.coupang.com/re/AFFSDP?lptag=AF9999999&subid=abcdabcdabcdabcd&pageKey=1"
    short = "https://link.coupang.com/a/abcdefgh"
    link = AffiliateLink(product_id=1, sub_id="abcdabcdabcdabcd", deeplink=landing, short_url=short,
                         hub_code="abcdefgh", signature=sign_link(key, "abcdabcdabcdabcd", landing, short, "AF1234567"),
                         publication_key="k", created_at=ctx.clock.now())
    with pytest.raises(IntegrityViolation, match="partner tag mismatch"):
        await LinkService(ctx).verify(link)


async def test_production_without_partner_id_refuses_to_verify(ctx: AppContext) -> None:
    ctx.settings.coupang.sandbox = False
    ctx.settings.coupang.partner_id = ""
    link = AffiliateLink(product_id=1, sub_id="s", deeplink="https://link.coupang.com/re/x", short_url="https://link.coupang.com/a/x",
                         hub_code="abcd", signature="0" * 64, publication_key="k", created_at=ctx.clock.now())
    with pytest.raises(IntegrityViolation, match="partner_id"):
        await LinkService(ctx).verify(link)


@respx.mock
async def test_create_link_rejects_a_deeplink_with_someone_elses_partner_tag(
        ctx: AppContext, stored_product: Product, accounts: dict[Platform, ChannelAccount]) -> None:
    ctx.settings.coupang.sandbox = False
    ctx.settings.coupang.partner_id = "AF1234567"
    respx.post(f"https://api-gateway.coupang.com{API_PREFIX}/deeplink").mock(return_value=httpx.Response(200, json={
        "rCode": "0", "rMessage": "", "data": [{
            "originalUrl": "https://www.coupang.com/vp/products/7101300001",
            "shortenUrl": "https://link.coupang.com/a/hijack1",
            "landingUrl": "https://link.coupang.com/re/AFFSDP?lptag=AF6666666&pageKey=7101300001"}]}))
    client = CoupangPartnersClient(ctx.settings, ctx.secrets, ctx.quota, ctx.clock, ctx.uow, http=httpx.AsyncClient())
    svc = LinkService(ctx, client=client)
    content_id = uuid.uuid4()
    with pytest.raises(IntegrityViolation, match="partner tag mismatch"):
        await svc.create_link(product=stored_product, content_id=content_id, platform=Platform.THREADS,
                              account_id=accounts[Platform.THREADS].id)
    async with ctx.uow() as repos:
        key = publication_key(content_id, "threads", accounts[Platform.THREADS].id)
        assert await repos.links.get_by_publication_key(key) is None  # nothing persisted
        assert len(await repos.audit.recent(action="link.integrity_violation")) == 1

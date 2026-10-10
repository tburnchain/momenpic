from __future__ import annotations

import asyncio

import httpx
import pytest

from acvm.cli import NICHES, seed_workspace
from acvm.core.enums import Platform
from acvm.policy.facts import build_facts
from acvm.policy.lint import auto_ground, lint_text

# ───────────────────────── policy lint ─────────────────────────


def test_auto_ground_maps_numbers_to_facts(product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    facts = build_facts(p, p.last_seen_at, 360)
    claim, _ = auto_ground("리뷰가 3,812개나 달렸어요", facts)
    assert claim is not None and claim.evidence == ["review_count"] and claim.grounded
    claim, _ = auto_ground("20개입이라 넉넉해요", facts)
    assert claim is not None and claim.evidence == ["spec.count"]
    claim, _ = auto_ground("평점 4.7점에 로켓배송이에요", facts)
    assert claim is not None and set(claim.evidence) == {"rating", "is_rocket"}
    claim, reason = auto_ground("리뷰가 9,999개예요", facts)
    assert claim is None and reason


def test_lint_flags_banned_terms_and_ungrounded_numbers(product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    facts = build_facts(p, p.last_seen_at, 360)
    res = lint_text("무조건 사야 하는 최고의 클립이에요. 면역력에도 좋아요. 판매량 50만 개 돌파! 오늘만 30% 할인. 리뷰 3,812개", facts)
    kinds = {i.kind for i in res.issues}
    assert {"superlative", "efficacy", "ungrounded", "discount"} <= kinds
    assert any(i.term == "최고의" and i.suggestion == "만족도 높은" for i in res.issues)
    assert not res.ok
    assert [c.evidence for c in res.claims] == [["review_count"]]
    stale = build_facts(p, p.last_seen_at.replace(hour=23), 360)
    assert any(i.kind == "stale_price" for i in lint_text("이 구성이 12,900원이에요", stale).issues)


# ───────────────────────── Studio API ─────────────────────────


@pytest.fixture
async def api(ctx, product_factory):  # type: ignore[no-untyped-def]
    from acvm.api.app import create_app
    from acvm.infra.platforms.registry import build_platform_clients

    for _, (handle, _) in NICHES.items():
        for p in Platform:
            await ctx.secrets.put(f"tokens/{p.value}/{handle}", "sandbox-token")
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    ctx.settings.gate.new_template_hold_count = 0
    async with ctx.uow() as r:
        await r.products.upsert(product_factory())
        await r.products.upsert(product_factory(2002, category="1024", name="종합 비타민 영양제", regulated=True))
    app = create_app(ctx.settings, ctx=ctx)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acvm.test", timeout=120) as c):
        c.headers["Authorization"] = f"Bearer {ctx.settings.security.dev_api_token}"
        yield c, ctx, ws_id, app


async def _accounts(c, platform: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    opts = (await c.get("/api/v1/studio/options")).json()
    return next(a for a in opts["accounts"] if a["platform"] == platform and a["niche"] == "kitchen")


async def test_options_and_products(api) -> None:  # type: ignore[no-untyped-def]
    c, *_ = api
    opts = (await c.get("/api/v1/studio/options")).json()
    assert opts["disclosure"]["locked"] is True and opts["caption_positions"]
    assert {t["id"] for t in opts["templates"]} >= {"problem-solution-v3", "text-v1"}
    lim = opts["limits"]  # 20–25 s pitches, fast voices only
    assert (lim["min_seconds"], lim["max_seconds"]) == (20.0, 25.0) and lim["syllables_per_second"] >= 7.5
    assert opts["voices"] and all(v["rate"] >= 1.2 for v in opts["voices"])
    prods = (await c.get("/api/v1/studio/products", params={"category": "1013"})).json()["items"]
    assert [x["id"] for x in prods] == [1001]  # regulated product never offered
    assert (await c.get("/api/v1/studio/products", params={"category": "1013", "refresh": True},
                        headers={"X-Dev-Role": "viewer"})).status_code == 403


async def test_text_draft_edit_lint_and_publish(api) -> None:  # type: ignore[no-untyped-def]
    c, _ctx, _ws_id, _app = api
    acc = await _accounts(c, "threads")
    r = await c.post("/api/v1/studio/drafts", json={"product_id": 1001, "kind": "text", "account_id": acc["id"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "text" and d["body"] and d["render_options"]["watermark"] == f"@{acc['handle']}"
    assert d["lint"]["ok"] is True, d["lint"]

    edited = d["body"] + "\n✔ 평점 4.7점이라 믿고 사요\n✔ 최고의 선택이에요"
    r = await c.put(f"/api/v1/studio/drafts/{d['id']}", json={"body": edited})
    d2 = r.json()
    assert any(i["kind"] == "superlative" for i in d2["lint"]["issues"])
    assert any(cl["evidence"] == ["rating"] for cl in d2["claims"])  # auto-grounded edit

    pub = (await c.post(f"/api/v1/studio/drafts/{d['id']}/publish", json={})).json()
    assert pub["verdict"] == "PASS", pub
    assert pub["publications"] and pub["publications"][0]["platform"] == "threads"
    final = (await c.get(f"/api/v1/studio/drafts/{d['id']}")).json()
    assert final["status"] == "approved"
    assert "최고" not in final["body"] and final["body"].split("\n")[0].startswith("이 포스팅은")
    assert (await c.put(f"/api/v1/studio/drafts/{d['id']}", json={"caption": "x"})).status_code == 422


async def test_regulated_product_and_text_on_video_platform_refused(api) -> None:  # type: ignore[no-untyped-def]
    c, *_ = api
    assert (await c.post("/api/v1/studio/drafts", json={"product_id": 2002})).status_code == 403
    r = await c.post("/api/v1/studio/drafts", json={"product_id": 1001, "kind": "text", "platform": "instagram"})
    assert r.status_code == 422


@pytest.mark.render
async def test_short_draft_render_then_auto_publish(api) -> None:  # type: ignore[no-untyped-def]
    c, _ctx, _ws_id, _app = api
    acc = await _accounts(c, "instagram")
    d = (await c.post("/api/v1/studio/drafts", json={"product_id": 1001, "account_id": acc["id"]})).json()
    assert d["kind"] == "short" and len(d["beats"]) == 5 and len(d["hooks"]) == 3
    assert (await c.post(f"/api/v1/studio/drafts/{d['id']}/render")).json()["state"] == "rendering"
    for _ in range(240):
        st = (await c.get(f"/api/v1/studio/drafts/{d['id']}")).json()
        if st["render"].get("state") in ("done", "failed"):
            break
        await asyncio.sleep(0.5)
    assert st["render"]["state"] == "done", st["render"]
    assert st["render"]["media_url"] and 20 <= st["render"]["duration"] <= 25.05
    threads = await _accounts(c, "threads")
    pub = (await c.post(f"/api/v1/studio/drafts/{d['id']}/publish",
                        json={"targets": [{"account_id": acc["id"]}, {"account_id": threads["id"]}]})).json()
    assert pub["verdict"] == "PASS", pub["findings"]
    assert {x["platform"] for x in pub["publications"]} == {"instagram", "threads"}


async def test_short_length_is_linted_and_enforced(api) -> None:  # type: ignore[no-untyped-def]
    c, _ctx, _ws_id, _app = api
    acc = await _accounts(c, "instagram")
    d = (await c.post("/api/v1/studio/drafts", json={"product_id": 1001, "account_id": acc["id"]})).json()
    dur = d["lint"]["duration"]
    assert (dur["min_seconds"], dur["max_seconds"]) == (20.0, 25.0) and dur["estimated_seconds"] <= 25.0
    beats: list[dict[str, str] | None] = [None] * len(d["beats"])
    beats[1] = {"narration": " ".join(["봉지 입구를 단단하게 잡아줘서 과자가 눅눅해지지 않아요."] * 8)}
    d2 = (await c.put(f"/api/v1/studio/drafts/{d['id']}", json={"beats": beats})).json()
    issue = next(i for i in d2["lint"]["issues"] if i["kind"] == "too_long")
    assert issue["severity"] == "error" and d2["lint"]["ok"] is False
    assert d2["lint"]["duration"]["estimated_seconds"] > 25.0
    live = (await c.post("/api/v1/studio/lint", json={"product_id": 1001, "text": "x",  # editor's live check
                                                      "narrations": [b["narration"] for b in d2["beats"]]})).json()
    assert live["ok"] is False and any(i["kind"] == "too_long" for i in live["issues"])
    assert "duration" not in (await c.post("/api/v1/studio/lint", json={"product_id": 1001, "text": "x"})).json()
    r = await c.post(f"/api/v1/studio/drafts/{d['id']}/render")  # refused up front, not as a failed render
    assert r.status_code == 422 and "너무 깁니다" in r.json()["message"]
    assert (await c.get(f"/api/v1/studio/drafts/{d['id']}")).json()["render"].get("state") != "rendering"


async def test_auto_text_end_to_end(api) -> None:  # type: ignore[no-untyped-def]
    c, *_ = api
    acc = await _accounts(c, "threads")
    res = (await c.post("/api/v1/studio/auto", json={"product_id": 1001, "kind": "text",
                                                     "targets": [{"account_id": acc["id"]}]})).json()
    assert res["verdict"] == "PASS" and res["publications"] and res["content_id"]


async def test_upload_requires_attestation_and_transcribe_only_own_footage(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, *_ = api
    r = await c.post("/api/v1/studio/assets", params={"rights": "own_footage", "attest": False}, content=b"x",
                     headers={"Content-Type": "video/mp4"})
    assert r.status_code == 422
    r = await c.post("/api/v1/studio/assets", params={"rights": "own_footage", "attest": True}, content=b"x",
                     headers={"Content-Type": "application/zip"})
    assert r.status_code == 415
    from acvm.core.enums import AssetSource
    from acvm.domain.models import Asset

    stock = Asset(kind="video", uri="file://x.mp4", sha256="f" * 64, rights_source=AssetSource.LICENSED_STOCK,
                  license="stock")
    async with ctx.uow() as repos:
        stock = await repos.assets.register(stock)
    assert (await c.post(f"/api/v1/studio/assets/{stock.id}/transcribe")).status_code == 403


async def test_targets_survive_hold_and_human_approval(api) -> None:  # type: ignore[no-untyped-def]
    """Studio publish → HOLD (new template) → reviewer approves → BOTH chosen targets get scheduled."""
    c, ctx, _, _app = api
    ctx.settings.gate.new_template_hold_count = 10
    threads = await _accounts(c, "threads")
    d = (await c.post("/api/v1/studio/drafts", json={"product_id": 1001, "kind": "text",
                                                      "account_id": threads["id"]})).json()
    car_threads = next(a for a in (await c.get("/api/v1/studio/options")).json()["accounts"]
                       if a["platform"] == "threads" and a["niche"] == "car")
    dup = await c.post(f"/api/v1/studio/drafts/{d['id']}/publish",
                       json={"targets": [{"account_id": threads["id"]}, {"account_id": car_threads["id"]}]})
    assert dup.status_code == 422 and "7.1" in dup.text  # same content to two accounts of one platform: refused
    kakao = await _accounts(c, "kakao")
    res = (await c.post(f"/api/v1/studio/drafts/{d['id']}/publish",
                        json={"targets": [{"account_id": threads["id"]}, {"account_id": kakao["id"]}]})).json()
    assert res["verdict"] == "HOLD" and res["publications"] == []
    item = next(h for h in (await c.get("/api/v1/holds")).json()["items"] if h["content_id"] == d["id"])
    assert (await c.post(f"/api/v1/holds/{item['id']}/decision", json={"approve": True})).status_code == 200
    final = (await c.get(f"/api/v1/studio/drafts/{d['id']}")).json()
    assert final["status"] == "approved"
    assert {p["platform"] for p in final["publications"]} == {"threads", "kakao"}


def test_claim_coverage_normalisation() -> None:
    from acvm.policy.lint import is_covered

    assert is_covered("평점은 4.5점", ["평점은 4.5점이에요."])
    assert is_covered("평점은 4.5점이에요", ["평점은 4.5점이에요."])
    assert not is_covered("리뷰가 6,230개", ["평점은 4.5점이에요."])


async def test_empty_catalog_is_loaded_once_on_first_view(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _ws_id, _app = api
    from acvm.db import tables as t

    async with ctx.uow() as r:  # a fresh install: nothing stored for this category yet
        await r.session.execute(t.ProductRow.__table__.delete().where(t.ProductRow.category_id == "1018"))
    first = (await c.get("/api/v1/studio/products", params={"category": "1018"})).json()["items"]
    assert first, "a fresh category is loaded from the catalog on first view"
    async with ctx.uow() as r:
        assert await r.kv.get("studio:catalog_autoload:1018", ctx.clock.now())
    calls: list[str] = []
    orig = _app.state.studio.p.products.select if hasattr(_app.state, "studio") else None
    if orig is not None:
        async def spy(**kw):  # type: ignore[no-untyped-def]
            calls.append(kw["category_id"])
            return await orig(**kw)
        _app.state.studio.p.products.select = spy
    assert (await c.get("/api/v1/studio/products", params={"category": "1018"})).json()["items"]
    assert calls == []  # stored products are served without spending the Coupang budget again

"""End-to-end: one command through every module, on sandbox Coupang + sandbox platforms.

text path (fast, always runs):  command → plan → M1 → M4 → M7 → M3 → M8 plan → publish → M9 → M10
short path (@render, ffmpeg):   + M5 assets → M6 render (real FFmpeg, locked disclosure) → D-03 frame check
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from acvm.cli import NICHES, seed_workspace
from acvm.context import AppContext
from acvm.core.enums import ContentStatus, Platform, PublicationState, RunStatus
from acvm.orchestration.local import LocalOrchestrator


@pytest.fixture
async def env(ctx: AppContext):  # type: ignore[no-untyped-def]
    from acvm.infra.platforms.registry import build_platform_clients

    for _, (handle, _) in NICHES.items():
        for p in Platform:
            await ctx.secrets.put(f"tokens/{p.value}/{handle}", "sandbox-token")
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    ctx.settings.gate.new_template_hold_count = 0  # template maturity is exercised in test_m7_gate
    orch = LocalOrchestrator(ctx)
    return ctx, ws_id, orch


async def _publish_all(ctx: AppContext, orch: LocalOrchestrator, hours: int = 40) -> dict[str, int]:
    for _ in range(hours):
        ctx.clock.advance(hours=1)  # type: ignore[attr-defined]
        await orch.tick()
    async with ctx.uow() as r:
        return await r.publications.state_counts(ctx.clock.now() - timedelta(days=30))


async def test_text_command_end_to_end(env) -> None:  # type: ignore[no-untyped-def]
    ctx, ws_id, orch = env
    run = await orch.submit("차량 3개 글로 만들어서 스레드에 올려줘", workspace_id=ws_id, actor="test")
    if run.status is RunStatus.AWAITING_APPROVAL:
        run = await orch.approve(run.id, by="test-owner")
    assert run.status is RunStatus.COMPLETED, run.summary
    async with ctx.uow() as r:
        contents = await r.contents.by_run(run.id)
    assert contents, "no content produced"
    approved = [c for c in contents if c.status is ContentStatus.APPROVED]
    assert approved, [c.status for c in contents]
    for c in approved:
        assert c.script.body.split("\n")[0].startswith("이 포스팅은 쿠팡 파트너스")

    states = await _publish_all(ctx, orch)
    assert states.get("PUBLISHED", 0) >= 1, states
    async with ctx.uow() as r:
        pubs = [p for c in approved for p in await r.publications.by_content(c.id)]
        links = [await r.links.get(p.link_id) for p in pubs]
    assert all(p.platform is Platform.THREADS for p in pubs)
    assert len({lk.sub_id for lk in links}) == len(pubs)  # subId ↔ publication 1:1
    assert all(p.caption.startswith("이 포스팅은 쿠팡 파트너스") for p in pubs)

    pipe = orch.pipeline
    out = await pipe.learn_hourly()
    assert out["insights"] >= 1
    out_daily = await pipe.learn_daily(ws_id)
    assert out_daily["report_rows"] >= 0
    async with ctx.uow() as r:
        arms = await r.arms.all(ws_id)
    assert arms and all(a.trials >= 1 for a in arms)


@pytest.mark.render
async def test_short_command_end_to_end_with_real_render(env) -> None:  # type: ignore[no-untyped-def]
    ctx, ws_id, orch = env
    run = await orch.submit("주방 2개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘", workspace_id=ws_id,
                            actor="test")
    if run.status is RunStatus.AWAITING_APPROVAL:
        run = await orch.approve(run.id, by="test-owner")
    assert run.status is RunStatus.COMPLETED, run.summary
    async with ctx.uow() as r:
        contents = await r.contents.by_run(run.id)
        checks = {c.id: await r.compliance.latest(c.id) for c in contents}
    shorts = [c for c in contents if c.render is not None]
    assert shorts, run.summary
    for c in shorts:
        assert c.render.width == 1080 and c.render.height == 1920
        assert c.render.disclosure_overlay.get("locked") is True
        assert 20 <= c.render.duration_sec <= 25.05
    passed = [c for c in contents if c.status is ContentStatus.APPROVED]
    assert passed, {str(k): v.verdict if v else None for k, v in checks.items()}
    states = await _publish_all(ctx, orch, hours=48)
    assert states.get("PUBLISHED", 0) >= 2, states
    async with ctx.uow() as r:
        pubs = [p for c in passed for p in await r.publications.by_content(c.id)]
    assert {p.platform for p in pubs} == {Platform.THREADS, Platform.INSTAGRAM}
    ig = [p for p in pubs if p.platform is Platform.INSTAGRAM]
    assert all(p.link_placement == "hub" for p in ig)
    assert any(p.state is PublicationState.PUBLISHED for p in ig)


# ───────────────────────── HTTP API ─────────────────────────


@pytest.fixture
async def api(env):  # type: ignore[no-untyped-def]
    from acvm.api.app import create_app

    ctx, ws_id, _orch = env
    app = create_app(ctx.settings, ctx=ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://acvm.test") as client:
            client.headers["Authorization"] = f"Bearer {ctx.settings.security.dev_api_token}"
            yield client, ctx, ws_id, app


async def test_api_contract(api) -> None:  # type: ignore[no-untyped-def]
    client, _ctx, ws_id, app = api
    me = (await client.get("/api/v1/me")).json()
    assert me["role"] == "owner" and me["workspace_id"] == str(ws_id)

    r = await client.post("/api/v1/commands", json={"text": "차량 2개 글로 만들어서 스레드에 올려줘"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["count"] == 2 and body["plan"]["targets"][0]["platform"] == "threads"
    run_id = body["run_id"]
    if body["status"] == "awaiting_approval":
        assert (await client.post(f"/api/v1/runs/{run_id}/approve", json={})).status_code == 200
    for t in list(app.state.gateway._tasks):
        await t
    detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert detail["run"]["status"] == "completed", detail["run"]
    assert detail["contents"] and "findings" in detail["contents"][0]

    assert (await client.get("/api/v1/runs")).json()["items"]
    assert "items" in (await client.get("/api/v1/holds")).json()
    for view in ("money", "pipeline", "risk", "learning"):
        assert (await client.get(f"/api/v1/dashboards/{view}")).status_code == 200, view
    rules = (await client.get("/api/v1/rules")).json()
    assert {x["id"] for x in rules["rules"]} >= {"D-01", "D-03", "C-04", "R-01"}
    be = (await client.get("/api/v1/quant/breakeven")).json()
    assert round(be["breakeven_views"], 1) == 694.4
    sim = (await client.get("/api/v1/quant/simulate", params={"median": 600, "sigma": 2})).json()
    assert 0 < sim["p_breakeven"] < 1 and sim["trials_for_95"] == 299
    assert (await client.get("/api/v1/accounts")).json()["items"]
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).json()["ready"] is True
    assert "acvm_gate_verdicts_total" in (await client.get("/metrics")).text


async def test_api_rbac_and_auth(api) -> None:  # type: ignore[no-untyped-def]
    client, *_ = api
    viewer = {"X-Dev-Role": "viewer"}
    assert (await client.post("/api/v1/commands", json={"text": "주방 1개"}, headers=viewer)).status_code == 403
    assert (await client.get("/api/v1/runs", headers=viewer)).status_code == 200
    reviewer = {"X-Dev-Role": "reviewer"}
    assert (await client.post("/api/v1/commands", json={"text": "주방 1개"}, headers=reviewer)).status_code == 403
    op = {"X-Dev-Role": "operator"}
    assert (await client.post("/api/v1/holds/00000000-0000-0000-0000-000000000000/decision",
                              json={"approve": True}, headers=op)).status_code == 403
    no_auth = await client.get("/api/v1/me", headers={"Authorization": "Bearer wrong"})
    assert no_auth.status_code == 401


async def test_hub_and_redirect(api) -> None:  # type: ignore[no-untyped-def]
    client, ctx, _ws_id, app = api
    r = await client.post("/api/v1/commands", json={"text": "주방 1개 글로 만들어서 스레드에 올려줘"})
    run_id = r.json()["run_id"]
    if r.json()["status"] == "awaiting_approval":
        await client.post(f"/api/v1/runs/{run_id}/approve", json={})
    for t in list(app.state.gateway._tasks):
        await t
    async with ctx.uow() as repos:
        contents = await repos.contents.by_run(__import__("uuid").UUID(run_id))
        pubs = [p for c in contents for p in await repos.publications.by_content(c.id)]
        link = await repos.links.get(pubs[0].link_id) if pubs else None
    if link is None:
        pytest.skip("no approved content in this sandbox draw")
    red = await client.get(f"/r/{link.hub_code}", follow_redirects=False)
    assert red.status_code == 302 and red.headers["location"].startswith("https://link.coupang.com/")
    assert (await client.get("/r/doesnotexist", follow_redirects=False)).status_code == 404
    clicks = await ctx.perf.clicks_since(ctx.clock.now() - timedelta(minutes=5))
    assert clicks and clicks[-1].ip_hash != "127.0.0.1"

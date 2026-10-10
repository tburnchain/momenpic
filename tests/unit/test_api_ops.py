from __future__ import annotations

import httpx
import pytest

from acvm.cli import NICHES, seed_workspace
from acvm.core.enums import Platform


@pytest.fixture
async def api(ctx):  # type: ignore[no-untyped-def]
    from acvm.api.app import create_app
    from acvm.infra.platforms.registry import build_platform_clients

    for _, (handle, _) in NICHES.items():
        for p in Platform:
            await ctx.secrets.put(f"tokens/{p.value}/{handle}", "sandbox-token")
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    ctx.settings.gate.new_template_hold_count = 0
    app = create_app(ctx.settings, ctx=ctx)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acvm.test") as c):
        c.headers["Authorization"] = f"Bearer {ctx.settings.security.dev_api_token}"
        yield c, ctx, ws_id, app


async def _run_text_command(c, app) -> str:  # type: ignore[no-untyped-def]
    r = await c.post("/api/v1/commands", json={"text": "차량 2개 글로 만들어서 스레드에 올려줘"})
    run_id = r.json()["run_id"]
    if r.json()["status"] == "awaiting_approval":
        await c.post(f"/api/v1/runs/{run_id}/approve", json={})
    for task in list(app.state.gateway._tasks):
        await task
    return run_id


async def test_system_status_reports_modes_and_never_secret_values(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _, _ = api
    s = (await c.get("/api/v1/system/status")).json()
    ids = {x["id"]: x for x in s["checks"]}
    assert ids["coupang"]["status"] == "warn" and "샌드박스" in ids["coupang"]["detail"]
    assert ids["link_key"]["status"] == "ok"
    assert s["g0"]["passed"] is False and len(s["g0"]["items"]) == 6
    assert any(a["credential"] == "configured" for a in s["accounts"])
    secret = await ctx.secrets.get(ctx.settings.security.link_signing_key_ref)
    assert secret not in (await c.get("/api/v1/system/status")).text


async def test_checklist_secrets_and_credentials_are_write_only(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _ws_id, _ = api
    for item in ("partners_policy", "ad_law", "ai_act", "platform_terms", "tax_entity"):
        assert (await c.post("/api/v1/system/checklist", json={"item": item, "done": True})).status_code == 200
    s = (await c.get("/api/v1/system/status")).json()
    assert all(i["done"] for i in s["g0"]["items"] if i["id"] != "coupang_api")

    r = await c.put("/api/v1/system/secrets/coupang_access_key", json={"value": "AK-test-1234567890"})
    assert r.status_code == 200 and "AK-test" not in r.text
    assert await ctx.secrets.get(ctx.settings.coupang.access_key_ref) == "AK-test-1234567890"
    assert (await c.put("/api/v1/system/secrets/unknown", json={"value": "xxxxxxxxxx"})).status_code == 404
    assert (await c.put("/api/v1/system/secrets/coupang_access_key", json={"value": "x" * 10},
                        headers={"X-Dev-Role": "operator"})).status_code == 403

    acc = (await c.get("/api/v1/accounts")).json()["items"][0]
    r = await c.put(f"/api/v1/accounts/{acc['id']}/credentials", json={"token": "EAAG-test-token-value"})
    assert r.status_code == 200 and "EAAG" not in r.text
    audit_text = (await c.get("/api/v1/audit")).text
    assert "EAAG" not in audit_text and "AK-test" not in audit_text and "secret.set" in audit_text


async def test_workspace_budget_is_enforced_from_console_setting(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, ws_id, _app = api
    ws = (await c.patch("/api/v1/workspace", json={"daily_budget_krw": 12345})).json()
    assert ws["daily_budget_krw"] == 12345
    async with ctx.uow() as repos:
        assert await ctx.daily_budget(repos, ws_id) == 12345
    assert (await c.patch("/api/v1/workspace", json={"daily_budget_krw": 1}, headers={"X-Dev-Role": "viewer"})).status_code == 403


async def test_publications_board_content_dossier_takedown_and_alerts(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _ws_id, app = api
    run_id = await _run_text_command(c, app)
    board = (await c.get("/api/v1/publications")).json()["items"]
    assert board and {b["state"] for b in board} <= {"QUEUED", "PUBLISHED", "PROCESSING"}
    assert all(b["caption"].startswith("이 포스팅은 쿠팡 파트너스") for b in board)
    queued = (await c.get("/api/v1/publications", params={"state": "queued"})).json()["items"]
    assert len(queued) == len([b for b in board if b["state"] == "QUEUED"])

    dossier = (await c.get(f"/api/v1/contents/{board[0]['content_id']}")).json()
    assert dossier["run_id"] == run_id and dossier["checks"] and dossier["checks"][-1]["verdict"] == "PASS"
    assert dossier["script"]["disclosure"].startswith("이 포스팅은")

    # publish one, then take it down (saga compensation path)
    ctx.clock.advance(hours=30)
    await app.state.gateway.pipeline.publish_due()
    published = (await c.get("/api/v1/publications", params={"state": "PUBLISHED"})).json()["items"]
    assert published
    r = await c.post(f"/api/v1/publications/{published[0]['id']}/takedown", json={"reason": "테스트 회수"})
    assert r.status_code == 200 and r.json()["state"] in ("TAKEN_DOWN", "PUBLISHED")
    assert (await c.post(f"/api/v1/publications/{published[0]['id']}/takedown", json={"reason": "x" * 3},
                         headers={"X-Dev-Role": "operator"})).status_code == 403
    feed = (await c.get("/api/v1/alerts")).json()
    assert "items" in feed and "pending_holds" in feed


async def test_claude_key_switches_agents_on_without_restart(api) -> None:  # type: ignore[no-untyped-def]
    c, ctx, _, _app = api
    assert ctx.llm is None
    ids = {x["id"]: x for x in (await c.get("/api/v1/system/status")).json()["checks"]}
    assert ids["llm"]["status"] == "warn"
    r = await c.put("/api/v1/system/secrets/anthropic_api_key", json={"value": "sk-ant-test-0000000000"})
    assert r.status_code == 200 and "sk-ant" not in r.text
    assert ctx.llm is not None  # client constructed lazily — no network call happens here
    ids = {x["id"]: x for x in (await c.get("/api/v1/system/status")).json()["checks"]}
    assert ids["llm"]["status"] == "ok"
    ctx.llm = None  # keep the rest of the suite offline

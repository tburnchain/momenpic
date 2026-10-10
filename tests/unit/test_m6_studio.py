"""M6 with Studio render options: voice override, caption style/position, watermark, captions off, beat asset
override with an uploaded own-footage clip — D-03 must still pass and option changes must re-render."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from acvm.context import AppContext
from acvm.core.enums import ContentKind, HookType
from acvm.core.errors import ValidationFailed
from acvm.domain.models import Content, Product, Script, ScriptBeat, Workspace
from acvm.media.caption_styles import watermark_anchor
from acvm.media.ffmpeg import extract_frame, measure_loudness, probe, run
from acvm.media.safe_area import layout_for, union
from acvm.media.verify import verify_disclosure_overlay
from acvm.modules.m5_asset.service import AssetService
from acvm.modules.m6_render import RenderService


def _beats() -> list[ScriptBeat]:
    return [
        ScriptBeat(start_sec=0, end_sec=3, role="hook", narration="과자 봉지, 아직도 집게로 막아두세요?",
                   on_screen_text="과자 봉지 아직도 이렇게?", visual="hook_text"),
        ScriptBeat(start_sec=3, end_sec=7, role="amplify", narration="하루만 지나도 눅눅해져서 버리게 되죠.",
                   visual="problem_scene"),
        ScriptBeat(start_sec=7, end_sec=11, role="reveal", narration="그래서 찾은 게 과자 밀봉 클립이에요.",
                   on_screen_text="과자 밀봉 클립 20개입", visual="product_closeup"),
        ScriptBeat(start_sec=11, end_sec=14, role="benefits", narration="20개입이라 넉넉하고 큰 봉지도 잡혀요.",
                   on_screen_text="20개입 · 11cm · ABS", visual="product_in_use"),
        ScriptBeat(start_sec=14, end_sec=16, role="benefits", narration="리뷰도 3,812개나 있어요.",
                   on_screen_text="리뷰 3,812개", visual="benefit_card"),
        ScriptBeat(start_sec=16, end_sec=19, role="cta", narration="제품 정보는 프로필 링크에서 확인하세요.",
                   on_screen_text="제품 정보는 프로필 링크에서 확인하세요", visual="cta_card"),
    ]


async def _content(ctx: AppContext, ws: Workspace, product: Product, options: dict[str, object]) -> Content:
    script = Script(hooks=["과자 봉지, 아직도 집게로 막아두세요?"], hook_type=HookType.QUESTION, beats=_beats(),
                    persona="30대 여성 직장인", template_id="problem-solution-v3", duration_sec=19)
    c = Content(workspace_id=ws.id, product_id=product.id, kind=ContentKind.SHORT, persona="30대 여성 직장인",
                template_id="problem-solution-v3", script=script, created_at=ctx.clock.now(), render_options=options)
    async with ctx.uow() as r:
        await r.products.upsert(product)
        await r.contents.add(c)
    return c


async def _timeline(ctx: AppContext, media_uri: str) -> dict[str, object]:
    return dict(json.loads(await ctx.objects.get_bytes(media_uri[: -len(".mp4")] + ".timeline.json")))


@pytest.mark.render
async def test_studio_options_render_and_rerender(ctx: AppContext, workspace: Workspace, product_factory,
                                                  tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    clip = tmp_path / "own.mp4"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=s=720x1280:r=30:d=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               str(clip)])
    async with ctx.uow() as r:
        await r.products.upsert(p)
    own = await AssetService(ctx).ingest_upload(workspace.id, clip, "own.mp4", "video/mp4", product_id=p.id,
                                                attested_by="owner@tburn")
    options = {"voice_id": "ko-M-40s-punchy", "caption_style": "highlight_yellow", "caption_position": "upper",
               "watermark": "@kitchen_pick", "beat_assets": {"3": str(own.id)}}
    c = await _content(ctx, workspace, p, options)
    svc = RenderService(ctx)
    meta = await svc.render(c.id)

    video = await ctx.objects.local_path(meta.media_uri)
    info = await probe(video)
    assert (info.width, info.height, round(info.fps)) == (1080, 1920, 30) and 20 <= info.duration <= 25.05
    assert await measure_loudness(video) == pytest.approx(-14.0, abs=1.0)
    assert meta.voice_id == "ko-M-40s-punchy" and meta.voice_source.endswith(":ko-M-40s-punchy")
    assert own.id in meta.asset_ids
    check = await verify_disclosure_overlay(video, meta.disclosure_overlay)
    assert check.ok, check.details  # options never weaken the locked band

    tl = await _timeline(ctx, meta.media_uri)
    style = tl["style"]
    assert isinstance(style, dict)
    assert style["caption_style"] == "highlight_yellow" and style["caption_position"] == "upper"
    assert style["watermark"] == "@kitchen_pick" and style["voice_id"] == "ko-M-40s-punchy"
    scenes = tl["scenes"]
    assert isinstance(scenes, list) and scenes[3]["asset_id"] == str(own.id) and scenes[3]["kind"] == "video"
    async with ctx.uow() as r:
        assert own.id in await r.contents.asset_ids(c.id)

    # the watermark is drawn top-right below the band (after the band has gone, over a calm card area)
    wx, wy = watermark_anchor(layout_for(union(None)))
    frame = np.asarray(Image.open(io.BytesIO(await extract_frame(video, 5.0))).convert("L"), dtype=np.float64)
    assert frame[wy:wy + 34, wx - 220:wx].std() > 8.0

    # change options → re-render (new file, new event, new cost row); unchanged → reuse
    async with ctx.uow() as r:
        cur = await r.contents.get(c.id)
        assert cur is not None
        cur.render_options = {"caption_style": "box_dark", "caption_position": "middle", "captions_enabled": False,
                              "watermark": ""}
        await r.contents.save(cur)
    meta2 = await svc.render(c.id)
    assert meta2.sha256 != meta.sha256 and meta2.voice_id == "ko-F-30s-brisk"  # persona voice again
    check2 = await verify_disclosure_overlay(await ctx.objects.local_path(meta2.media_uri), meta2.disclosure_overlay)
    assert check2.ok
    tl2 = await _timeline(ctx, meta2.media_uri)
    assert tl2["style"]["captions_enabled"] is False and tl2["style"]["caption_position"] == "middle"  # type: ignore[index]
    assert await svc.render(c.id) == meta2
    async with ctx.uow() as r:
        done = [e for e in await r.outbox.pending() if e.type == "render.done"]
        cost = await r.costs.content_total(c.id)
    assert len(done) == 2 and len({e.idempotency_key for e in done}) == 2
    assert cost == pytest.approx(svc._estimated_cost(meta.duration_sec) + svc._estimated_cost(meta2.duration_sec),
                                 rel=1e-3)


async def test_invalid_studio_options_fail_fast(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    c = await _content(ctx, workspace, product_factory(pid=9100), {"voice_id": "celebrity-clone"})
    with pytest.raises(ValidationFailed):
        await RenderService(ctx).render(c.id)
    c2 = await _content(ctx, workspace, product_factory(pid=9101), {"caption_position": "sideways"})
    with pytest.raises(ValidationFailed):
        await RenderService(ctx).render(c2.id)

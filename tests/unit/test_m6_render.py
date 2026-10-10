"""M6 end-to-end: RenderService on a synthetic 6-beat script (§4 M6 output contract, D-03, R-01, idempotency)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from acvm.context import AppContext
from acvm.core.enums import AssetSource, ContentKind, ContentStatus, HookType
from acvm.core.errors import NotFound, ValidationFailed
from acvm.core.events import Event, ScriptReady
from acvm.domain.models import Content, Product, Script, ScriptBeat, Workspace
from acvm.media.cards import hook_card, product_card, render_card
from acvm.media.ffmpeg import measure_loudness, probe, sha256_file
from acvm.media.fonts import libass_text_width, resolve_font
from acvm.media.verify import verify_disclosure_overlay
from acvm.modules.m6_render import FFmpegRenderer, RenderService, TimedToneTTS, VisualRef, build_timeline, build_tts


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


async def _content(ctx: AppContext, ws: Workspace, product: Product, *,
                   kind: ContentKind = ContentKind.SHORT) -> Content:
    script = Script(hooks=["과자 봉지, 아직도 집게로 막아두세요?"], hook_type=HookType.QUESTION,
                    beats=_beats() if kind is ContentKind.SHORT else [], body="본문", persona="30대 여성 직장인",
                    template_id="problem-solution-v3", duration_sec=19)
    c = Content(workspace_id=ws.id, product_id=product.id, kind=kind, persona="30대 여성 직장인",
                template_id="problem-solution-v3", script=script, created_at=ctx.clock.now())
    async with ctx.uow() as r:
        await r.products.upsert(product)
        await r.contents.add(c)
    return c


@pytest.mark.render
async def test_render_short_meets_contract_and_is_idempotent(ctx: AppContext, workspace: Workspace,
                                                            product_factory) -> None:  # type: ignore[no-untyped-def]
    c = await _content(ctx, workspace, product_factory())
    svc = RenderService(ctx)
    meta = await svc.render(c.id)

    # §4 M6 output contract, measured on the stored object
    video = await ctx.objects.local_path(meta.media_uri)
    info = await probe(video)
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == pytest.approx(30.0) and info.video_codec == "h264" and info.pix_fmt == "yuv420p"
    assert info.audio_codec == "aac" and info.sample_rate == 48_000
    assert 20.0 <= info.duration <= 25.0 + 1e-3 and 20.0 <= meta.duration_sec <= 25.0 + 1e-3
    assert await measure_loudness(video) == pytest.approx(-14.0, abs=1.0)
    assert meta.loudness_lufs == pytest.approx(-14.0, abs=1.0)
    assert (meta.width, meta.height, meta.fps) == (1080, 1920, 30)
    assert meta.sha256 == sha256_file(video)
    assert meta.media_uri == f"file://media/{c.id}/{meta.sha256}.mp4"
    assert meta.thumbnail_uri == f"file://media/{c.id}/{meta.sha256}.jpg"
    with Image.open(io.BytesIO(await ctx.objects.get_bytes(meta.thumbnail_uri))) as thumb:
        assert thumb.format == "JPEG" and thumb.size == (1080, 1920)

    # D-03: locked overlay recorded and provable on decoded frames
    ov = meta.disclosure_overlay
    assert ov["locked"] is True and ov["start"] == 0.0 and ov["end"] >= 3.0 and "광고" in ov["text"]
    assert ov["box"] == [0, 0, 1080, 220] and ov["verification"]["ok"] is True
    check = await verify_disclosure_overlay(video, ov)
    assert check.ok and check.score > 0.95

    # R-01 / R-03: every input is a ledger row, voice provenance recorded
    provider = (await build_tts(ctx)).provider  # real SAPI voice on Windows dev boxes, tone elsewhere
    assert meta.voice_id == "ko-F-30s-brisk" and meta.voice_source == f"tts:{provider}:ko-F-30s-brisk"
    assert meta.renderer == "acvm-ffmpeg/1" and meta.template_id == "problem-solution-v3"
    async with ctx.uow() as r:
        rows = await r.assets.get_many(meta.asset_ids)
        stored = await r.contents.get(c.id)
        links = await r.contents.asset_ids(c.id)
        cost = await r.costs.content_total(c.id)
        pending = await r.outbox.pending()
    assert set(rows) == set(meta.asset_ids)
    assert any(a.kind == "voice" for a in rows.values())
    assert all(a.rights_source in (AssetSource.GENERATED_CARD, AssetSource.LICENSED_STOCK) for a in rows.values())
    assert stored is not None and stored.status is ContentStatus.RENDERED and stored.render == meta
    assert set(links) == set(meta.asset_ids)
    expected = (ctx.settings.costs.tts_per_30s_krw * meta.duration_sec / 30 + ctx.settings.costs.render_per_short_krw
                + ctx.settings.costs.storage_per_short_krw)
    assert cost == pytest.approx(expected, rel=1e-3)
    done = [e for e in pending if e.type == "render.done"]
    assert len(done) == 1 and done[0].idempotency_key == f"{c.id}:render:{meta.sha256}"
    assert done[0].payload["media_uri"] == meta.media_uri

    # idempotency: the second call returns the stored meta without touching the renderer
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("renderer must not run again")

    svc.renderer.render = _boom  # type: ignore[method-assign]
    assert await RenderService(ctx, renderer=svc.renderer).render(c.id) == meta
    assert await svc.handle_script_ready(Event.of(ScriptReady(content_id=c.id, claims=[], persona=c.persona))) == meta
    async with ctx.uow() as r:
        assert len([e for e in await r.outbox.pending() if e.type == "render.done"]) == 1
        assert await r.costs.content_total(c.id) == pytest.approx(cost)
    assert await ctx.relay_outbox() >= 1
    assert len(ctx.bus.of_type("render.done")) == 1  # type: ignore[attr-defined]


@pytest.mark.render
async def test_control_without_band_fails_and_encode_is_deterministic(tmp_path: Path) -> None:
    """Same scenes/audio rendered without the ASS band must fail D-03; with it, pass — and re-encode identically."""
    spec_beats = _beats()
    font = resolve_font()
    assets: dict[str, Path] = {}
    visuals: dict[int, VisualRef] = {}
    for i, b in enumerate(spec_beats):
        card = (product_card("과자 밀봉 클립 20개입", 12_900, seed="p") if "product" in b.visual
                else hook_card(b.on_screen_text or b.narration, seed="p"))
        path = tmp_path / f"v{i}.png"
        path.write_bytes(render_card(card, font))
        assets[f"a{i}"] = path
        visuals[i] = VisualRef(asset_id=f"a{i}", uri=f"file://{path.name}", kind="image",
                               rights_source="generated_card", text_baked="product" not in b.visual,
                               aspect=1080 / 1920, generated_card=True)
    tts = await TimedToneTTS().synthesize([b.narration for b in spec_beats], tmp_path / "n.wav", "ko-F-30s-brisk")
    from acvm.config import GatePolicy, RenderSpec

    spec, gate = RenderSpec(), GatePolicy()
    tl = build_timeline(content_id="control", template_id="problem-solution-v3", beats=spec_beats, words=tts.words,
                        narration_duration=tts.duration_s, visuals=visuals, spec=spec, gate=gate,
                        font_family=font.family, font_file=str(font.path),
                        measure=lambda t, s: (libass_text_width(font, t, s), float(s)))
    renderer = FFmpegRenderer(spec)
    work = tmp_path / "work"
    arts = await renderer.render(tl, narration_wav=tts.wav_path, asset_paths=assets, font=font, workdir=work)
    overlay = tl["overlays"]["disclosure"]
    assert (await verify_disclosure_overlay(arts.master, overlay)).ok

    bare = await renderer.encode_video(tl, assets, work, ass_name=None, out_name="bare.video.mp4")
    control = await renderer.mux(work, video=bare, audio=arts.audio, out_name="control.mp4",
                                 duration=float(tl["duration"]))
    check = await verify_disclosure_overlay(control, overlay)
    assert not check.ok and check.score < 0.5

    again = await renderer.encode_video(tl, assets, work, ass_name="master.ass", out_name="again.video.mp4")
    first = work / "master.video.mp4"
    assert sha256_file(again) == sha256_file(first)


async def test_text_content_is_not_rendered(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    c = await _content(ctx, workspace, product_factory(pid=7007), kind=ContentKind.TEXT)
    svc = RenderService(ctx)
    with pytest.raises(ValidationFailed):
        await svc.render(c.id)
    assert await svc.handle_script_ready(Event.of(ScriptReady(content_id=c.id, claims=[], persona="x"))) is None


async def test_unknown_content_raises_not_found(ctx: AppContext) -> None:
    import uuid

    with pytest.raises(NotFound):
        await RenderService(ctx).render(uuid.uuid4())

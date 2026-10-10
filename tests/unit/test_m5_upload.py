"""M5 Studio uploads (① 영상소스): own-footage ingestion, derived edits and beat overrides (R-01, §8.2)."""

from __future__ import annotations

import io
import uuid
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from acvm.context import AppContext
from acvm.core.enums import AssetSource, ContentKind, HookType
from acvm.core.errors import NotFound, PolicyViolation, ValidationFailed
from acvm.domain.models import Asset, Content, Product, Script, ScriptBeat, Workspace
from acvm.media.edit import cut_video
from acvm.media.ffmpeg import run
from acvm.modules.m5_asset import service as m5
from acvm.modules.m5_asset.service import AssetService


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def _mp4(path: Path, seconds: float = 3.0) -> Path:
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               f"testsrc2=s=540x960:r=30:d={seconds}", "-f", "lavfi", "-i",
               f"sine=frequency=330:sample_rate=48000:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


async def _content(ctx: AppContext, ws: Workspace, product: Product, options: dict[str, object]) -> Content:
    beats = [
        ScriptBeat(start_sec=0, end_sec=3, role="hook", narration="훅 문장이에요.", visual="hook_text"),
        ScriptBeat(start_sec=3, end_sec=6, role="amplify", narration="문제 장면이에요.", visual="problem_scene"),
        ScriptBeat(start_sec=6, end_sec=9, role="reveal", narration="상품 등장이에요.", visual="product_closeup"),
        ScriptBeat(start_sec=9, end_sec=12, role="cta", narration="프로필 링크에서 확인하세요.", visual="cta_card"),
    ]
    c = Content(workspace_id=ws.id, product_id=product.id, kind=ContentKind.SHORT, persona="30대 여성",
                template_id="problem-solution-v3", created_at=ctx.clock.now(), render_options=options,
                script=Script(hooks=["훅"], hook_type=HookType.QUESTION, beats=beats, persona="30대 여성",
                              template_id="problem-solution-v3"))
    async with ctx.uow() as r:
        await r.products.upsert(product)
        await r.contents.add(c)
    return c


@pytest.mark.render
async def test_upload_video_registers_own_footage_and_dedupes(ctx: AppContext, workspace: Workspace,
                                                              product_factory, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    svc = AssetService(ctx)
    clip = await _mp4(tmp_path / "broll.mp4")
    a = await svc.ingest_upload(workspace.id, clip, "내 촬영.mp4", "video/mp4", rights="own_footage", product_id=p.id,
                                tags=("studio",), attested_by="owner@tburn")
    assert a.kind == "video" and a.rights_source is AssetSource.OWN_FOOTAGE and a.product_id == p.id
    assert a.uri == f"file://assets/own/{a.sha256}.mp4"
    assert a.attribution == "uploaded by owner@tburn" and "studio" in a.tags and f"product:{p.id}" in a.tags
    assert f"workspace:{workspace.id}" in a.tags and "owner@tburn" in a.license
    again = await svc.ingest_upload(workspace.id, clip.read_bytes(), "copy.mp4", "video/mp4", attested_by="owner@tburn")
    assert again.id == a.id  # sha256 de-duplication
    # derived cut inherits rights and is traceable to its parent
    cut = await cut_video(await ctx.objects.local_path(a.uri), 0.5, 1.5, tmp_path / "cut.mp4")
    d = await svc.derive_asset(a, cut, "video", op="cut")
    assert d.rights_source is AssetSource.OWN_FOOTAGE and d.license == a.license and d.product_id == p.id
    assert f"derived:cut:{a.id}" in d.tags and d.uri.startswith("file://assets/derived/")
    with pytest.raises(ValidationFailed):
        await svc.derive_asset(a, cut, "video", op="Cut Video!")
    ghost = a.model_copy(update={"id": uuid.uuid4()})
    with pytest.raises(PolicyViolation):
        await svc.derive_asset(ghost, cut, "video", op="cut")


@pytest.mark.render
async def test_upload_validation(ctx: AppContext, workspace: Workspace, tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    svc = AssetService(ctx)
    img = await svc.ingest_upload(workspace.id, _png(), "photo.png", "image/png", attested_by="op")
    assert img.kind == "image" and img.uri.endswith(".png")
    with pytest.raises(PolicyViolation):
        await svc.ingest_upload(workspace.id, _png(), "p.png", "image/png", rights="licensed_stock",  # type: ignore[arg-type]
                                attested_by="op")
    with pytest.raises(ValidationFailed):
        await svc.ingest_upload(workspace.id, _png(), "p.png", "image/png", attested_by="  ")
    with pytest.raises(ValidationFailed):  # unsupported type
        await svc.ingest_upload(workspace.id, b"GIF89a", "a.gif", "image/gif", attested_by="op")
    with pytest.raises(ValidationFailed):  # extension contradicts MIME family
        await svc.ingest_upload(workspace.id, _png(), "clip.mp4", "image/png", attested_by="op")
    with pytest.raises(ValidationFailed):  # PNG bytes declared as JPEG
        await svc.ingest_upload(workspace.id, _png(), "p.jpg", "image/jpeg", attested_by="op")
    with pytest.raises(ValidationFailed):  # not a video at all
        await svc.ingest_upload(workspace.id, b"\x00" * 2048, "v.mp4", "video/mp4", attested_by="op")
    with pytest.raises(NotFound):
        await svc.ingest_upload(workspace.id, _png(), "p.png", "image/png", product_id=999_999, attested_by="op")
    monkeypatch.setattr(m5, "MAX_UPLOAD_SECONDS", 1.0)
    with pytest.raises(ValidationFailed):
        await svc.ingest_upload(workspace.id, await _mp4(tmp_path / "long.mp4", 2.0), "l.mp4", "video/mp4",
                                attested_by="op")
    monkeypatch.setattr(m5, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(ValidationFailed):
        await svc.ingest_upload(workspace.id, _png(), "big.png", "image/png", attested_by="op")


async def test_select_for_applies_beat_overrides(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory(pid=8008)
    svc = AssetService(ctx)
    own = await svc.ingest_upload(workspace.id, _png(), "own.png", "image/png", attested_by="op")
    c = await _content(ctx, workspace, p, {"beat_assets": {"1": str(own.id)}, "caption_position": "middle"})
    picks = await svc.select_for(c, p)
    assert picks[1].id == own.id
    assert picks[0].rights_source is AssetSource.GENERATED_CARD

    # AI imagery may not replace the product on a product beat
    ai = await svc.ingest_bytes(_png()[:-8] + b"\x00" * 8, kind="image", rights_source=AssetSource.AI_GENERATED_BACKGROUND,
                                license="gen bg", ext=".png", tags=["background"])
    bad = await _content(ctx, workspace, p, {"beat_assets": {"2": str(ai.id)}})
    with pytest.raises(PolicyViolation):
        await svc.select_for(bad, p)
    missing = await _content(ctx, workspace, p, {"beat_assets": {"0": str(uuid.uuid4())}})
    with pytest.raises(PolicyViolation):
        await svc.select_for(missing, p)
    expired = await svc.register(Asset(kind="image", uri=own.uri, sha256="e" * 64,
                                       rights_source=AssetSource.LICENSED_STOCK, license="stock",
                                       expires_at=date(2026, 12, 31)))
    async with ctx.uow() as r:
        from acvm.db import tables as t

        row = await r.session.get(t.AssetRow, expired.id)
        assert row is not None
        row.expires_at = date(2026, 1, 1)
    old = await _content(ctx, workspace, p, {"beat_assets": {"0": str(expired.id)}})
    with pytest.raises(PolicyViolation):
        await svc.select_for(old, p)
    with pytest.raises(ValidationFailed):
        await svc.select_for(await _content(ctx, workspace, p, {"beat_assets": {"7": str(own.id)}}), p)

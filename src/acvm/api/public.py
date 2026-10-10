"""Public surface (§10 네트워크: only the link hub, /r redirects and signed media are internet-facing)."""

from __future__ import annotations

import asyncio
import mimetypes

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from acvm.api.auth import ctx_of
from acvm.core.errors import NotFound
from acvm.db import tables as t
from acvm.infra.objects import LocalObjectStore

router = APIRouter()


@router.get("/hub/{handle}", response_class=HTMLResponse)
async def hub(handle: str, request: Request) -> HTMLResponse:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        acc = (await repos.session.execute(select(t.ChannelAccountRow).where(t.ChannelAccountRow.handle == handle)
                                           .limit(1))).scalar_one_or_none()
        meta = await repos.kv.get_stale(f"hub:{acc.id}") if acc else None
    if acc is None:
        raise HTTPException(404, "hub not found")
    if not meta:
        html = await request.app.state.gateway.pipeline.links.build_hub_page(acc.id)
    else:
        html = (await ctx.objects.get_bytes(str(meta["uri"]))).decode("utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=300",
                                       "Content-Security-Policy": "default-src 'self'; img-src * data:; style-src 'unsafe-inline'"})


@router.get("/r/{code}")
async def redirect(code: str, request: Request) -> RedirectResponse:
    ip = request.headers.get("CF-Connecting-IP") or (request.client.host if request.client else "0.0.0.0")  # noqa: S104 — fallback label, not a bind
    try:
        target = await request.app.state.gateway.pipeline.links.resolve_click(
            code, ip, request.headers.get("User-Agent", ""), request.headers.get("Referer", ""))
    except NotFound as exc:
        raise HTTPException(404, "link not found") from exc
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/media/{key:path}")
async def media(key: str, request: Request, exp: int = 0, sig: str = "") -> Response:
    ctx = ctx_of(request)
    store = ctx.objects
    if not isinstance(store, LocalObjectStore):
        raise HTTPException(404, "media served from object storage")
    if not store.verify(key, exp, sig):
        raise HTTPException(403, "expired or invalid signature")
    path = await store.local_path(f"file://{key}")
    if not path.exists():
        raise HTTPException(404, "not found")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return Response(path.read_bytes(), media_type=ctype, headers={"Cache-Control": "private, max-age=600"})


_SANDBOX_IMG: dict[int, bytes] = {}


@router.get("/sandbox/product-image/{product_id}.jpg")
async def sandbox_product_image(product_id: int, request: Request) -> Response:
    """Sandbox catalogue thumbnails (the sandbox has no real Coupang CDN). Disabled outside sandbox mode."""
    ctx = ctx_of(request)
    if not ctx.settings.coupang.sandbox:
        raise HTTPException(404, "not found")
    if product_id not in _SANDBOX_IMG:
        async with ctx.uow() as repos:
            product = await repos.products.get(product_id)
        name, price = (product.name, product.price) if product else (f"상품 {product_id}", None)
        _SANDBOX_IMG[product_id] = await asyncio.to_thread(_square_card, name, price, product_id)
    return Response(_SANDBOX_IMG[product_id], media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


def _square_card(name: str, price: int | None, product_id: int) -> bytes:
    import io

    from acvm.media.cards import product_card, render_card_image

    img = render_card_image(product_card(name, price, seed=f"sandbox:{product_id}"))
    w, h = img.size
    side = min(w, h)
    top = (h - side) // 2
    thumb = img.crop((0, top, side, top + side)).resize((492, 492)).convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=86)
    return buf.getvalue()

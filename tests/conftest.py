from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from acvm.context import AppContext, build_test_context
from acvm.core.clock import FrozenClock
from acvm.core.enums import AccountStatus, Platform
from acvm.domain.models import ChannelAccount, Product, Workspace


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 8, 0, 30, tzinfo=UTC))  # 09:30 KST


@pytest.fixture
async def ctx(tmp_path, clock: FrozenClock) -> AsyncIterator[AppContext]:  # type: ignore[no-untyped-def]
    c = await build_test_context(clock=clock, tmp_dir=str(tmp_path))
    yield c
    await c.db.dispose()


@pytest.fixture
async def workspace(ctx: AppContext, clock: FrozenClock) -> Workspace:
    ws = Workspace(name="TBURN LABS", created_at=clock.now())
    async with ctx.uow() as r:
        await r.workspaces.add(ws)
    return ws


@pytest.fixture
async def accounts(ctx: AppContext, workspace: Workspace, clock: FrozenClock) -> dict[Platform, ChannelAccount]:
    """Mature (> 14 days) kitchen-niche accounts on every platform."""
    out: dict[Platform, ChannelAccount] = {}
    async with ctx.uow() as r:
        for p in Platform:
            acc = ChannelAccount(
                workspace_id=workspace.id, platform=p, handle=f"kitchen_pick_{p.value}",
                platform_user_id=f"uid-{p.value}", token_ref=f"tokens/{p.value}/kitchen_pick",
                daily_cap=ctx.settings.quota.daily_cap[p.value], status=AccountStatus.ACTIVE,
                niche="kitchen", created_at=clock.now() - timedelta(days=40))
            await r.accounts.add(acc)
            out[p] = acc
    return out


def make_product(pid: int = 1001, *, price: int = 12_900, category: str = "1013", regulated: bool = False,
                 name: str = "과자 밀봉 클립 20개입 대용량 세트", at: datetime | None = None) -> Product:
    return Product(
        id=pid, coupang_product_id=str(pid), name=name, category_id=category, category_name="주방용품",
        price=price, rating=4.7, review_count=3_812, is_rocket=True, is_free_shipping=True,
        image_url=f"https://image.example/{pid}.jpg",
        product_url=f"https://www.coupang.com/vp/products/{pid}", regulated=regulated,
        last_seen_at=at or datetime(2026, 10, 8, 0, 0, tzinfo=UTC),
        price_checked_at=at or datetime(2026, 10, 8, 0, 0, tzinfo=UTC),
        verified_facts={"count": "20개입", "material": "ABS 플라스틱", "length_cm": 11})


@pytest.fixture
def product_factory():  # type: ignore[no-untyped-def]
    return make_product


@pytest.fixture
def uid() -> uuid.UUID:
    return uuid.uuid4()

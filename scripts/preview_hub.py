"""Render a sample link-hub page from a sandbox run (dev tool): `uv run python scripts/preview_hub.py`.

Writes var/preview/hub.html — open it in a browser to review the public page a shopper sees.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from acvm.cli import NICHES, seed_workspace
from acvm.context import build_test_context
from acvm.core.enums import Platform, RunStatus
from acvm.infra.platforms.registry import build_platform_clients
from acvm.orchestration.local import LocalOrchestrator


async def main() -> None:
    out = Path("var/preview")
    out.mkdir(parents=True, exist_ok=True)
    ctx = await build_test_context(tmp_dir=str(out / "ctx"))
    for _, (handle, _) in NICHES.items():
        for p in Platform:
            await ctx.secrets.put(f"tokens/{p.value}/{handle}", "sandbox-token")
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    ctx.settings.gate.new_template_hold_count = 0
    orch = LocalOrchestrator(ctx)
    run = await orch.submit("주방 4개 글로 만들어서 스레드에 올려줘", workspace_id=ws_id, actor="preview", execute=False)
    if run.status is RunStatus.AWAITING_APPROVAL:
        await orch.pipeline.approve(run.id, by="preview")
    await orch.execute(run.id)
    async with ctx.uow() as repos:
        acc = next(a for a in await repos.accounts.list(ws_id) if a.platform is Platform.THREADS and a.niche == "kitchen")
    html = await orch.pipeline.links.build_hub_page(acc.id)
    (out / "hub.html").write_text(html, encoding="utf-8")
    sys.stdout.write(f"{(out / 'hub.html').resolve()}\n")
    await ctx.db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

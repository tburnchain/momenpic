"""`acvm demo` — the vending machine running for N simulated days, fully offline.

Real components: planner, Commander rule parser, M1 scoring, M3 link signing, M4 script engine, M5/M6
FFmpeg renders with the locked disclosure overlay, M7 gate (incl. frame verification), M8 state machine,
quotas and scheduling, M9 attribution, M10 Thompson sampling, amplification, playbook.
Simulated: the Coupang API (sandbox catalogue + deterministic reports) and the social platforms
(sandbox clients with lognormal view growth). Time is a frozen clock advanced hour by hour.
"""

from __future__ import annotations

import secrets
import shutil
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from acvm.config import CoupangConfig, ObjectStoreConfig, PlatformConfig, Settings
from acvm.context import build_context
from acvm.core.clock import KST, FrozenClock
from acvm.core.enums import RunStatus
from acvm.core.logging import configure_logging, get_logger
from acvm.infra.secrets import EnvSecretStore

log = get_logger(__name__)


async def run_demo(*, days: int, out_dir: str, commands: list[str]) -> dict[str, Any]:
    from acvm.cli import NICHES, seed_workspace
    from acvm.core.enums import Platform
    from acvm.infra.platforms.registry import build_platform_clients
    from acvm.orchestration.local import LocalOrchestrator

    configure_logging(level="WARNING", json=False)
    t0 = time.perf_counter()
    root = Path(out_dir).resolve()  # noqa: ASYNC240
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    settings = Settings(env="dev", data_dir=root, database_url=f"sqlite+aiosqlite:///{(root / 'acvm.db').as_posix()}",
                        objects=ObjectStoreConfig(backend="local", local_root=str(root / "objects")),
                        coupang=CoupangConfig(sandbox=True), platforms=PlatformConfig(dry_run=True),
                        workspace_daily_budget_krw=50_000)
    clock = FrozenClock(datetime(2026, 10, 8, 0, 5, tzinfo=UTC))  # 09:05 KST
    ctx = await build_context(settings, clock=clock, create_schema=True)
    overrides = {settings.security.link_signing_key_ref: secrets.token_hex(32),
                 settings.security.click_ip_salt_ref: secrets.token_hex(16),
                 settings.coupang.access_key_ref: "sandbox", settings.coupang.secret_key_ref: "sandbox"}
    for _, (handle, _) in NICHES.items():
        for p in Platform:
            overrides[f"tokens/{p.value}/{handle}"] = "sandbox-token"
    ctx.secrets = EnvSecretStore(overrides)
    ctx.extras["platform_clients"] = build_platform_clients(ctx)
    ws_id = await seed_workspace(ctx, mature_days=30)
    orch = LocalOrchestrator(ctx)
    pipe = orch.pipeline

    timeline: list[str] = []
    runs = []
    for text in commands:
        run = await orch.submit(text, workspace_id=ws_id, actor="demo-operator", execute=False)
        timeline.append(f"{_kst(clock)} 명령 접수: {text} → 승인 {run.plan.approval.mode if run.plan else '-'}"
                        f" ({'; '.join(run.plan.approval.reasons) if run.plan else ''})")
        if run.status is RunStatus.AWAITING_APPROVAL:
            await pipe.approve(run.id, by="demo-operator")
            timeline.append(f"{_kst(clock)} 콘솔 승인: {run.id}")
        run = await orch.execute(run.id)
        runs.append(run)
        timeline.append(f"{_kst(clock)} 실행 완료: {run.summary.get('verdicts')}")
        reviewed = await _review_holds(ctx, pipe, ws_id)
        if reviewed:
            timeline.append(f"{_kst(clock)} HOLD 검토: {reviewed}")

    published_before = 0
    for hour in range(days * 24):
        clock.advance(hours=1)
        await orch.tick()
        await pipe.learn_hourly()
        local = clock.now().astimezone(KST)
        if local.hour == 10:
            res = await pipe.learn_daily(ws_id)
            timeline.append(f"{_kst(clock)} 일일 학습: {res}")
        await ctx.relay_outbox()
        await _review_holds(ctx, pipe, ws_id)
        async with ctx.uow() as repos:
            counts = await repos.publications.state_counts(clock.now() - timedelta(days=60))
        published = counts.get("PUBLISHED", 0)
        if published != published_before:
            timeline.append(f"{_kst(clock)} 게시 누적 {published}건 · 상태 {dict(counts)}")
            published_before = published
        _ = hour

    from acvm.modules.m9_analytics import dashboards

    money = await dashboards.money_view(ctx, ws_id, days + 1)
    learning = await dashboards.learning_view(ctx, ws_id)
    async with ctx.uow() as repos:
        all_runs = await repos.runs.recent(ws_id, 100)
        verdicts = await repos.compliance.verdict_counts(datetime(2000, 1, 1, tzinfo=UTC))
        states = await repos.publications.state_counts(datetime(2000, 1, 1, tzinfo=UTC))
        rules = await repos.playbook.active(ws_id, 10)
        contents = [c for r in all_runs for c in await repos.contents.by_run(r.id)]
    media = sorted(str(p.relative_to(root)) for p in (root / "objects").rglob("*.mp4"))
    arms = sorted(learning.get("arms", []), key=lambda a: -(a.get("rpi_mean") or 0))[:5]
    report = {
        "simulated_days": days,
        "wall_seconds": round(time.perf_counter() - t0, 1),
        "runs": [{"command": r.raw_command, "status": r.status, "verdicts": (r.summary or {}).get("verdicts")}
                 for r in all_runs],
        "amplification_runs": sum(1 for r in all_runs if r.raw_command.startswith("amplify:")),
        "contents": len(contents),
        "content_status": dict(Counter(c.status.value for c in contents)),
        "gate_verdicts": verdicts,
        "publication_states": states,
        "money": money.get("totals"),
        "channels": money.get("channels"),
        "top_arms": arms,
        "playbook": [r.text for r in rules],
        "rendered_videos": media[:10],
        "sample_script": _sample(contents),
        "timeline": timeline[-40:],
        "artifacts_dir": str(root),
    }
    await ctx.close()
    return report


def _kst(clock: FrozenClock) -> str:
    return clock.now().astimezone(KST).strftime("%m-%d %H:%M")


async def _review_holds(ctx: Any, pipe: Any, ws_id: Any) -> dict[str, int]:
    """Simulated reviewer (§8.4, ~20 s/item): approves items held only for template/account maturity (H-01);
    anything else (IP, voice, non-converging fixes) is rejected — the conservative human default."""
    out: Counter[str] = Counter()
    async with ctx.uow() as repos:
        items = await repos.compliance.pending_reviews(ws_id)
    for it in items:
        only_maturity = all(str(r).startswith("H-01") for r in it.reasons)
        await pipe.gate.decide_review(it.id, approve=only_maturity, by="demo-reviewer",
                                      note="성숙도 HOLD 확인" if only_maturity else "위험 요소 — 반려")
        out["approved" if only_maturity else "rejected"] += 1
    if items:
        await ctx.relay_outbox()
    return dict(out)


def _sample(contents: list[Any]) -> dict[str, Any] | None:
    for c in contents:
        if c.status.value == "approved" and c.kind.value == "short":
            return {"hook": c.script.hook, "beats": [f"[{b.start_sec:.0f}-{b.end_sec:.0f}s {b.role}] {b.narration}"
                                                     for b in c.script.beats],
                    "caption": c.script.caption, "claims": [(cl.text, cl.evidence) for cl in c.claims]}
    return None


__all__ = ["run_demo", "timedelta"]

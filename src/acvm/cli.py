"""`acvm` command line.

    acvm db create | acvm db upgrade      schema (SQLite dev: create; PostgreSQL: alembic)
    acvm seed                             workspace, owner, niche accounts, licensed voices
    acvm command "주방 5개 쇼츠 만들어서 스레드랑 인스타에 올려줘"
    acvm approve <run_id> · acvm tick · acvm rules · acvm quant breakeven|simulate
    acvm serve · acvm worker · acvm loops
    acvm demo --days 3                    full offline simulation on a frozen clock
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import typer

from acvm.config import Settings, get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ACVM — AI 제휴 콘텐츠 자판기")
db_app = typer.Typer(help="Database schema")
quant_app = typer.Typer(help="Revenue math (§2)")
user_app = typer.Typer(help="Console members (e-mail + password login)")
app.add_typer(db_app, name="db")
app.add_typer(quant_app, name="quant")
app.add_typer(user_app, name="user")

NICHES = {"kitchen": ("kitchen_pick", "30대 여성 직장인"), "car": ("car_pick", "30대 남성 직장인"),
          "storage": ("tidy_pick", "40대 주부")}


def _out(obj: Any) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@db_app.command("create")
def db_create() -> None:
    """Create tables directly from the ORM (SQLite dev)."""
    from acvm.db.session import Database

    async def go() -> None:
        db = Database(get_settings().database_url)
        await db.create_all()
        await db.dispose()

    _run(go())
    typer.echo("schema created")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Alembic upgrade (PostgreSQL production path). Uses the packaged migration env, so it works from an
    installed wheel as well as from a source checkout."""
    from acvm.db.migrations import upgrade

    upgrade(get_settings().database_url, revision)


async def seed_workspace(ctx: Any, *, mature_days: int = 0, owner_email: str | None = None) -> uuid.UUID:
    """Idempotent seed: one workspace, an owner, 3 niche brands × 6 platforms (§7.1 #4 계정 = 브랜드)."""
    from acvm.core.enums import AccountStatus, Platform
    from acvm.core.ids import uuid7
    from acvm.db import tables as t
    from acvm.domain.models import ChannelAccount, Workspace

    now = ctx.clock.now()
    async with ctx.uow() as repos:
        ws = await repos.workspaces.first()
        if ws is None:
            ws = await repos.workspaces.add(Workspace(name="TBURN LABS", created_at=now,
                                                      daily_budget_krw=ctx.settings.workspace_daily_budget_krw))
            if ctx.settings.security.auth_mode != "session" or owner_email:
                # identity-provider modes map an e-mail to a role; session mode creates members with passwords
                # (`acvm user create … --role owner`), so no password-less placeholder is seeded there
                repos.session.add(t.AppUserRow(
                    id=uuid7(), workspace_id=ws.id,
                    email=owner_email or os.environ.get("ACVM_OWNER_EMAIL", "owner@acvm.local"),
                    display_name="Owner", role="owner", created_at=now))
        existing = {(a.platform, a.handle) for a in await repos.accounts.list(ws.id)}
        for niche, (handle, persona) in NICHES.items():
            for p in Platform:
                if (p, handle) in existing:
                    continue
                await repos.accounts.add(ChannelAccount(
                    workspace_id=ws.id, platform=p, handle=handle, platform_user_id=f"{p.value}-{handle}",
                    token_ref=f"tokens/{p.value}/{handle}", daily_cap=ctx.settings.quota.daily_cap[p.value],
                    status=AccountStatus.ACTIVE, niche=niche, persona_default=persona,
                    created_at=now - timedelta(days=mature_days)))
    return uuid.UUID(str(ws.id))


@app.command()
def seed(mature: bool = typer.Option(False, help="Backdate accounts 30 days (skips the 14-day HOLD)")) -> None:
    from acvm.context import build_context

    async def go() -> None:
        ctx = await build_context(get_settings(), create_schema=get_settings().database_url.startswith("sqlite"))
        ws = await seed_workspace(ctx, mature_days=30 if mature else 0)
        await ctx.close()
        typer.echo(f"workspace {ws} seeded")

    _run(go())


async def _local(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    from acvm.context import build_context
    from acvm.infra.platforms.registry import build_platform_clients
    from acvm.orchestration.local import LocalOrchestrator

    s = settings or get_settings()
    ctx = await build_context(s, create_schema=s.database_url.startswith("sqlite"))
    ctx.extras.setdefault("platform_clients", build_platform_clients(ctx))
    return ctx, LocalOrchestrator(ctx)


def _accounts_ctx() -> Any:
    from acvm.context import build_context

    settings = get_settings()
    return build_context(settings, create_schema=settings.database_url.startswith("sqlite"))


def _read_password(stdin: bool) -> str:
    if stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return str(typer.prompt("비밀번호", hide_input=True, confirmation_prompt="비밀번호 확인"))


@user_app.command("create")
def user_create(email: str, role: str = typer.Option("viewer", help="owner | operator | reviewer | viewer"),
                name: str = typer.Option("", help="표시 이름"),
                password_stdin: bool = typer.Option(False, help="비밀번호를 표준입력 한 줄로 받기 (스크립트용)")) -> None:
    """Create (or update) an active member. The password is typed at a hidden prompt — never as an argument."""
    from acvm.core.enums import Role
    from acvm.core.errors import ACVMError
    from acvm.security.accounts import Accounts

    password = _read_password(password_stdin)

    async def go() -> None:
        ctx = await _accounts_ctx()
        try:
            member = await Accounts(ctx).create(email, password, role=Role(role), name=name)
        except ACVMError as exc:
            typer.echo(f"실패: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await ctx.close()
        typer.echo(f"회원 저장됨: {member.email} · {member.role.value} · {member.status}")

    _run(go())


@user_app.command("set-password")
def user_set_password(email: str, password_stdin: bool = typer.Option(False)) -> None:
    """Reset a member's password (signs out all of their sessions)."""
    from acvm.core.errors import ACVMError
    from acvm.security.accounts import Accounts

    password = _read_password(password_stdin)

    async def go() -> None:
        ctx = await _accounts_ctx()
        try:
            accounts = Accounts(ctx)
            found = next((m for m in await accounts.list() if m.email == email.strip().lower()), None)
            if found is None:
                typer.echo("실패: 그런 회원이 없습니다", err=True)
                raise typer.Exit(1)
            await accounts.create(email, password, role=found.role, status=found.status)
        except ACVMError as exc:
            typer.echo(f"실패: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await ctx.close()
        typer.echo(f"비밀번호를 바꿨습니다: {email.strip().lower()}")

    _run(go())


@user_app.command("list")
def user_list() -> None:
    from acvm.security.accounts import Accounts

    async def go() -> None:
        ctx = await _accounts_ctx()
        try:
            for m in await Accounts(ctx).list():
                typer.echo(f"{m.email:40} {m.role.value:9} {m.status:9} 비밀번호 {'있음' if m.has_password else '없음'}")
        finally:
            await ctx.close()

    _run(go())


@user_app.command("disable")
def user_disable(email: str) -> None:
    from acvm.core.errors import ACVMError
    from acvm.security.accounts import Accounts

    async def go() -> None:
        ctx = await _accounts_ctx()
        try:
            accounts = Accounts(ctx)
            found = next((m for m in await accounts.list() if m.email == email.strip().lower()), None)
            if found is None:
                typer.echo("실패: 그런 회원이 없습니다", err=True)
                raise typer.Exit(1)
            await accounts.update(found.id, actor_id=None, status="disabled")
        except ACVMError as exc:
            typer.echo(f"실패: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await ctx.close()
        typer.echo(f"정지했습니다: {email.strip().lower()}")

    _run(go())


@app.command()
def command(text: str, actor: str = "cli@acvm.local", execute: bool = True) -> None:
    """Natural-language command → plan → (auto-approved) execution."""

    async def go() -> None:
        ctx, orch = await _local()
        async with ctx.uow() as repos:
            ws = await repos.workspaces.first()
        if ws is None:
            typer.echo("run `acvm seed` first")
            raise typer.Exit(1)
        run = await orch.submit(text, workspace_id=ws.id, actor=actor, execute=execute)
        _out({"run_id": run.id, "status": run.status.value, "plan": run.plan.explanation if run.plan else None,
              "approval": run.plan.approval.model_dump() if run.plan else None, "summary": run.summary})
        await ctx.close()

    _run(go())


@app.command()
def approve(run_id: str, by: str = "cli@acvm.local") -> None:
    async def go() -> None:
        ctx, orch = await _local()
        run = await orch.approve(uuid.UUID(run_id), by=by)
        _out({"run_id": run.id, "status": run.status.value, "summary": run.summary})
        await ctx.close()

    _run(go())


@app.command()
def tick() -> None:
    """One scheduler pass (publish due items, poll processing ones)."""

    async def go() -> None:
        ctx, orch = await _local()
        n = await orch.tick()
        typer.echo(f"{n} publication(s) processed")
        await ctx.close()

    _run(go())


@app.command()
def rules() -> None:
    from acvm.modules.m7_gate.ruleset import describe, fingerprint

    _out({"version": "v1", "fingerprint": fingerprint(), "rules": describe()})


@quant_app.command("breakeven")
def q_breakeven(c_unit: float = 150, ctr: float = 0.008, cvr: float = 0.03, aov: float = 30_000, r: float = 0.03) -> None:
    from acvm.quant.economics import Funnel, breakeven_impressions

    f = Funnel(ctr, cvr, aov, r)
    _out({"I*": round(breakeven_impressions(c_unit, f), 1), "commission_per_order": f.commission_per_order,
          "revenue_per_impression": f.revenue_per_impression, "epc": f.epc})


@quant_app.command("simulate")
def q_simulate(median: float = 600, sigma: float = 2.0, c_unit: float = 150, n: int = 300, q: float = 0.01) -> None:
    from acvm.quant.distributions import LogNormal
    from acvm.quant.economics import Funnel, content_economics, prob_at_least_one_tail, trials_for_tail

    s = get_settings().economics
    ce = content_economics(LogNormal.from_median(median, sigma), Funnel(s.ctr, s.cvr, s.aov_krw, s.commission_rate), c_unit)
    _out({"mean_views": round(ce.expected_impressions), "I*": round(ce.breakeven_views),
          "P(I>=I*)": round(ce.p_breakeven, 4), "E[profit]/content": round(ce.expected_profit, 1),
          "top1%_share": round(ce.tail_share_top1, 3), "top5%_share": round(ce.tail_share_top5, 3),
          f"P(>=1 top-{q:.0%} in {n})": round(prob_at_least_one_tail(q, n), 4), "trials_for_95%": trials_for_tail(q, 0.95)})


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104 — container entry point
    import uvicorn

    uvicorn.run("acvm.api.app:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


@app.command()
def worker(queue: str | None = None) -> None:
    from acvm.orchestration.worker import main

    _run(main(queue, 9464))


@app.command()
def loops() -> None:
    from acvm.orchestration.loops import main

    _run(main())


@app.command()
def demo(days: int = typer.Option(3, min=1, max=30), out_dir: str = "var/demo",
         command_text: str = "주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘",
         text_command: str = "차량용품 3개 글로 만들어서 스레드에 올려줘") -> None:
    """Offline end-to-end simulation (sandbox Coupang + sandbox platforms + real FFmpeg renders) on a
    frozen clock: commands → gate → scheduled publishing → insights/reports → bandit → amplification."""
    from acvm.orchestration.demo import run_demo

    _out(_run(run_demo(days=days, out_dir=out_dir, commands=[command_text, text_command])))


if __name__ == "__main__":
    app()


__all__ = ["UTC", "app", "datetime", "seed_workspace"]

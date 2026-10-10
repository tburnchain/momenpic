"""Periodic control loops: ``python -m acvm.orchestration.loops``.

Runs on every node but only the elected leader executes (Redis lock with TTL; single-node dev mode
is always leader). Every task is idempotent, so a leadership flap at worst repeats a no-op.

    every 2 s     outbox relay (transactional outbox → event bus)
    every 30 s    publish scheduler (local mode) / PublishWorkflow starter (temporal mode)
    every 60 s    recurring commands (cron, KST), gauges (queue depth, account status)
    every 15 min  invalid-click scan (M9) → link pause (M3)
    hourly        platform insights + attribution (M9 → M10/M2), account health re-evaluation (M8)
    every 6 h     link health sweep (M3)
    daily 10:00   Coupang reports D+1 (M9), playbook refresh (M10), retention (audit 1 y, outbox 7 d)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from acvm.config import get_settings
from acvm.context import AppContext, build_context
from acvm.core.clock import KST
from acvm.core.logging import configure_logging, get_logger
from acvm.core.metrics import ACCOUNT_STATUS, BUDGET_LIMIT_KRW, BUDGET_SPENT_KRW, QUEUE_DEPTH
from acvm.db import tables as t
from acvm.orchestration.cron import Cron
from acvm.orchestration.pipeline import Pipeline, wire_handlers

log = get_logger(__name__)


class Leader:
    def __init__(self, ctx: AppContext, ttl_s: int = 30) -> None:
        self.redis = ctx.extras.get("redis")
        self.ttl = ttl_s
        self.me = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    async def is_leader(self) -> bool:
        if self.redis is None:
            return True
        key = "acvm:leader:loops"
        if await self.redis.set(key, self.me, nx=True, ex=self.ttl):
            return True
        cur = await self.redis.get(key)
        if cur and cur.decode() == self.me:
            await self.redis.expire(key, self.ttl)
            return True
        return False


class Loops:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.pipeline = Pipeline(ctx)
        wire_handlers(ctx, self.pipeline)
        self.leader = Leader(ctx)
        self._last: dict[str, datetime] = {}

    def _due(self, name: str, every: timedelta) -> bool:
        now = self.ctx.clock.now()
        last = self._last.get(name)
        if last is None or now - last >= every:
            self._last[name] = now
            return True
        return False

    async def _safe(self, name: str, fn: Callable[[], Awaitable[object]]) -> None:
        try:
            await fn()
        except Exception as exc:
            log.exception("loop_failed", loop=name, error=str(exc)[:200])

    async def once(self) -> None:
        if not await self.leader.is_leader():
            return
        now = self.ctx.clock.now()
        local = now.astimezone(KST)
        await self._safe("relay", self.ctx.relay_outbox)
        if self._due("publish", timedelta(seconds=30)):
            await self._safe("publish", self._publish)
        if self._due("minute", timedelta(seconds=60)):
            await self._safe("recurring", self._recurring)
            await self._safe("gauges", self._gauges)
        if self._due("invalid_clicks", timedelta(minutes=15)):
            await self._safe("invalid_clicks", self.pipeline.analytics.detect_invalid_clicks)
        if self._due("hourly", timedelta(hours=1)):
            await self._safe("learn_hourly", self.pipeline.learn_hourly)
            await self._safe("accounts", self._accounts)
        if self._due("six_hourly", timedelta(hours=6)):
            await self._safe("link_health", self.pipeline.links.health_sweep)
        if local.hour >= 10 and self._due(f"daily:{local.date()}", timedelta(days=1)):
            await self._safe("daily", self._daily)

    async def _publish(self) -> None:
        if self.ctx.settings.orchestrator == "temporal":
            await self._start_publish_workflows()
        else:
            await self.pipeline.publish_due()

    async def _start_publish_workflows(self) -> None:
        from temporalio.common import WorkflowIDReusePolicy

        from acvm.orchestration.temporal.workflows import PublishWorkflow

        client = await self._temporal()
        due = await self.pipeline.dist.due(100)
        for pub in due:
            with contextlib.suppress(Exception):  # already running → WorkflowAlreadyStartedError
                await client.start_workflow(PublishWorkflow.run, str(pub.id), id=f"publish-{pub.id}",
                                            task_queue=self.ctx.settings.temporal_publish_queue,
                                            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)

    async def _recurring(self) -> None:
        now = self.ctx.clock.now().replace(second=0, microsecond=0)
        async with self.ctx.uow() as repos:
            rows = (await repos.session.execute(select(t.RecurringCommandRow)
                                                .where(t.RecurringCommandRow.active.is_(True)))).scalars().all()
        for row in rows:
            if not Cron(row.cron).matches(now):
                continue
            key = f"recurring:{row.id}:{now.isoformat()}"
            async with self.ctx.uow() as repos:
                if await repos.activities.get(key):
                    continue
                await repos.activities.put(key, "recurring", {"fired": True}, now)
            run = await self.pipeline.submit(row.raw_command, workspace_id=row.workspace_id, actor=f"cron:{row.created_by}")
            async with self.ctx.uow() as repos:
                db_row = await repos.session.get(t.RecurringCommandRow, row.id)
                if db_row is not None:
                    db_row.last_run_id = run.id
            if self.ctx.settings.orchestrator == "temporal":
                # durable path: the CommandWorkflow waits for console approval itself when the plan needs it
                from acvm.orchestration.temporal.activities import CommandInput
                from acvm.orchestration.temporal.workflows import CommandWorkflow

                client = await self._temporal()
                s = self.ctx.settings
                await client.start_workflow(CommandWorkflow.run,
                                            CommandInput(str(run.id), s.temporal_render_queue, s.temporal_publish_queue),
                                            id=f"command-{run.id}", task_queue=s.temporal_task_queue)
            elif run.status.value == "approved":
                from acvm.orchestration.local import LocalOrchestrator

                await LocalOrchestrator(self.ctx, pipeline=self.pipeline).execute(run.id)

    async def _temporal(self) -> Any:
        from temporalio.client import Client

        client = self.ctx.extras.get("temporal")
        if client is None:
            client = self.ctx.extras["temporal"] = await Client.connect(self.ctx.settings.temporal_host,
                                                                        namespace=self.ctx.settings.temporal_namespace)
        return client

    async def _gauges(self) -> None:
        async with self.ctx.uow() as repos:
            states = await repos.publications.state_counts(self.ctx.clock.now() - timedelta(days=30))
            for state, n in states.items():
                QUEUE_DEPTH.labels(queue=f"publication_{state.lower()}").set(n)
            ws = await repos.workspaces.first()
            if ws:
                QUEUE_DEPTH.labels(queue="hold_review").set(len(await repos.compliance.pending_reviews(ws.id)))
                for acc in await repos.accounts.list(ws.id):
                    for st in ("active", "cooldown", "restricted"):
                        ACCOUNT_STATUS.labels(platform=acc.platform.value, status=st).set(0)
                for acc in await repos.accounts.list(ws.id):
                    ACCOUNT_STATUS.labels(platform=acc.platform.value, status=acc.status.value).inc()
                spent = await repos.costs.workspace_total(ws.id, self.ctx.day_start_kst())
                BUDGET_SPENT_KRW.labels(workspace=str(ws.id)).set(spent)
                BUDGET_LIMIT_KRW.labels(workspace=str(ws.id)).set(await self.ctx.daily_budget(repos, ws.id))

    async def _accounts(self) -> None:
        async with self.ctx.uow() as repos:
            ws = await repos.workspaces.first()
            accounts = await repos.accounts.list(ws.id) if ws else []
        for acc in accounts:
            await self.pipeline.dist.evaluate_account(acc.id)

    async def _daily(self) -> None:
        async with self.ctx.uow() as repos:
            ws = await repos.workspaces.first()
        if ws is not None:
            await self.pipeline.learn_daily(ws.id)
        cutoff = self.ctx.clock.now() - timedelta(days=self.ctx.settings.security.audit_retention_days)
        async with self.ctx.uow() as repos:
            if self.ctx.db.dialect == "postgresql":
                from sqlalchemy import text

                await repos.session.execute(text("SET LOCAL acvm.retention = 'on'"))
            await repos.audit.purge_older_than(cutoff)
            await repos.outbox.purge(self.ctx.clock.now() - timedelta(days=7))

    async def run_forever(self, interval_s: float = 2.0) -> None:
        await self.ctx.bus.start()
        while True:
            await self.once()
            await asyncio.sleep(interval_s)


async def main(metrics_port: int = 9464) -> None:
    configure_logging()
    settings = get_settings()
    from prometheus_client import start_http_server

    # account-status / queue-depth / budget gauges live in this process. Containers scrape 0.0.0.0:9464; a shared
    # host runs it on loopback with its own port (ACVM_LOOPS_METRICS_ADDR / ACVM_LOOPS_METRICS_PORT).
    start_http_server(int(os.environ.get("ACVM_LOOPS_METRICS_PORT", metrics_port)),
                      addr=os.environ.get("ACVM_LOOPS_METRICS_ADDR", "0.0.0.0"))  # noqa: S104
    ctx = await build_context(settings)
    from acvm.infra.platforms.registry import build_platform_clients

    ctx.extras.setdefault("platform_clients", build_platform_clients(ctx))
    try:
        await Loops(ctx).run_forever()
    finally:
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())

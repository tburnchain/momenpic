"""Orchestrator gateways: the API talks to one of these, never to modules directly."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

from acvm.context import AppContext
from acvm.core.enums import RunStatus
from acvm.core.logging import get_logger
from acvm.domain.models import CommandRun
from acvm.orchestration.local import LocalOrchestrator
from acvm.orchestration.pipeline import Pipeline

log = get_logger(__name__)


class Gateway(Protocol):
    pipeline: Pipeline

    async def submit(self, text: str, *, workspace_id: uuid.UUID, actor: str) -> CommandRun: ...

    async def approve(self, run_id: uuid.UUID, *, by: str) -> CommandRun: ...

    async def reject(self, run_id: uuid.UUID, *, by: str, reason: str) -> None: ...

    async def close(self) -> None: ...


class LocalGateway:
    """Runs approved commands as background tasks in the API process (dev / single-node P0)."""

    def __init__(self, ctx: AppContext) -> None:
        self.orch = LocalOrchestrator(ctx)
        self.pipeline = self.orch.pipeline
        self._tasks: set[asyncio.Task[object]] = set()

    def _spawn(self, run_id: uuid.UUID) -> None:
        async def go() -> None:
            try:
                await self.orch.execute(run_id)
            except Exception:
                log.exception("run_failed", run=str(run_id))
                await self.pipeline.mark(run_id, RunStatus.FAILED)

        task = asyncio.create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def submit(self, text: str, *, workspace_id: uuid.UUID, actor: str) -> CommandRun:
        run = await self.orch.submit(text, workspace_id=workspace_id, actor=actor, execute=False)
        if run.status is RunStatus.APPROVED:
            self._spawn(run.id)
        return run

    async def approve(self, run_id: uuid.UUID, *, by: str) -> CommandRun:
        run = await self.pipeline.approve(run_id, by=by)
        if run.status is RunStatus.APPROVED:
            self._spawn(run_id)
        return run

    async def reject(self, run_id: uuid.UUID, *, by: str, reason: str) -> None:
        await self.pipeline.reject(run_id, by=by, reason=reason)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()


class TemporalGateway:
    """Durable execution: one CommandWorkflow per run; approvals are workflow signals."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.pipeline = Pipeline(ctx)
        self._client: Any = None

    async def client(self) -> Any:
        if self._client is None:
            from temporalio.client import Client

            self._client = await Client.connect(self.ctx.settings.temporal_host,
                                                namespace=self.ctx.settings.temporal_namespace)
        return self._client

    async def submit(self, text: str, *, workspace_id: uuid.UUID, actor: str) -> CommandRun:
        from acvm.orchestration.temporal.activities import CommandInput
        from acvm.orchestration.temporal.workflows import CommandWorkflow

        run = await self.pipeline.submit(text, workspace_id=workspace_id, actor=actor)
        c = await self.client()
        s = self.ctx.settings
        await c.start_workflow(CommandWorkflow.run, CommandInput(str(run.id), s.temporal_render_queue,
                                                                 s.temporal_publish_queue),
                               id=f"command-{run.id}", task_queue=s.temporal_task_queue)
        return run

    async def approve(self, run_id: uuid.UUID, *, by: str) -> CommandRun:
        c = await self.client()
        await c.get_workflow_handle(f"command-{run_id}").signal("approve", by)
        run, _ = await self.pipeline.load(run_id)
        return run

    async def reject(self, run_id: uuid.UUID, *, by: str, reason: str) -> None:
        await self.pipeline.reject(run_id, by=by, reason=reason)
        c = await self.client()
        await c.get_workflow_handle(f"command-{run_id}").signal("reject", by)

    async def close(self) -> None:
        return None


def build_gateway(ctx: AppContext) -> LocalGateway | TemporalGateway:
    return TemporalGateway(ctx) if ctx.settings.orchestrator == "temporal" else LocalGateway(ctx)

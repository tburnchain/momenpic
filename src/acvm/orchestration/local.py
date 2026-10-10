"""In-process orchestrator: same activities, same idempotency, no Temporal. Used for dev, tests and the
`acvm demo` simulation; production uses `acvm.orchestration.temporal` (identical semantics, durable)."""

from __future__ import annotations

import uuid

from acvm.context import AppContext
from acvm.core.enums import Intent, RunStatus
from acvm.core.logging import get_logger
from acvm.domain.models import CommandRun
from acvm.orchestration.pipeline import Pipeline, gather_limited, wire_handlers

log = get_logger(__name__)


class LocalOrchestrator:
    def __init__(self, ctx: AppContext, *, concurrency: int = 4, pipeline: Pipeline | None = None) -> None:
        self.ctx = ctx
        self.pipeline = pipeline or Pipeline(ctx)
        self.concurrency = concurrency
        wire_handlers(ctx, self.pipeline)

    async def submit(self, text: str, *, workspace_id: uuid.UUID, actor: str, execute: bool = True) -> CommandRun:
        run = await self.pipeline.submit(text, workspace_id=workspace_id, actor=actor)
        await self.ctx.relay_outbox()
        if execute and run.status is RunStatus.APPROVED:
            return await self.execute(run.id)
        return run

    async def approve(self, run_id: uuid.UUID, *, by: str, execute: bool = True) -> CommandRun:
        run = await self.pipeline.approve(run_id, by=by)
        if execute and run.status is RunStatus.APPROVED:
            return await self.execute(run_id)
        return run

    async def execute(self, run_id: uuid.UUID) -> CommandRun:
        run, plan = await self.pipeline.load(run_id)
        if plan.intent is Intent.ANALYZE:
            await self.pipeline.mark(run_id, RunStatus.COMPLETED, analysis="see /dashboards")
            run, _ = await self.pipeline.load(run_id)
            return run
        product_ids = await self.pipeline.select(run_id)
        results = await gather_limited([self.pipeline.produce(run_id, pid, i) for i, pid in enumerate(product_ids)],
                                       self.concurrency)
        run = await self.pipeline.finalize_run(run_id, list(results))
        await self.ctx.relay_outbox()
        return run

    async def tick(self) -> int:
        """One scheduler pass: publish what is due, poll processing items, relay events."""
        out = await self.pipeline.publish_due()
        await self.ctx.relay_outbox()
        return len(out)

"""Temporal activities — thin, typed wrappers over `Pipeline` (one process-wide AppContext)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from acvm.core.errors import ACVMError, QuotaExhausted
from acvm.orchestration.pipeline import Pipeline, ProductResult

NON_RETRYABLE = ["PolicyViolation", "ValidationFailed", "NotFound", "IntegrityViolation", "BudgetExceeded",
                 "ConfigError", "Conflict"]


@dataclass
class CommandInput:
    run_id: str
    render_queue: str = "acvm-render"
    publish_queue: str = "acvm-publish"


@dataclass
class ProduceInput:
    run_id: str
    product_id: int
    rank: int = 0


@dataclass
class FinalizeInput:
    run_id: str
    results: list[dict[str, Any]]


@dataclass
class ApproveInput:
    run_id: str
    by: str


def _wrap(exc: Exception) -> Exception:
    if isinstance(exc, QuotaExhausted):
        return ApplicationError(str(exc), type="QuotaExhausted", non_retryable=False,
                                next_retry_delay=timedelta(seconds=exc.retry_after_seconds))
    if isinstance(exc, ACVMError):
        return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=not exc.retryable)
    return exc


class Activities:
    def __init__(self, pipeline: Pipeline) -> None:
        self.p = pipeline

    @activity.defn(name="run_status")
    async def run_status(self, run_id: str) -> str:
        run, _ = await self.p.load(uuid.UUID(run_id))
        return run.status.value

    @activity.defn(name="approve_run")
    async def approve_run(self, inp: ApproveInput) -> str:
        try:
            run = await self.p.approve(uuid.UUID(inp.run_id), by=inp.by)
        except Exception as exc:
            raise _wrap(exc) from exc
        return run.status.value

    @activity.defn(name="select_products")
    async def select_products(self, run_id: str) -> list[int]:
        try:
            return await self.p.select(uuid.UUID(run_id))
        except Exception as exc:
            raise _wrap(exc) from exc

    @activity.defn(name="produce_product")
    async def produce_product(self, inp: ProduceInput) -> dict[str, Any]:
        try:
            res = await self.p.produce(uuid.UUID(inp.run_id), inp.product_id, inp.rank)
        except Exception as exc:
            raise _wrap(exc) from exc
        return res.as_dict()

    @activity.defn(name="finalize_run")
    async def finalize_run(self, inp: FinalizeInput) -> str:
        results = [ProductResult(product_id=r["product_id"],
                                 content_id=uuid.UUID(r["content_id"]) if r.get("content_id") else None,
                                 verdict=r.get("verdict", ""), publications=[uuid.UUID(x) for x in r.get("publications", [])],
                                 notes=r.get("notes", [])) for r in inp.results]
        run = await self.p.finalize_run(uuid.UUID(inp.run_id), results)
        await self.p.ctx.relay_outbox()
        return run.status.value

    @activity.defn(name="publish_publication")
    async def publish_publication(self, publication_id: str) -> dict[str, Any]:
        try:
            pub = await self.p.publish_publication(uuid.UUID(publication_id))
        except Exception as exc:
            raise _wrap(exc) from exc
        await self.p.ctx.relay_outbox()
        return {"state": pub.state.value, "scheduled_at": pub.scheduled_at.isoformat()}

    def all(self) -> list[Any]:
        return [*self.main(), *self.render(), *self.publish()]

    def main(self) -> list[Any]:
        """acvm-main: orchestration bookkeeping only — no ffmpeg, no social tokens."""
        return [self.run_status, self.approve_run, self.select_products, self.finalize_run]

    def render(self) -> list[Any]:
        """acvm-render: script → assets → FFmpeg render → gate → links → schedule."""
        return [self.produce_product]

    def publish(self) -> list[Any]:
        """acvm-publish: the only activity that reads social-account tokens (§10)."""
        return [self.publish_publication]

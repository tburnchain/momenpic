"""Temporal workflows (§5, §9.1: long-running, retry and saga semantics in code).

CommandWorkflow  — approval gate (signal or auto) → select → fan-out produce per product → finalize →
                   one PublishWorkflow per scheduled publication (ParentClosePolicy.ABANDON so a run can end
                   while its posts are still days away).
PublishWorkflow  — durable timer until scheduled_at → publish activity (price refresh + preflight + M8) →
                   follow deferrals / processing polls until a terminal state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from acvm.orchestration.temporal.activities import (
        NON_RETRYABLE,
        ApproveInput,
        CommandInput,
        FinalizeInput,
        ProduceInput,
    )

ACTIVITY_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                             maximum_interval=timedelta(minutes=10), maximum_attempts=8,
                             non_retryable_error_types=NON_RETRYABLE)
APPROVAL_TIMEOUT = timedelta(hours=24)
FANOUT = 4


async def _act(name: str, arg: Any, *, minutes: int = 10, task_queue: str | None = None) -> Any:
    """Activities are invoked by registered name so workflow code never imports activity implementations.
    ``task_queue`` routes heavy/privileged work to dedicated workers (render: ffmpeg; publish: tokens)."""
    return await workflow.execute_activity(name, arg, start_to_close_timeout=timedelta(minutes=minutes),
                                           retry_policy=ACTIVITY_RETRY, task_queue=task_queue)


@workflow.defn(name="CommandWorkflow")
class CommandWorkflow:
    def __init__(self) -> None:
        self._decision: str | None = None
        self._by = ""

    @workflow.signal
    def approve(self, by: str) -> None:
        self._decision, self._by = "approve", by

    @workflow.signal
    def reject(self, by: str) -> None:
        self._decision, self._by = "reject", by

    @workflow.query
    def decision(self) -> str | None:
        return self._decision

    @workflow.run
    async def run(self, inp: CommandInput) -> dict[str, Any]:
        run_id = inp.run_id
        status = await _act("run_status", run_id)
        if status == "awaiting_approval":
            try:
                await workflow.wait_condition(lambda: self._decision is not None, timeout=APPROVAL_TIMEOUT)
            except TimeoutError:
                return {"run_id": run_id, "status": "approval_timeout"}
            if self._decision == "reject":
                return {"run_id": run_id, "status": "rejected"}
            await _act("approve_run", ApproveInput(run_id, self._by))
        elif status not in ("approved", "running"):
            return {"run_id": run_id, "status": status}

        product_ids: list[int] = await _act("select_products", run_id)
        results: list[dict[str, Any]] = []
        for i in range(0, len(product_ids), FANOUT):
            batch = product_ids[i : i + FANOUT]
            results += await asyncio.gather(*(_act("produce_product", ProduceInput(run_id, pid, i + j), minutes=20,
                                                   task_queue=inp.render_queue)
                                              for j, pid in enumerate(batch)))
        final = await _act("finalize_run", FinalizeInput(run_id, results))
        for r in results:
            for pub_id in r.get("publications", []):
                await workflow.start_child_workflow(
                    PublishWorkflow.run, pub_id, id=f"publish-{pub_id}", task_queue=inp.publish_queue,
                    parent_close_policy=ParentClosePolicy.ABANDON)
        return {"run_id": run_id, "status": final, "products": len(results)}


@workflow.defn(name="PublishWorkflow")
class PublishWorkflow:
    @workflow.run
    async def run(self, publication_id: str) -> str:
        for _ in range(200):  # bounded: deferrals + processing polls
            res: dict[str, str] = await _act("publish_publication", publication_id, minutes=15)
            state = res["state"]
            if state in ("PUBLISHED", "FAILED", "EXPIRED", "CANCELLED", "TAKEN_DOWN", "MANUAL_PENDING"):
                return state
            if state == "PROCESSING":
                await workflow.sleep(timedelta(seconds=30))
                continue
            when = datetime.fromisoformat(res["scheduled_at"])
            delay = (when - workflow.now()).total_seconds()
            await workflow.sleep(timedelta(seconds=max(5.0, delay)))
        return "gave_up"

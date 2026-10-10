"""Temporal worker entry point: ``python -m acvm.orchestration.worker [--queue acvm-render]``.

Each process serves exactly the roles whose queue it listens on (§10 least privilege):

    acvm-main     CommandWorkflow + bookkeeping activities          (Vault role acvm-worker)
    acvm-render   produce_product: script · FFmpeg render · gate    (acvm-worker, render image)
    acvm-publish  PublishWorkflow + publish_publication              (acvm-publisher — social tokens)

When several roles share one queue name (single-node dev), one worker serves them all.
Metrics are exposed on :9464 for Prometheus; tracing is enabled when ACVM_OTEL_ENDPOINT is set.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from prometheus_client import start_http_server
from temporalio.client import Client
from temporalio.worker import Worker

from acvm.config import Settings, get_settings
from acvm.context import build_context
from acvm.core.logging import configure_logging, get_logger
from acvm.orchestration.pipeline import Pipeline, wire_handlers
from acvm.orchestration.telemetry import setup_tracing
from acvm.orchestration.temporal.activities import Activities
from acvm.orchestration.temporal.workflows import CommandWorkflow, PublishWorkflow

log = get_logger(__name__)


def roles_for(queue: str, s: Settings, acts: Activities) -> tuple[list[Any], list[Any]]:
    workflows: list[Any] = []
    activities: list[Any] = []
    if queue == s.temporal_task_queue:
        workflows.append(CommandWorkflow)
        activities += acts.main()
    if queue == s.temporal_render_queue:
        activities += acts.render()
    if queue == s.temporal_publish_queue:
        workflows.append(PublishWorkflow)
        activities += acts.publish()
    if not workflows and not activities:
        raise SystemExit(f"queue {queue!r} matches no role (main={s.temporal_task_queue}, "
                         f"render={s.temporal_render_queue}, publish={s.temporal_publish_queue})")
    return workflows, activities


async def main(queue: str | None, metrics_port: int) -> None:
    settings = get_settings()
    configure_logging()
    setup_tracing(settings, service="acvm-worker")
    start_http_server(metrics_port)
    ctx = await build_context(settings)
    pipeline = Pipeline(ctx)
    wire_handlers(ctx, pipeline)
    await ctx.bus.start()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    q = queue or settings.temporal_task_queue
    workflows, activities = roles_for(q, settings, Activities(pipeline))
    worker = Worker(client, task_queue=q, workflows=workflows, activities=activities,
                    max_concurrent_activities=settings.render.concurrency * 2)
    log.info("worker_started", queue=q, workflows=[w.__name__ for w in workflows],
             activities=[getattr(a, "__name__", str(a)) for a in activities])
    try:
        await worker.run()
    finally:
        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=None)
    ap.add_argument("--metrics-port", type=int, default=9464)
    a = ap.parse_args()
    asyncio.run(main(a.queue, a.metrics_port))

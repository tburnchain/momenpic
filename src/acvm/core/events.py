"""§6.3 event contracts and the bus that carries them.

Modules never call each other (§4 preamble): a module consumes exactly one input event type and
emits events. The envelope carries an idempotency key; consumers de-duplicate on it, so the
at-least-once delivery of Redis Streams / Temporal retries never produces duplicate side effects.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import orjson
from pydantic import BaseModel, ConfigDict, Field

from acvm.core.enums import Platform, PublicationState, Verdict
from acvm.core.ids import uuid7
from acvm.core.logging import get_logger

log = get_logger(__name__)


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    TYPE: ClassVar[str]
    PRODUCER: ClassVar[str]
    CONSUMERS: ClassVar[tuple[str, ...]]


# ─────────────── §6.3 table (exact) ───────────────


class ProductSelected(EventPayload):
    TYPE = "product.selected"
    PRODUCER = "M1"
    CONSUMERS = ("M3", "M4")
    product_id: int
    pos: float
    run_id: uuid.UUID
    rank: int = 0


class ScriptReady(EventPayload):
    TYPE = "script.ready"
    PRODUCER = "M4"
    CONSUMERS = ("M5", "M6")
    content_id: uuid.UUID
    claims: list[dict[str, Any]]
    persona: str


class RenderDone(EventPayload):
    TYPE = "render.done"
    PRODUCER = "M6"
    CONSUMERS = ("M7",)
    content_id: uuid.UUID
    media_uri: str
    duration: float


class GateDecided(EventPayload):
    TYPE = "gate.decided"
    PRODUCER = "M7"
    CONSUMERS = ("M8", "console")
    content_id: uuid.UUID
    verdict: Verdict
    findings: list[dict[str, Any]]
    check_id: uuid.UUID


class PublicationStateChanged(EventPayload):
    TYPE = "publication.state_changed"
    PRODUCER = "M8"
    CONSUMERS = ("M9", "console")
    publication_id: uuid.UUID
    state: PublicationState
    platform: Platform
    content_id: uuid.UUID


class PerfUpdated(EventPayload):
    TYPE = "perf.updated"
    PRODUCER = "M9"
    CONSUMERS = ("M10",)
    content_id: uuid.UUID
    epc: float
    percentile: float
    platform: Platform | None = None


class ArmUpdated(EventPayload):
    TYPE = "arm.updated"
    PRODUCER = "M10"
    CONSUMERS = ("M1", "M4")
    arm_key: str
    posterior: dict[str, float]


# ─────────────── supporting events (control plane & risk) ───────────────


class LinkCreated(EventPayload):
    TYPE = "link.created"
    PRODUCER = "M3"
    CONSUMERS = ("M8",)
    link_id: uuid.UUID
    sub_id: str
    product_id: int


class LinkHealthChanged(EventPayload):
    TYPE = "link.health_changed"
    PRODUCER = "M3"
    CONSUMERS = ("M8", "console")
    link_id: uuid.UUID
    health: str
    replacement_product_id: int | None = None


class AccountStatusChanged(EventPayload):
    TYPE = "account.status_changed"
    PRODUCER = "M8"
    CONSUMERS = ("console", "M10")
    account_id: uuid.UUID
    status: str
    reason: str
    until: datetime | None = None


class InvalidClickDetected(EventPayload):
    TYPE = "invalid_click.detected"
    PRODUCER = "M9"
    CONSUMERS = ("M3", "console")
    link_id: uuid.UUID | None
    sub_id: str
    reason: str
    score: float


class AmplifyRequested(EventPayload):
    TYPE = "amplify.requested"
    PRODUCER = "M10"
    CONSUMERS = ("orchestrator",)
    content_id: uuid.UUID
    variants: int
    cross_platforms: list[Platform]


class CommandPlanned(EventPayload):
    TYPE = "command.planned"
    PRODUCER = "orchestrator"
    CONSUMERS = ("console",)
    run_id: uuid.UUID
    approval: str
    est_cost_krw: float


EVENT_TYPES: dict[str, type[EventPayload]] = {
    cls.TYPE: cls
    for cls in (
        ProductSelected, ScriptReady, RenderDone, GateDecided, PublicationStateChanged, PerfUpdated,
        ArmUpdated, LinkCreated, LinkHealthChanged, AccountStatusChanged, InvalidClickDetected,
        AmplifyRequested, CommandPlanned,
    )
}


class Event(BaseModel):
    """Envelope. ``idempotency_key`` defaults to the event id but producers set a semantic key
    (e.g. ``content_id:verdict:iteration``) so a replayed producer emits a de-duplicable event."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid7)
    type: str
    version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    workspace_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    trace_id: str | None = None
    idempotency_key: str = ""
    payload: dict[str, Any]

    @classmethod
    def of(cls, payload: EventPayload, *, workspace_id: uuid.UUID | None = None,
           run_id: uuid.UUID | None = None, idempotency_key: str | None = None,
           trace_id: str | None = None, occurred_at: datetime | None = None) -> Event:
        """Build an envelope. Pass ``occurred_at=ctx.clock.now()`` so replays/simulations stay deterministic."""
        ev = cls(type=payload.TYPE, workspace_id=workspace_id, run_id=run_id, trace_id=trace_id,
                 payload=payload.model_dump(mode="json"),
                 **({"occurred_at": occurred_at} if occurred_at is not None else {}))
        ev.idempotency_key = idempotency_key or str(ev.id)
        return ev

    def parsed(self) -> EventPayload:
        return EVENT_TYPES[self.type].model_validate(self.payload)

    def to_bytes(self) -> bytes:
        return orjson.dumps(self.model_dump(mode="json"))

    @classmethod
    def from_bytes(cls, raw: bytes | str) -> Event:
        return cls.model_validate(orjson.loads(raw))


Handler = Callable[[Event], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(self, event_type: str, group: str, handler: Handler) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class InMemoryEventBus:
    """Synchronous-dispatch bus for tests and the single-process dev runtime.

    De-duplicates per (group, idempotency_key) exactly like the Redis consumer does, so tests
    exercise the same exactly-once-effect semantics as production.
    """

    def __init__(self, *, raise_handler_errors: bool = False) -> None:
        self._subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self._seen: set[tuple[str, str]] = set()
        self.history: list[Event] = []
        self.failures: list[tuple[Event, str, BaseException]] = []
        self.raise_handler_errors = raise_handler_errors

    def subscribe(self, event_type: str, group: str, handler: Handler) -> None:
        self._subs[event_type].append((group, handler))

    async def publish(self, event: Event) -> None:
        """Consumer isolation matches Redis Streams: a failing consumer group never breaks the publisher or
        the other groups. Its failure is logged and kept in ``failures``; the dedupe mark is dropped so a
        re-publish of the same event is redelivered to that group (as an un-ACKed stream entry would be)."""
        self.history.append(event)
        for group, handler in list(self._subs.get(event.type, [])) + list(self._subs.get("*", [])):
            key = (group, event.idempotency_key or str(event.id))
            if key in self._seen:
                continue
            self._seen.add(key)
            try:
                await handler(event)
            except Exception as exc:
                self._seen.discard(key)
                self.failures.append((event, group, exc))
                log.exception("event_handler_failed", type=event.type, group=group)
                if self.raise_handler_errors:
                    raise

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def of_type(self, event_type: str) -> list[Event]:
        return [e for e in self.history if e.type == event_type]


class RedisStreamsEventBus:
    """Production bus: one Redis Stream per event type, one consumer group per subscriber.

    * at-least-once: entries are ACKed only after the handler returns;
    * exactly-once *effect*: a per-group SET NX on the idempotency key (7-day TTL) drops replays;
    * crash recovery: pending entries idle > ``claim_idle_ms`` are XAUTOCLAIMed by live consumers.
    """

    STREAM_PREFIX = "acvm:ev:"

    def __init__(self, redis: Any, consumer: str | None = None, *, maxlen: int = 1_000_000,
                 claim_idle_ms: int = 60_000, dedupe_ttl_s: int = 7 * 86400) -> None:
        self.redis = redis
        self.consumer = consumer or f"c-{uuid.uuid4().hex[:8]}"
        self.maxlen = maxlen
        self.claim_idle_ms = claim_idle_ms
        self.dedupe_ttl_s = dedupe_ttl_s
        self._subs: list[tuple[str, str, Handler]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    def subscribe(self, event_type: str, group: str, handler: Handler) -> None:
        self._subs.append((event_type, group, handler))

    async def publish(self, event: Event) -> None:
        await self.redis.xadd(self.STREAM_PREFIX + event.type, {"e": event.to_bytes()},
                              maxlen=self.maxlen, approximate=True)

    async def start(self) -> None:
        for event_type, group, handler in self._subs:
            stream = self.STREAM_PREFIX + event_type
            try:
                await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            except Exception as exc:  # BUSYGROUP — group already exists
                if "BUSYGROUP" not in str(exc):
                    raise
            self._tasks.append(asyncio.create_task(self._consume(stream, group, handler)))

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle(self, stream: str, group: str, entry_id: Any, fields: dict[Any, Any],
                      handler: Handler) -> None:
        raw = fields.get(b"e") or fields.get("e")
        if raw is None:
            await self.redis.xack(stream, group, entry_id)  # malformed entry: drop, never block the group
            return
        event = Event.from_bytes(raw)
        dedupe = f"acvm:dedupe:{group}:{event.idempotency_key or event.id}"
        if await self.redis.set(dedupe, "1", nx=True, ex=self.dedupe_ttl_s):
            try:
                await handler(event)
            except Exception:
                await self.redis.delete(dedupe)
                log.exception("event_handler_failed", stream=stream, group=group)
                return  # leave pending; XAUTOCLAIM will retry it
        await self.redis.xack(stream, group, entry_id)

    async def _consume(self, stream: str, group: str, handler: Handler) -> None:
        while not self._stopping.is_set():
            try:
                claimed = await self.redis.xautoclaim(stream, group, self.consumer,
                                                      min_idle_time=self.claim_idle_ms, count=16)
                for entry_id, fields in (claimed[1] if claimed else []):
                    if fields:
                        await self._handle(stream, group, entry_id, fields, handler)
                batch = await self.redis.xreadgroup(group, self.consumer, {stream: ">"}, count=32,
                                                    block=2000)
                for _, entries in batch or []:
                    for entry_id, fields in entries:
                        await self._handle(stream, group, entry_id, fields, handler)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event_consumer_error", stream=stream, group=group)
                await asyncio.sleep(1.0)

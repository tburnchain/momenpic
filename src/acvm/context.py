"""Composition root: builds every adapter from `Settings` and hands modules one `AppContext`."""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from acvm.config import Settings
from acvm.core.clock import KST, Clock, SystemClock
from acvm.core.errors import BudgetExceeded
from acvm.core.events import Event, EventBus, InMemoryEventBus
from acvm.core.logging import get_logger
from acvm.db.repos import Repos
from acvm.db.session import Database
from acvm.infra.embeddings import build_embedder
from acvm.infra.objects import build_object_store
from acvm.infra.perf_memory import MemoryPerfStore
from acvm.infra.quota import MemoryQuotaStore, RedisQuotaStore
from acvm.infra.secrets import EnvSecretStore, build_secret_store
from acvm.ports import LLM, TTS, Embedder, ObjectStore, PerfStore, QuotaStore, SecretStore

log = get_logger(__name__)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    bus: EventBus
    clock: Clock
    secrets: SecretStore
    objects: ObjectStore
    quota: QuotaStore
    perf: PerfStore
    embedder: Embedder
    llm: LLM | None = None  # None → agents use their deterministic strategies
    tts: TTS | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # module-owned singletons (clients, caches)

    # ── helpers ──
    def rng(self, *parts: object) -> random.Random:
        """Deterministic RNG per logical decision (replay-safe under Temporal)."""
        seed = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
        return random.Random(int.from_bytes(seed, "big"))

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[Repos]:
        """Unit of work; events added to ``repos.outbox`` commit atomically with the state change."""
        async with self.db.session() as s:
            yield Repos(s)

    async def emit(self, repos: Repos, event: Event) -> None:
        """Transactional outbox write; the relay publishes after commit."""
        await repos.outbox.add(event)

    async def relay_outbox(self, limit: int = 500) -> int:
        """Publish committed outbox events to the bus (at-least-once; consumers de-duplicate)."""
        async with self.uow() as repos:
            events = await repos.outbox.pending(limit)
        published: list[uuid.UUID] = []
        for ev in events:
            await self.bus.publish(ev)
            published.append(ev.id)
        if published:
            async with self.uow() as repos:
                await repos.outbox.mark_published(published, self.clock.now())
        return len(published)

    def day_start_kst(self, at: datetime | None = None) -> datetime:
        now = (at or self.clock.now()).astimezone(KST)
        return datetime.combine(now.date(), time(0, 0), KST)

    async def charge(self, repos: Repos, *, workspace_id: uuid.UUID, run_id: uuid.UUID | None, stage: str,
                     amount_krw: float, key: str, cap_krw: float | None = None,
                     content_id: uuid.UUID | None = None, detail: dict[str, Any] | None = None) -> None:
        """Record spend; raise BudgetExceeded when the run cap or workspace daily budget is hit (§5.2)."""
        if cap_krw is not None and run_id is not None:
            spent = await repos.costs.run_total(run_id)
            if spent + amount_krw > cap_krw + 1e-9:
                raise BudgetExceeded("run budget cap reached", spent=spent, cap=cap_krw)
        day_spent = await repos.costs.workspace_total(workspace_id, self.day_start_kst())
        budget = await self.daily_budget(repos, workspace_id)
        if day_spent + amount_krw > budget + 1e-9:
            raise BudgetExceeded("workspace daily budget reached", spent=day_spent, budget=budget)
        await repos.costs.charge(workspace_id=workspace_id, run_id=run_id, stage=stage, amount_krw=amount_krw,
                                 key=key, at=self.clock.now(), content_id=content_id, detail=detail)

    async def daily_budget(self, repos: Repos, workspace_id: uuid.UUID) -> float:
        """Operator-editable workspace budget (console › 설정); the env setting is the default for new workspaces."""
        ws = await repos.workspaces.get(workspace_id)
        return float(ws.daily_budget_krw) if ws is not None else self.settings.workspace_daily_budget_krw

    async def close(self) -> None:
        await self.bus.stop()
        await self.db.dispose()


async def build_context(settings: Settings, *, clock: Clock | None = None, bus: EventBus | None = None,
                        create_schema: bool = False) -> AppContext:
    settings.assert_production_safe()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url)
    if create_schema:
        await db.create_all()
    secrets = build_secret_store(settings.security, data_dir=settings.data_dir, env=settings.env)
    redis = None
    if settings.redis_url:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(settings.redis_url)
    quota: QuotaStore = RedisQuotaStore(redis) if redis is not None else MemoryQuotaStore()
    if bus is None:
        if redis is not None:
            from acvm.core.events import RedisStreamsEventBus

            bus = RedisStreamsEventBus(redis)
        else:
            bus = InMemoryEventBus()
    perf: PerfStore
    if settings.clickhouse_url:
        from acvm.infra.perf_clickhouse import ClickHousePerfStore

        perf = ClickHousePerfStore(settings.clickhouse_url)
    elif not settings.database_url.startswith("sqlite+aiosqlite:///:memory:"):
        from acvm.infra.perf_sql import SqlPerfStore

        perf = SqlPerfStore(db)
        await perf.ensure_schema()
    else:
        perf = MemoryPerfStore()
    llm: LLM | None = None
    if settings.llm.backend != "offline":
        llm = await resolve_llm(settings, secrets)
    ctx = AppContext(
        settings=settings, db=db, bus=bus, clock=clock or SystemClock(), secrets=secrets,
        objects=build_object_store(settings.objects, secrets, settings.data_dir / "cache"),
        quota=quota, perf=perf, embedder=build_embedder(settings.embeddings, secrets), llm=llm)
    if redis is not None:
        ctx.extras["redis"] = redis
    return ctx


async def resolve_llm(settings: Settings, secrets: SecretStore, *, disabled: set[str] | frozenset[str] = frozenset()
                      ) -> LLM | None:
    """The LLM chain from the keys that exist right now (secret store first — §10 — then env).

    * Claude when its key is present (``ANTHROPIC_API_KEY`` env counts), Gemini when its key is present;
    * both → :class:`~acvm.infra.llm.ChainLLM` (Claude first, Gemini takes over on outages/rate limits);
    * ``backend="anthropic"`` always builds the Claude client (the SDK may resolve an ``ant`` profile);
      ``"gemini"`` uses Gemini only; ``"auto"`` with no key → None (deterministic agents).

    ``disabled`` holds integrations the operator paused in the console. Called again whenever a key is
    registered/removed or a switch flips, so no restart is needed.
    """
    import os

    from acvm.infra.llm import AnthropicLLM, ChainLLM

    async def _get(ref: str) -> str | None:
        try:
            return (await secrets.get(ref)).strip() or None
        except Exception:
            return None

    backend = settings.llm.backend
    members: list[Any] = []
    claude_key = await _get("anthropic/api_key")
    if "anthropic" not in disabled and backend != "gemini" and (
            claude_key or os.environ.get("ANTHROPIC_API_KEY") or backend == "anthropic"):
        members.append(AnthropicLLM(settings.llm, api_key=claude_key))
    gemini_key = await _get(settings.gemini.key_ref)
    if gemini_key and "gemini" not in disabled and backend in ("auto", "gemini"):
        from acvm.infra.gemini import GeminiLLM

        members.append(GeminiLLM(settings.gemini, api_key=gemini_key))
    if not members:
        return None
    return members[0] if len(members) == 1 else ChainLLM(members)


async def build_test_context(settings: Settings | None = None, clock: Clock | None = None,
                             tmp_dir: str | None = None) -> AppContext:
    """Fully in-process context: SQLite, memory bus/quota/perf, env secrets.

    With ``tmp_dir`` the database is a file (one connection per session, real transaction isolation).
    In-memory SQLite shares a single connection between all sessions, so under concurrent production
    (gather over products) one coroutine's rollback would silently discard another's uncommitted
    writes — never use it for anything that runs activities concurrently.
    """
    from acvm.config import ObjectStoreConfig

    db_url = f"sqlite+aiosqlite:///{tmp_dir}/acvm.db".replace("\\", "/") if tmp_dir else "sqlite+aiosqlite:///:memory:"
    s = settings or Settings(env="test", database_url=db_url)
    if tmp_dir:
        s.objects = ObjectStoreConfig(backend="local", local_root=f"{tmp_dir}/objects")
        s.data_dir = __import__("pathlib").Path(tmp_dir)
    db = Database(s.database_url)
    await db.create_all()
    secrets = EnvSecretStore({
        s.security.link_signing_key_ref: "test-link-signing-key",
        s.security.click_ip_salt_ref: "test-salt",
        s.coupang.access_key_ref: "test-access",
        s.coupang.secret_key_ref: "test-secret",
    })
    return AppContext(settings=s, db=db, bus=InMemoryEventBus(), clock=clock or SystemClock(), secrets=secrets,
                      objects=build_object_store(s.objects, secrets, s.data_dir / "cache"),
                      quota=MemoryQuotaStore(), perf=MemoryPerfStore(),
                      embedder=build_embedder(s.embeddings, secrets))


def kst_day_window(day: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(day.astimezone(KST).date(), time(0, 0), KST)
    return start, start + timedelta(days=1)

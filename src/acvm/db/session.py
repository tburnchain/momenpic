"""Async engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import Connection, event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from acvm.db.tables import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        kwargs: dict[str, object] = {"echo": echo}
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=1800)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if self.dialect == "sqlite":
                await conn.run_sync(_sqlite_add_missing_columns)

    async def drop_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: commits on success, rolls back on any exception."""
        async with self.sessions() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


def _sqlite_add_missing_columns(conn: Connection) -> list[str]:
    """Dev-only schema catch-up for SQLite files created by an older build: ``create_all`` never alters
    existing tables, so a column added to the ORM later would 500 every query touching it. Missing
    columns are added with their ORM default (nullable). PostgreSQL never takes this path — production
    schema changes go through Alembic revisions."""
    insp = inspect(conn)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            default = ""
            arg = getattr(col.default, "arg", None)
            if col.default is not None and getattr(col.default, "is_scalar", False):
                default = f" DEFAULT {_sql_literal(arg)}"
            elif callable(arg):
                probe = getattr(arg, "__name__", "")
                default = " DEFAULT '{}'" if probe == "dict" else (" DEFAULT '[]'" if probe == "list" else "")
            conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}')
            added.append(f"{table.name}.{col.name}")
    return added


def _sql_literal(v: object) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"

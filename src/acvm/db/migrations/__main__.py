"""``python -m acvm.db.migrations {upgrade|current|wait|sql}`` — dependency-free migration entry point.

* ``upgrade [REV]``        apply migrations (default head) to ACVM_DATABASE_URL
* ``current``              print the database revision
* ``wait [--timeout S]``   block until the database is at head (k8s init containers: the migrate Job owns upgrades)
* ``sql [REV]``            print the offline DDL for review (e.g. against a postgresql URL)
"""

from __future__ import annotations

import argparse
import sys
import time

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from acvm.db.migrations import alembic_config, current, upgrade


def _db_revision(url: str) -> str | None:
    if make_url(url).get_dialect().is_async:
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine

        async def _get() -> str | None:
            engine = create_async_engine(url, poolclass=pool.NullPool)
            try:
                async with engine.connect() as conn:
                    return await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
            finally:
                await engine.dispose()

        return asyncio.run(_get())
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _resolve_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    import os

    env = os.environ.get("ACVM_DATABASE_URL")
    if env:
        return env
    from acvm.config import Settings

    return Settings().database_url


def wait_for_head(url: str, timeout: float, interval: float = 5.0) -> bool:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    deadline = time.monotonic() + timeout
    while True:
        try:
            rev = _db_revision(url)
        except Exception as exc:  # database not reachable yet
            rev = None
            print(f"waiting for database: {type(exc).__name__}", file=sys.stderr)
        if rev == head:
            print(f"database at head {head}")
            return True
        if time.monotonic() >= deadline:
            print(f"timeout: database revision {rev!r}, expected {head!r}", file=sys.stderr)
            return False
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m acvm.db.migrations")
    ap.add_argument("--url", default=None, help="database URL (default: ACVM_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upgrade")
    up.add_argument("revision", nargs="?", default="head")
    sub.add_parser("current")
    w = sub.add_parser("wait")
    w.add_argument("--timeout", type=float, default=600.0)
    sq = sub.add_parser("sql")
    sq.add_argument("revision", nargs="?", default="head")
    args = ap.parse_args(argv)

    url = _resolve_url(args.url)
    if args.cmd == "upgrade":
        upgrade(url, args.revision)
    elif args.cmd == "current":
        current(url)
    elif args.cmd == "wait":
        return 0 if wait_for_head(url, args.timeout) else 1
    elif args.cmd == "sql":
        upgrade(url, args.revision, sql=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

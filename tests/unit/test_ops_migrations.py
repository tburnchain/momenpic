"""Alembic migrations: SQLite round-trip (online) and PostgreSQL DDL review (offline, no server needed)."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from acvm.db.migrations import MIGRATIONS_DIR, alembic_config
from acvm.db.tables import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
PG_URL = "postgresql+asyncpg://acvm:secret%40x@db.internal:5432/acvm"  # '%' must survive ConfigParser


def _sqlite_urls(tmp_path: Path) -> tuple[str, str]:
    db = (tmp_path / "migrate.db").as_posix()
    return f"sqlite+aiosqlite:///{db}", f"sqlite:///{db}"


def _offline_sql(url: str, *, downgrade: bool = False) -> str:
    buf = io.StringIO()
    cfg = alembic_config(url, output_buffer=buf)
    if downgrade:
        command.downgrade(cfg, "head:base", sql=True)
    else:
        command.upgrade(cfg, "head", sql=True)
    return buf.getvalue()


@pytest.fixture(scope="module")
def pg_upgrade_sql() -> str:
    return _offline_sql(PG_URL)


def test_repo_alembic_ini_resolves_package_script_location() -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    assert Path(script.dir).resolve() == MIGRATIONS_DIR
    assert script.get_heads() == ["0001"]
    assert script.get_revision("0001").down_revision is None


def test_sqlite_upgrade_matches_orm_then_downgrades_clean(tmp_path: Path) -> None:
    async_url, sync_url = _sqlite_urls(tmp_path)
    cfg = alembic_config(async_url)
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
            # PostgreSQL-only hardening must not leak into SQLite.
            assert conn.execute(text("SELECT count(*) FROM sqlite_master WHERE type='trigger'")).scalar_one() == 0
            # ORM ↔ migrated schema agree (guards against un-migrated model edits once 0001 is frozen).
            diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}),
                                     Base.metadata)
        assert diffs == [], diffs
        for table in Base.metadata.sorted_tables:
            got = {ix["name"] for ix in insp.get_indexes(table.name)}
            assert {ix.name for ix in table.indexes} <= got, table.name
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine(sync_url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")  # idempotent round trip
    engine = create_engine(sync_url)
    try:
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


async def test_upgrade_from_inside_running_event_loop(tmp_path: Path) -> None:
    """`acvm db upgrade` may be async; env.py must not call asyncio.run() on the caller's loop."""
    async_url, sync_url = _sqlite_urls(tmp_path)
    command.upgrade(alembic_config(async_url), "head")
    engine = create_engine(sync_url)
    try:
        assert "publication" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_offline_postgres_creates_extension_before_tables(pg_upgrade_sql: str) -> None:
    sql = pg_upgrade_sql
    ext = sql.index("CREATE EXTENSION IF NOT EXISTS vector")
    first_table = min(sql.index(f"CREATE TABLE {name} (") for name in Base.metadata.tables)
    assert ext < first_table
    for name in Base.metadata.tables:
        assert re.search(rf"CREATE TABLE {name} \(", sql), name
    assert "VECTOR(1024)" in sql.upper()
    assert "JSONB" in sql


def test_offline_postgres_affiliate_link_is_append_only(pg_upgrade_sql: str) -> None:
    sql = pg_upgrade_sql
    assert "CREATE OR REPLACE FUNCTION acvm_affiliate_link_guard()" in sql
    assert "BEFORE UPDATE OR DELETE ON affiliate_link" in sql
    assert "BEFORE TRUNCATE ON affiliate_link" in sql
    body = sql[sql.index("acvm_affiliate_link_guard()"):sql.index("trg_affiliate_link_append_only")]
    for col in ("product_id", "sub_id", "deeplink", "short_url", "hub_code", "signature", "publication_key"):
        assert f"NEW.{col} IS DISTINCT FROM OLD.{col}" in body, col
    for mutable in ("health", "checked_at"):
        assert f"NEW.{mutable} IS DISTINCT FROM" not in body
    assert "'DELETE', 'TRUNCATE'" in body


def test_offline_postgres_audit_log_guard_honours_retention_flag(pg_upgrade_sql: str) -> None:
    sql = pg_upgrade_sql
    assert "CREATE OR REPLACE FUNCTION acvm_audit_log_guard()" in sql
    assert "BEFORE UPDATE OR DELETE ON audit_log" in sql
    assert "BEFORE TRUNCATE ON audit_log" in sql
    assert "current_setting('acvm.retention', true)" in sql
    assert "USING brin (at)" in sql


def test_offline_postgres_hnsw_indexes(pg_upgrade_sql: str) -> None:
    for table in ("content_dna", "content", "playbook_rule"):
        assert re.search(
            rf"CREATE INDEX IF NOT EXISTS ix_{table}_embedding_hnsw ON {table} "
            r"USING hnsw \(embedding vector_cosine_ops\)", pg_upgrade_sql), table


def test_offline_sql_has_no_driver_placeholders(pg_upgrade_sql: str) -> None:
    # Trigger bodies are emitted verbatim; a stray '%' or ':name' would be mangled by the DBAPI paramstyle.
    for fn in ("acvm_affiliate_link_guard", "acvm_audit_log_guard"):
        start = pg_upgrade_sql.index(f"FUNCTION {fn}()")
        body = pg_upgrade_sql[start:pg_upgrade_sql.index("$$;", start)]
        assert "%" not in body
        assert not re.search(r"(?<![:\w]):[A-Za-z_]", body)


def test_offline_postgres_downgrade_drops_hardening_and_tables() -> None:
    sql = _offline_sql(PG_URL, downgrade=True)
    for trigger in ("trg_affiliate_link_append_only", "trg_audit_log_append_only"):
        assert f"DROP TRIGGER IF EXISTS {trigger}" in sql
    assert "DROP FUNCTION IF EXISTS acvm_audit_log_guard()" in sql
    for name in Base.metadata.tables:
        assert f"DROP TABLE IF EXISTS {name}" in sql, name
    assert sql.index("DROP TABLE IF EXISTS publication") < sql.index("DROP TABLE IF EXISTS affiliate_link")


def test_offline_sqlite_has_no_postgres_hardening() -> None:
    sql = _offline_sql("sqlite+aiosqlite:///./never-created.db")
    assert "CREATE TABLE affiliate_link" in sql
    assert "TRIGGER" not in sql
    assert "hnsw" not in sql
    assert "EXTENSION" not in sql

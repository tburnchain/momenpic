"""Alembic migrations for the ACVM transactional schema.

Programmatic entry points — `acvm db upgrade`, the compose `acvm-migrate` one-shot, the k8s migrate Job and
the tests all go through these so the script location never depends on the current working directory::

    from acvm.db.migrations import upgrade
    upgrade()                      # ACVM_DATABASE_URL → head
    upgrade(url, sql=True)         # offline: print the DDL instead of executing it
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent


def alembic_config(url: str | None = None, *, output_buffer: TextIO | None = None,
                   ini_path: str | Path | None = None) -> Config:
    """Alembic Config pointing at this package; ``url`` overrides ACVM_DATABASE_URL when given."""
    cfg = Config(str(ini_path)) if ini_path else Config(output_buffer=output_buffer)
    if ini_path and output_buffer is not None:
        cfg.output_buffer = output_buffer
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # ConfigParser interpolation
    cfg.attributes["configure_logger"] = ini_path is not None
    return cfg


def upgrade(url: str | None = None, revision: str = "head", *, sql: bool = False) -> None:
    command.upgrade(alembic_config(url), revision, sql=sql)


def downgrade(url: str | None = None, revision: str = "-1", *, sql: bool = False) -> None:
    command.downgrade(alembic_config(url), revision, sql=sql)


def current(url: str | None = None) -> None:
    command.current(alembic_config(url), verbose=True)

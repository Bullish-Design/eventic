"""Alembic migration environment for the Eventic `records` table.

The database URL is taken from ``DBOS_DATABASE_URL`` (falling back to the
``sqlalchemy.url`` placeholder in alembic.ini). The target metadata is
``eventic.persistence.models.Base.metadata`` so autogenerate reflects the
``records`` schema including the ``uq_records_id_version`` constraint (C6).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from eventic.store.schema import Base  # noqa: F401  (registers the triad)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get("DBOS_DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        url=get_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

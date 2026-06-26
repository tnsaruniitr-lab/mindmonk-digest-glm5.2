"""Alembic env — uses raw SQL migrations (no SQLAlchemy models).

Reads DATABASE_URL from the environment (same var the app uses). Falls back
to a local SQLite file for local dev when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the DB URL: prefer DATABASE_URL (prod/Railway), fall back to local SQLite.
db_url = os.getenv("DATABASE_URL", "").strip()
if not db_url:
    db_url = f"sqlite:///{os.path.abspath('podcast-digest.db')}"

# Normalize the scheme for SQLAlchemy's dialect resolution.
# psycopg v3 uses the postgresql+psycopg dialect (not the default psycopg2).
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    if "+" not in db_url.split("://")[0]:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

config.set_main_option("sqlalchemy.url", db_url)

# We use raw SQL migrations (no SQLAlchemy models / autogenerate).
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect and execute)."""
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
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

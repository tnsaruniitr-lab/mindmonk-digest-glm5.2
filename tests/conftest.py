"""Shared pytest fixtures.

The store tests need a database. We support two modes:
  1. TEST_DATABASE_URL env var → use that Postgres (e.g. a local docker PG)
  2. No TEST_DATABASE_URL → fall back to a temp SQLite file (default; no deps)

This means `pytest` works out-of-the-box with zero infra, but integration
tests against Postgres run when a real DB is available (CI / local docker).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Make the project root importable.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def tmp_sqlite_db():
    """A fresh SQLite DB file that's cleaned up after the test."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.db"


@pytest.fixture
def sqlite_store(tmp_sqlite_db):
    """A Store backed by SQLite (no Postgres needed)."""
    os.environ.pop("DATABASE_URL", None)  # force SQLite path
    from src.store import Store

    store = Store(tmp_sqlite_db)
    yield store
    store.close()


@pytest.fixture
def postgres_store():
    """A Store backed by Postgres (requires TEST_DATABASE_URL env var).

    Skipped if no test Postgres is available. Use this for true integration
    tests against the production DB engine.
    """
    db_url = os.environ.get("TEST_DATABASE_URL")
    if not db_url:
        pytest.skip("TEST_DATABASE_URL not set; skipping Postgres integration test")

    os.environ["DATABASE_URL"] = db_url
    from src.store import Store

    store = Store(None)
    yield store
    store.close()

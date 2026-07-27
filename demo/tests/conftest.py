"""Point the whole test suite at a throwaway database.

The ingest tests necessarily TRUNCATE `listings` — change detection is a
whole-table operation, so they cannot be scoped to a few rows. Run against the
working database and they destroy the loaded corpus, which is exactly what
happened once before this file existed.

The environment variable is set here, at collection time, because `mls_agent.db`
resolves its connection strings at import.
"""

from __future__ import annotations

import os

import psycopg

TEST_DB = "mls_test"
ADMIN = os.environ.get("DB_URL", "postgresql://mls:mls@localhost:5433/mls")
TEST_URL = ADMIN.rsplit("/", 1)[0] + f"/{TEST_DB}"


def _ensure_test_database() -> None:
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    # Settings live per-database, and the recall trap is a per-database setting.
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute(f"ALTER DATABASE \"{TEST_DB}\" SET hnsw.iterative_scan = 'relaxed_order'")


_ensure_test_database()
os.environ["DB_URL"] = TEST_URL
os.environ["APP_DB_URL"] = TEST_URL.replace("//mls:mls@", "//mls_app:mls_app@")

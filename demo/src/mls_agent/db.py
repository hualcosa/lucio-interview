"""Connections and the migration runner.

Two connection factories, deliberately distinct:

    admin_conn()  owner role. Migrations and ingest. Bypasses row-level security.
    app_conn()    application role. Every query path. Subject to row-level security.

Nothing in `domain/` may call `admin_conn`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).parent / "migrations"

ADMIN_URL = os.environ.get("DB_URL", "postgresql://mls:mls@localhost:5433/mls")
# Same database, lesser role. Password is demo-only; production reads from Secrets Manager.
APP_URL = os.environ.get("APP_DB_URL") or ADMIN_URL.replace("//mls:mls@", "//mls_app:mls_app@")


@contextmanager
def admin_conn() -> Iterator[psycopg.Connection]:
    """Owner connection. Migrations and ingest only."""
    with psycopg.connect(ADMIN_URL) as conn:
        yield conn


@contextmanager
def app_conn(brokerage_id: int) -> Iterator[psycopg.Connection]:
    """Application connection scoped to one principal.

    The brokerage id is established as a session variable before any query runs,
    and row-level security enforces it in the database. It is set here, from an
    already-verified identity — never accepted as a tool argument.
    """
    with psycopg.connect(APP_URL) as conn:
        # set_config parameterises properly; string interpolation into SET does not.
        conn.execute("SELECT set_config('app.brokerage_id', %s, false)", (str(brokerage_id),))
        yield conn


def migrate() -> list[str]:
    """Apply pending migrations in filename order. Returns what was applied."""
    applied: list[str] = []

    with admin_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}

        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)

        conn.commit()

    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"applied: {', '.join(done)}" if done else "already up to date")

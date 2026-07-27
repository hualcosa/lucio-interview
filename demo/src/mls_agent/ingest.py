"""Nightly ingest: land, diff, update only what changed.

This is the boundary the draft is missing (RESPONSE.md 3.2). The export stays a
full dump — we cannot change that — but only the delta ever reaches the database.

The whole thing is set operations in SQL rather than a row-by-row loop in Python.
That matters: streaming 2.2M rows through Python objects is precisely the mistake
being criticised. COPY into a staging table and let Postgres do the comparison.

One entry point serves two jobs:

    backfill   one-time, against an empty database. Not deployed infrastructure —
               a one-off migration does not deserve a production pipeline.
    nightly    recurring, 15-60k changed records, minutes on Lambda.

Same code, different starting state.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from mls_agent.db import admin_conn

# Business columns, in CSV order. Deliberately excludes updated_at: it changes on
# every dump by definition, so hashing it would make every row look modified and
# the diff would find 100% churn every night.
BUSINESS_COLUMNS = [
    "brokered_by",
    "status",
    "price",
    "bed",
    "bath",
    "acre_lot",
    "street",
    "city",
    "state",
    "zip_code",
    "house_size",
    "prev_sold_date",
    "list_date",
]
CSV_COLUMNS = ["listing_id", *BUSINESS_COLUMNS, "updated_at"]

# The export writes integers as floats ("103378.0"), which is exactly the sort of
# thing a decades-old system does and a schema-less format lets through silently.
# Staging is all text; casts are explicit and applied identically wherever a
# column is read, so the row digest stays stable across runs.
COLUMN_EXPR = {
    "brokered_by": "::numeric::bigint",
    "street": "::numeric::bigint",
    "house_size": "::numeric::integer",
    "bed": "::numeric::smallint",
    "bath": "::numeric::smallint",
    "price": "::numeric(12,2)",
    "acre_lot": "::numeric(12,3)",
    "prev_sold_date": "::date",
    "list_date": "::date",
}


def _staged(col: str, alias: str = "s") -> str:
    return f"NULLIF({alias}.{col}, ''){COLUMN_EXPR.get(col, '')}"


# Circuit breaker. A truncated or corrupt export from a decades-old system is not
# hypothetical, and without this one bad file silently overwrites the corpus.
# 15% is comfortably above real churn (~2%) and far below a truncation.
MAX_CHURN_PCT = 15.0
MIN_ROWS_FOR_BREAKER = 10_000  # below this, an empty table is the likelier explanation


class CircuitBreakerTripped(RuntimeError):
    """The export looks wrong. Halt and alert rather than process it."""


@dataclass
class IngestStats:
    dump_date: date
    records_seen: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    records_deleted: int = 0
    duration_ms: int = 0

    @property
    def records_changed(self) -> int:
        return self.records_inserted + self.records_updated + self.records_deleted

    @property
    def churn_pct(self) -> float:
        return 100.0 * self.records_changed / self.records_seen if self.records_seen else 0.0


def _row_hash_sql(alias: str) -> str:
    """Digest of the business columns only, over the cast values.

    Cast first, then stringify. Hashing the raw text would make "105000.0" and
    "105000" different rows, and the diff would report churn that did not happen.
    """
    cols = ", ".join(f"{_staged(c, alias)}::text" for c in BUSINESS_COLUMNS)
    return f"decode(md5(concat_ws('|', {cols})), 'hex')"


def ingest(csv_path: Path, dump_date: date, *, allow_high_churn: bool = False) -> IngestStats:
    """Diff one nightly export against current state and apply the delta."""
    started = time.perf_counter()
    stats = IngestStats(dump_date=dump_date)

    with admin_conn() as conn:
        run_id = conn.execute(
            "INSERT INTO ingest_runs (dump_date) VALUES (%s) RETURNING run_id",
            (dump_date,),
        ).fetchone()[0]
        # Committed before any work starts. A run that fails still has to leave a
        # record — otherwise the rollback erases the evidence along with the damage.
        conn.commit()

        try:
            _stage(conn, csv_path)

            stats.records_seen = conn.execute("SELECT count(*) FROM staging").fetchone()[0]
            existing = conn.execute("SELECT count(*) FROM listings").fetchone()[0]

            _preflight(conn, stats, existing, allow_high_churn)
            _apply(conn, stats, dump_date)

            stats.duration_ms = int((time.perf_counter() - started) * 1000)
            conn.execute(
                """
                UPDATE ingest_runs
                   SET finished_at = now(), status = 'completed',
                       records_seen = %s, records_inserted = %s,
                       records_updated = %s, records_deleted = %s
                 WHERE run_id = %s
                """,
                (
                    stats.records_seen,
                    stats.records_inserted,
                    stats.records_updated,
                    stats.records_deleted,
                    run_id,
                ),
            )
            conn.commit()

        except CircuitBreakerTripped as exc:
            conn.rollback()  # discards staging; the run record was already committed
            conn.execute(
                """UPDATE ingest_runs
                      SET finished_at = now(), status = 'halted', halt_reason = %s
                    WHERE run_id = %s""",
                (str(exc), run_id),
            )
            conn.commit()
            raise

    return stats


def _stage(conn, csv_path: Path) -> None:
    """COPY the export into a staging table. Postgres parses it, not Python."""
    conn.execute("DROP TABLE IF EXISTS staging")
    # Every column is text: the export is schema-less, so refusing a row at COPY
    # time would abort the whole load over one malformed field. Land it all, then
    # cast deliberately and drop what cannot be trusted.
    cols_ddl = ",\n            ".join(f"{c} text" for c in CSV_COLUMNS)
    conn.execute(f"CREATE UNLOGGED TABLE staging (\n            {cols_ddl}\n        )")

    cols = ", ".join(CSV_COLUMNS)
    with (
        csv_path.open("rb") as f,
        conn.cursor().copy(
            f"COPY staging ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)"
        ) as cp,
    ):
        while chunk := f.read(1 << 20):
            cp.write(chunk)

    # A dump missing brokered_by would silently orphan rows from every tenant,
    # and a row with no id cannot be diffed at all.
    conn.execute("""
        DELETE FROM staging
         WHERE NULLIF(listing_id, '') IS NULL
            OR NULLIF(brokered_by, '') IS NULL
            OR NULLIF(status, '') IS NULL
    """)
    # Indexed after loading, not before: building an index during COPY is slower.
    conn.execute("CREATE UNIQUE INDEX staging_pk ON staging ((listing_id::bigint))")
    conn.execute("ANALYZE staging")


def _preflight(conn, stats: IngestStats, existing: int, allow_high_churn: bool) -> None:
    """Estimate the blast radius before touching the serving table."""
    if existing < MIN_ROWS_FOR_BREAKER:
        return  # backfill: everything is an insert, and that is correct

    would_change = conn.execute(f"""
        SELECT (SELECT count(*)
                  FROM staging s
                  LEFT JOIN listings l ON l.listing_id = s.listing_id::bigint
                 WHERE l.listing_id IS NULL
                    OR l.row_hash IS DISTINCT FROM {_row_hash_sql("s")})
             + (SELECT count(*)
                  FROM listings l
                 WHERE NOT EXISTS (
                     SELECT 1 FROM staging s WHERE s.listing_id::bigint = l.listing_id))
    """).fetchone()[0]

    pct = 100.0 * would_change / existing
    if pct > MAX_CHURN_PCT and not allow_high_churn:
        raise CircuitBreakerTripped(
            f"export would change {pct:.1f}% of {existing:,} records "
            f"(ceiling {MAX_CHURN_PCT}%). Refusing to process what looks like a "
            f"truncated or corrupt file. Override with --allow-high-churn if intended."
        )


def _apply(conn, stats: IngestStats, dump_date: date) -> None:
    """Insert new, update changed, remove withdrawn. Untouched rows are never written."""
    business = ", ".join(BUSINESS_COLUMNS)
    staged = ", ".join(_staged(c) for c in BUSINESS_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in BUSINESS_COLUMNS)

    # xmax = 0 distinguishes a fresh insert from an update inside ON CONFLICT,
    # which is how insert and update counts come out of a single statement.
    result = conn.execute(
        f"""
        WITH upserted AS (
            INSERT INTO listings (listing_id, {business}, updated_at, row_hash)
            SELECT s.listing_id::bigint, {staged}, %s, {_row_hash_sql("s")}
              FROM staging s
              LEFT JOIN listings l ON l.listing_id = s.listing_id::bigint
             WHERE l.listing_id IS NULL
                OR l.row_hash IS DISTINCT FROM {_row_hash_sql("s")}
            ON CONFLICT (listing_id) DO UPDATE
               SET {updates}, updated_at = EXCLUDED.updated_at, row_hash = EXCLUDED.row_hash
         RETURNING xmax = 0 AS inserted
        )
        SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted)
          FROM upserted
    """,
        (dump_date,),
    ).fetchone()

    stats.records_inserted, stats.records_updated = result

    stats.records_deleted = conn.execute("""
        DELETE FROM listings l
         WHERE NOT EXISTS (
             SELECT 1 FROM staging s WHERE s.listing_id::bigint = l.listing_id)
    """).rowcount

    conn.execute("DROP TABLE IF EXISTS staging")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", type=Path)
    ap.add_argument("--dump-date", type=date.fromisoformat, required=True)
    ap.add_argument("--allow-high-churn", action="store_true", help="override the circuit breaker")
    args = ap.parse_args()

    stats = ingest(args.csv_path, args.dump_date, allow_high_churn=args.allow_high_churn)

    for k, v in asdict(stats).items():
        print(f"  {k:18} {v:,}" if isinstance(v, int) else f"  {k:18} {v}")
    print(f"  {'churn':18} {stats.churn_pct:.2f}%")

    if stats.records_updated or stats.records_deleted:
        print(
            f"\n  Only {stats.records_changed:,} of {stats.records_seen:,} records were "
            f"written ({stats.churn_pct:.2f}%). That ratio is the economic argument."
        )
    else:
        print("\n  Backfill: everything is new, so everything is written. Once.")


if __name__ == "__main__":
    main()

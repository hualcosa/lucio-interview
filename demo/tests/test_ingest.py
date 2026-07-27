"""Ingest behaviour: the delta is the delta, and a bad export gets refused.

The circuit breaker is the operational guardrail in RESPONSE.md 4.3. It earned
its place during development by catching a real bug — a date derived from the
dump date aged every listing by a day between runs, so every row hash changed
and the diff reported 100% churn. Without the breaker that would have silently
rewritten 2.2 million rows.
"""

from __future__ import annotations

import csv
from datetime import date

import pytest

from mls_agent import ingest as ing
from mls_agent.db import admin_conn, migrate

DAY1, DAY2 = date(2026, 1, 1), date(2026, 1, 2)


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ing.CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def listing(lid: int, *, price="100000.0", status="for_sale", broker="500.0"):
    return {
        "listing_id": str(lid),
        "brokered_by": broker,
        "status": status,
        "price": price,
        "bed": "3",
        "bath": "2",
        "acre_lot": "0.25",
        "street": "12345.0",
        "city": "Austin",
        "state": "Texas",
        "zip_code": "78701",
        "house_size": "1800.0",
        "prev_sold_date": "",
        "list_date": "2025-06-01",
        "updated_at": "",
    }


@pytest.fixture(autouse=True)
def clean():
    migrate()
    with admin_conn() as conn:
        conn.execute("TRUNCATE listings, ingest_runs RESTART IDENTITY")
        conn.commit()
    yield


def test_unchanged_records_are_not_rewritten(tmp_path):
    """The central claim: a full dump in, only the delta out."""
    rows = [listing(i) for i in range(1, 101)]
    ing.ingest(write_csv(tmp_path / "d1.csv", rows), DAY1)

    rows[0]["price"] = "125000.0"  # one price move
    rows.append(listing(101))  # one new listing
    rows.pop(1)  # one withdrawal

    stats = ing.ingest(write_csv(tmp_path / "d2.csv", rows), DAY2)

    assert (stats.records_updated, stats.records_inserted, stats.records_deleted) == (1, 1, 1)
    assert stats.records_seen == 100


def test_a_second_identical_dump_changes_nothing(tmp_path):
    """Re-running the same export must be a no-op, not a full rewrite.

    This is what fails when the row digest includes a field that moves on every
    dump — and it is exactly the bug the circuit breaker caught.
    """
    rows = [listing(i) for i in range(1, 101)]
    ing.ingest(write_csv(tmp_path / "d1.csv", rows), DAY1)
    stats = ing.ingest(write_csv(tmp_path / "d2.csv", rows), DAY2)

    assert stats.records_changed == 0


def test_float_formatted_integers_do_not_read_as_changes(tmp_path):
    """The export writes "103378.0" where the schema wants a bigint.

    Casting must happen before hashing, or "100000.0" and "100000" look like
    different rows and every night reports total churn.
    """
    rows = [listing(i) for i in range(1, 101)]
    ing.ingest(write_csv(tmp_path / "d1.csv", rows), DAY1)

    for r in rows:  # same values, written differently
        r["price"] = "100000.00"
        r["brokered_by"] = "500"

    assert ing.ingest(write_csv(tmp_path / "d2.csv", rows), DAY2).records_changed == 0


def test_rows_missing_a_tenant_are_dropped_not_orphaned(tmp_path):
    rows = [listing(i) for i in range(1, 11)]
    rows[0]["brokered_by"] = ""
    rows[1]["status"] = ""

    assert ing.ingest(write_csv(tmp_path / "d1.csv", rows), DAY1).records_seen == 8


class TestCircuitBreaker:
    @pytest.fixture(autouse=True)
    def lower_threshold(self, monkeypatch):
        # The real floor is 10k rows; testing at that size is pointless slowness.
        monkeypatch.setattr(ing, "MIN_ROWS_FOR_BREAKER", 10)

    def test_a_truncated_export_is_refused(self, tmp_path):
        ing.ingest(write_csv(tmp_path / "d1.csv", [listing(i) for i in range(1, 101)]), DAY1)

        truncated = [listing(i) for i in range(1, 21)]  # 80% of the corpus vanished
        with pytest.raises(ing.CircuitBreakerTripped, match="truncated or corrupt"):
            ing.ingest(write_csv(tmp_path / "d2.csv", truncated), DAY2)

    def test_a_refused_export_leaves_the_data_untouched(self, tmp_path):
        ing.ingest(write_csv(tmp_path / "d1.csv", [listing(i) for i in range(1, 101)]), DAY1)

        with pytest.raises(ing.CircuitBreakerTripped):
            ing.ingest(write_csv(tmp_path / "d2.csv", [listing(1)]), DAY2)

        with admin_conn() as conn:
            assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 100

    def test_the_halt_is_recorded_for_alerting(self, tmp_path):
        ing.ingest(write_csv(tmp_path / "d1.csv", [listing(i) for i in range(1, 101)]), DAY1)

        with pytest.raises(ing.CircuitBreakerTripped):
            ing.ingest(write_csv(tmp_path / "d2.csv", [listing(1)]), DAY2)

        with admin_conn() as conn:
            status, reason = conn.execute(
                "SELECT status, halt_reason FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()

        assert status == "halted"
        assert "%" in reason, "the alert must say how much would have changed"

    def test_an_override_exists_for_a_legitimate_bulk_change(self, tmp_path):
        """Brokerages do occasionally re-list everything. The breaker warns; it does not veto."""
        ing.ingest(write_csv(tmp_path / "d1.csv", [listing(i) for i in range(1, 101)]), DAY1)

        stats = ing.ingest(
            write_csv(tmp_path / "d2.csv", [listing(1)]), DAY2, allow_high_churn=True
        )
        assert stats.records_deleted == 99

    def test_a_backfill_is_not_mistaken_for_a_disaster(self, tmp_path):
        """100% churn against an empty table is correct, not alarming."""
        stats = ing.ingest(
            write_csv(tmp_path / "d1.csv", [listing(i) for i in range(1, 101)]), DAY1
        )
        assert stats.records_inserted == 100

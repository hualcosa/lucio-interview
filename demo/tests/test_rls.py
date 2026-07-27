"""Row-level security is the claim in RESPONSE.md 3.5. This is the proof.

If any of these fail, the central security argument of the review is false.
"""

from __future__ import annotations

import psycopg
import pytest

from mls_agent.db import admin_conn, app_conn, migrate

BROKER_A, BROKER_B = 900_001, 900_002


@pytest.fixture(scope="module", autouse=True)
def seed():
    migrate()
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER_A, BROKER_B))
        conn.execute(
            """
            INSERT INTO listings
                (listing_id, brokered_by, status, price, bed, city, state,
                 updated_at, row_hash)
            VALUES
                (990001, %s, 'for_sale', 400000, 3, 'Austin',  'Texas',   '2026-07-27', '\\x00'),
                (990002, %s, 'for_sale', 600000, 4, 'Dallas',  'Texas',   '2026-07-27', '\\x00'),
                (990003, %s, 'for_sale', 350000, 2, 'Houston', 'Texas',   '2026-07-27', '\\x00')
            """,
            (BROKER_A, BROKER_A, BROKER_B),
        )
        conn.commit()
    yield
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER_A, BROKER_B))
        conn.execute("DELETE FROM actions WHERE brokered_by IN (%s, %s)", (BROKER_A, BROKER_B))
        conn.commit()


def visible_ids(brokerage_id: int) -> set[int]:
    with app_conn(brokerage_id) as conn:
        rows = conn.execute("SELECT listing_id FROM listings WHERE listing_id >= 990000").fetchall()
    return {r[0] for r in rows}


def test_principal_sees_only_own_listings():
    assert visible_ids(BROKER_A) == {990001, 990002}
    assert visible_ids(BROKER_B) == {990003}


def test_query_that_forgets_to_filter_still_cannot_leak():
    """The whole point: safety sits below the application, not inside it.

    This query has no WHERE on brokered_by at all — exactly the mistake an
    application (or a model writing its own SQL) would make.
    """
    with app_conn(BROKER_B) as conn:
        rows = conn.execute(
            "SELECT brokered_by FROM listings WHERE listing_id >= 990000"
        ).fetchall()

    assert {r[0] for r in rows} == {BROKER_B}


def test_session_without_a_principal_sees_nothing():
    """Fail closed. An unset principal must not mean unrestricted."""
    from mls_agent.db import APP_URL

    with psycopg.connect(APP_URL) as conn:  # no set_config
        rows = conn.execute("SELECT listing_id FROM listings WHERE listing_id >= 990000").fetchall()

    assert rows == []


def test_cannot_write_an_action_for_another_brokerage():
    """WITH CHECK: a principal cannot insert rows attributed to someone else."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn(BROKER_A) as conn:
        conn.execute(
            """INSERT INTO actions (listing_id, brokered_by, action_type, requested_by)
                   VALUES (990003, %s, 'flag', 'test')""",
            (BROKER_B,),
        )


def test_writes_land_awaiting_approval():
    """Consequential actions are prepared, not performed (RESPONSE.md 3.7)."""
    with app_conn(BROKER_A) as conn:
        conn.execute(
            """INSERT INTO actions (listing_id, brokered_by, action_type, requested_by)
               VALUES (990001, %s, 'flag', 'test')""",
            (BROKER_A,),
        )
        status = conn.execute("SELECT status FROM actions WHERE listing_id = 990001").fetchone()[0]
        conn.commit()

    assert status == "pending_approval"

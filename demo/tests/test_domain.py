"""Domain service behaviour.

Note what this file does NOT import: anything from `mls_agent.mcp`. The domain
layer is testable without a protocol session, which is the whole argument in
Section 3.6 of the review.
"""

from __future__ import annotations

import pytest

from mls_agent import domain
from mls_agent.db import admin_conn, migrate
from mls_agent.domain import AuthContext

BROKER, OTHER = 910_001, 910_002
AUTH = AuthContext(brokerage_id=BROKER, subject="agent@example.com")


@pytest.fixture(autouse=True)
def seed():
    migrate()
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.execute(
            """
            INSERT INTO listings
                (listing_id, brokered_by, status, price, bed, bath, house_size,
                 city, state, zip_code, list_date, updated_at, row_hash)
            VALUES
                -- three-bed, under 500k, listed 120 days ago: the canonical hit
                (991001, %(b)s, 'for_sale', 450000, 3, 2, 1800, 'Austin', 'Texas', '78701',
                 current_date - 120, current_date, '\\x00'),
                -- three-bed but over budget: must NOT come back for max_price=500000
                (991002, %(b)s, 'for_sale', 530000, 3, 2, 1900, 'Austin', 'Texas', '78701',
                 current_date - 130, current_date, '\\x00'),
                -- under budget but listed yesterday: fails the days-on-market filter
                (991003, %(b)s, 'for_sale', 400000, 3, 1, 1500, 'Austin', 'Texas', '78702',
                 current_date - 1,   current_date, '\\x00'),
                -- already sold
                (991004, %(b)s, 'sold',     300000, 3, 1, 1400, 'Austin', 'Texas', '78702',
                 current_date - 200, current_date, '\\x00'),
                -- another brokerage's listing, otherwise a perfect match
                (991005, %(o)s, 'for_sale', 420000, 3, 2, 1700, 'Austin', 'Texas', '78701',
                 current_date - 150, current_date, '\\x00')
            """,
            {"b": BROKER, "o": OTHER},
        )
        conn.execute(
            "INSERT INTO ingest_runs (dump_date, status) VALUES (current_date, 'completed')"
        )
        conn.commit()
    yield
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.execute("DELETE FROM actions  WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.commit()


def ids(result) -> set[int]:
    return {r["listing_id"] for r in result.rows}


class TestSearchIsExact:
    """The argument against vector search, as executable assertions.

    Each of these is a predicate a similarity search would get approximately
    right — which is to say, wrong.
    """

    def test_price_ceiling_is_a_comparison_not_a_similarity(self):
        result = domain.search_listings(AUTH, city="Austin", max_price=500_000)
        assert 991002 not in ids(result), "a $530k listing came back for 'under $500k'"

    def test_days_on_market_filter_is_exact(self):
        result = domain.search_listings(AUTH, city="Austin", min_days_on_market=90)
        assert 991003 not in ids(result)

    def test_the_canonical_question(self):
        """Three-bed, under $500k, sitting more than ninety days."""
        result = domain.search_listings(
            AUTH, city="Austin", beds=3, max_price=500_000, min_days_on_market=90
        )
        assert ids(result) == {991001}

    def test_status_defaults_to_for_sale(self):
        assert 991004 not in ids(domain.search_listings(AUTH, city="Austin"))

    def test_rejects_unknown_status_rather_than_returning_nothing(self):
        with pytest.raises(ValueError, match="status must be one of"):
            domain.search_listings(AUTH, status="pending")


class TestAuthorisation:
    def test_other_brokerages_listings_are_invisible(self):
        result = domain.search_listings(AUTH, city="Austin", beds=3, min_days_on_market=90)
        assert 991005 not in ids(result), "leaked another brokerage's listing"

    def test_brokerage_is_not_a_caller_supplied_filter(self):
        """There is no parameter to widen scope with — by construction."""
        import inspect

        params = inspect.signature(domain.search_listings).parameters
        assert not any("broker" in p for p in params)


class TestGuardrails:
    def test_row_cap_is_enforced(self):
        result = domain.search_listings(AUTH, limit=10_000)
        assert len(result.rows) <= domain.MAX_LIMIT

    def test_truncation_is_reported_not_hidden(self):
        result = domain.search_listings(AUTH, city="Austin", limit=1)
        assert result.truncated is True


class TestFreshness:
    def test_every_result_carries_its_data_currency(self):
        assert domain.search_listings(AUTH, city="Austin").as_of is not None
        assert domain.market_stats(AUTH, city="Austin").as_of is not None

    def test_serialised_output_is_json_safe(self):
        import json

        payload = domain.serialise(domain.search_listings(AUTH, city="Austin"))
        json.dumps(payload)  # Decimals and dates would raise here
        assert payload["as_of"]


class TestWritesArePrepared:
    def test_flagging_requires_approval(self):
        result = domain.flag_listing(AUTH, 991001, reason="price looks stale")
        assert result.rows[0]["status"] == "pending_approval"
        assert result.meta["requires_approval"] is True

    def test_cannot_flag_another_brokerages_listing(self):
        with pytest.raises(PermissionError):
            domain.flag_listing(AUTH, 991005, reason="not mine to flag")

    def test_reason_is_mandatory(self):
        with pytest.raises(ValueError, match="reason is required"):
            domain.flag_listing(AUTH, 991001, reason="   ")


class TestAggregates:
    def test_median_is_a_question_similarity_cannot_answer(self):
        stats = domain.market_stats(AUTH, city="Austin").rows[0]
        assert stats["listings"] == 3  # the for_sale ones owned by BROKER
        assert stats["median_price"] == 450_000

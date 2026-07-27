"""The adapter must stay thin, and the boundary must stay real.

Two of these tests assert architectural properties rather than behaviour. That is
deliberate: RESPONSE.md 3.6 claims a boundary exists, and a claim nobody checks
is a claim that erodes.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mls_agent import domain, mcp_adapter
from mls_agent.db import admin_conn, migrate
from mls_agent.domain import AuthContext

BROKER, OTHER = 920_001, 920_002
AUTH = AuthContext(brokerage_id=BROKER, subject="agent@example.com")

SRC = Path(mcp_adapter.__file__).parent


@pytest.fixture(autouse=True)
def seed():
    migrate()
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.execute(
            """
            INSERT INTO listings
                (listing_id, brokered_by, status, price, bed, bath, city, state,
                 list_date, updated_at, row_hash)
            VALUES
                (992001, %(b)s, 'for_sale', 450000, 3, 2, 'Austin', 'Texas',
                 current_date - 120, current_date, '\\x00'),
                -- a second row for the same broker, so limit=1 actually truncates
                (992003, %(b)s, 'for_sale', 470000, 4, 3, 'Austin', 'Texas',
                 current_date - 110, current_date, '\\x00'),
                (992002, %(o)s, 'for_sale', 460000, 3, 2, 'Austin', 'Texas',
                 current_date - 120, current_date, '\\x00')
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


class TestTheBoundaryIsReal:
    def test_domain_does_not_import_the_protocol(self):
        """The dependency arrow points one way. Enforced, not documented.

        If this ever fails, the layering claim in the review has quietly become
        false and the domain layer can no longer be reused or tested alone.
        """
        tree = ast.parse((SRC / "domain.py").read_text())
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
        }

        assert not any("mcp" in m for m in imported), f"domain imports a protocol: {imported}"

    def test_the_adapter_holds_no_sql(self):
        source = (SRC / "mcp_adapter.py").read_text().upper()
        for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE FROM"):
            assert keyword not in source, f"business logic leaked into the adapter: {keyword}"


class TestToolContracts:
    def test_no_tool_lets_the_model_choose_a_tenant(self):
        """The authorisation predicate is not a parameter. By construction."""
        for tool in mcp_adapter.TOOLS:
            props = tool.inputSchema["properties"]
            assert not any(
                k in props for k in ("brokerage_id", "brokered_by", "broker", "tenant")
            ), f"{tool.name} exposes scope as an argument"

    def test_no_tool_accepts_raw_sql(self):
        for tool in mcp_adapter.TOOLS:
            props = tool.inputSchema["properties"]
            assert not any("sql" in k.lower() or "query" in k.lower() for k in props), tool.name

    def test_every_tool_rejects_unknown_arguments(self):
        for tool in mcp_adapter.TOOLS:
            assert tool.inputSchema.get("additionalProperties") is False, tool.name

    def test_result_size_is_bounded_in_the_schema_not_only_in_code(self):
        limit = mcp_adapter.TOOLS[0].inputSchema["properties"]["limit"]
        assert limit["maximum"] == domain.MAX_LIMIT

    def test_the_write_tool_announces_that_it_does_not_write(self):
        flag = next(t for t in mcp_adapter.TOOLS if t.name == "flag_listing")
        assert "approval" in flag.description.lower()
        assert "does not change" in flag.description.lower()

    def test_every_tool_is_dispatchable(self):
        assert {t.name for t in mcp_adapter.TOOLS} == set(mcp_adapter.HANDLERS)


class TestDispatch:
    def test_search_returns_only_the_callers_listings(self):
        payload = mcp_adapter.dispatch(AUTH, "search_listings", {"city": "Austin"})
        assert {r["listing_id"] for r in payload["rows"]} == {992001, 992003}

    def test_responses_carry_freshness_in_prose_not_only_in_a_field(self):
        payload = mcp_adapter.dispatch(AUTH, "search_listings", {"city": "Austin"})
        assert payload["as_of"]
        assert "24 hours" in payload["_note"], "a model will read the note, not the field"

    def test_truncation_is_announced_to_the_model(self):
        payload = mcp_adapter.dispatch(AUTH, "search_listings", {"limit": 1})
        assert "_note_truncated" in payload

    def test_output_is_json_serialisable(self):
        json.dumps(mcp_adapter.dispatch(AUTH, "market_stats", {"city": "Austin"}))

    def test_unknown_tool_is_refused(self):
        with pytest.raises(ValueError, match="unknown tool"):
            mcp_adapter.dispatch(AUTH, "drop_database", {})

    def test_flagging_reports_pending_rather_than_done(self):
        payload = mcp_adapter.dispatch(
            AUTH, "flag_listing", {"listing_id": 992001, "reason": "stale price"}
        )
        assert payload["rows"][0]["status"] == "pending_approval"

    def test_another_brokerages_listing_is_not_found_rather_than_forbidden(self):
        """Confirming a record exists but is not yours is itself a disclosure."""
        with pytest.raises(PermissionError):
            mcp_adapter.dispatch(AUTH, "flag_listing", {"listing_id": 992002, "reason": "not mine"})

"""Business logic. Knows nothing about MCP.

This module is the seam argued for in RESPONSE.md 3.6 — it is independently
callable and independently testable, and a web dashboard or a scheduled report
could use it tomorrow without speaking a protocol.

Two rules hold throughout:

1.  Every function takes an `AuthContext` as its first argument, and the
    authorisation predicate is derived from it. A caller cannot pass a
    brokerage id as a filter; the database enforces the scope regardless.

2.  Tools are coarse and parameterised. There is no raw SQL surface. The model
    chooses *which* vetted question to ask and fills typed arguments -- it never
    authors the query (RESPONSE.md 4.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

from mls_agent.db import app_conn

# Guardrails. A tool that can return a million rows is a denial-of-service
# primitive and a context-window bomb.
MAX_LIMIT = 200
DEFAULT_LIMIT = 25
STATEMENT_TIMEOUT_MS = 5_000

VALID_STATUS = frozenset({"for_sale", "sold", "ready_to_build"})


@dataclass(frozen=True)
class AuthContext:
    """A verified identity. Constructed only from a validated token."""

    brokerage_id: int
    subject: str


@dataclass
class Result:
    """Every response carries its data currency.

    The source system exports nightly, so answers are up to 24h stale. Saying so
    is cheap and is what lets the agent caveat honestly (RESPONSE.md 3.7).
    """

    rows: list[dict[str, Any]]
    as_of: date | None
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def _as_of(conn) -> date | None:
    row = conn.execute(
        "SELECT max(dump_date) FROM ingest_runs WHERE status = 'completed'"
    ).fetchone()
    return row[0] if row else None


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def search_listings(
    auth: AuthContext,
    *,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    status: str | None = "for_sale",
    min_price: float | None = None,
    max_price: float | None = None,
    beds: int | None = None,
    min_beds: int | None = None,
    baths: int | None = None,
    min_days_on_market: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Result:
    """Exact structured search. The canonical case the review keeps citing:

        three-bed condos under $500k that have sat more than ninety days

    Every predicate here is exact. This is why the design has no embeddings —
    "under $500,000" is a comparison, not a similarity (RESPONSE.md 3.3).
    """
    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"status must be one of {sorted(VALID_STATUS)}")

    limit = max(1, min(limit, MAX_LIMIT))

    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, value: Any) -> None:
        if value is not None:
            where.append(clause)
            params.append(value)

    add("status = %s", status)
    add("state = %s", state)
    add("zip_code = %s", zip_code)
    add("price >= %s", min_price)
    add("price <= %s", max_price)
    add("bed = %s", beds)
    add("bed >= %s", min_beds)
    add("bath = %s", baths)
    add("list_date <= current_date - %s", min_days_on_market)
    # Fuzzy on city only: users misremember place names, not prices.
    add("city %% %s", city)

    sql = f"""
        SELECT listing_id, status, price, bed, bath, house_size,
               city, state, zip_code, list_date,
               (current_date - list_date) AS days_on_market
        FROM listings
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY list_date ASC
        LIMIT %s
    """
    params.append(limit + 1)  # one extra: detects truncation without a second count

    with app_conn(auth.brokerage_id) as conn:
        conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        rows = _rows(conn.execute(sql, params))
        as_of = _as_of(conn)

    truncated = len(rows) > limit
    return Result(rows=rows[:limit], as_of=as_of, truncated=truncated)


def market_stats(
    auth: AuthContext,
    *,
    city: str | None = None,
    state: str | None = None,
    status: str = "for_sale",
) -> Result:
    """Aggregates. Note this is a question vector search cannot answer at all —
    similarity has no notion of a median."""
    if status not in VALID_STATUS:
        raise ValueError(f"status must be one of {sorted(VALID_STATUS)}")

    where = ["status = %s"]
    params: list[Any] = [status]
    if state:
        where.append("state = %s")
        params.append(state)
    if city:
        where.append("city %% %s")
        params.append(city)

    sql = f"""
        SELECT count(*)                                                    AS listings,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price)          AS median_price,
               min(price)                                                  AS min_price,
               max(price)                                                  AS max_price,
               avg(current_date - list_date)::numeric(10,1)                AS avg_days_on_market
        FROM listings
        WHERE {" AND ".join(where)}
    """

    with app_conn(auth.brokerage_id) as conn:
        conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        rows = _rows(conn.execute(sql, params))
        as_of = _as_of(conn)

    return Result(rows=rows, as_of=as_of, meta={"filters": {"city": city, "state": state}})


def flag_listing(auth: AuthContext, listing_id: int, reason: str) -> Result:
    """Prepare an action. Deliberately does not perform one.

    The legacy system is batch-only, so nothing can be written back synchronously
    anyway -- but the more important reason is that an agent which autonomously
    mutates the system of record is an audit finding, not a feature. This returns
    `pending_approval` (RESPONSE.md 3.7).
    """
    if not reason.strip():
        raise ValueError("reason is required: an unexplained flag is not actionable")

    with app_conn(auth.brokerage_id) as conn:
        conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")

        # RLS makes this return nothing if the listing belongs to someone else,
        # so the check and the authorisation are the same operation.
        owned = conn.execute(
            "SELECT 1 FROM listings WHERE listing_id = %s", (listing_id,)
        ).fetchone()
        if not owned:
            raise PermissionError(f"listing {listing_id} not found or not accessible")

        rows = _rows(
            conn.execute(
                """
                INSERT INTO actions
                    (listing_id, brokered_by, action_type, payload, requested_by)
                VALUES (%s, %s, 'flag', %s, %s)
                RETURNING action_id, listing_id, action_type, status, created_at
                """,
                (
                    listing_id,
                    auth.brokerage_id,  # from the verified identity, not from the caller
                    Json({"reason": reason}),
                    auth.subject,
                ),
            )
        )
        as_of = _as_of(conn)
        conn.commit()

    return Result(rows=rows, as_of=as_of, meta={"requires_approval": True})


def _jsonable(value: Any) -> Any:
    """Decimals and dates do not survive JSON. Tool responses must."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def serialise(result: Result) -> dict[str, Any]:
    """Shape a Result for transport. The MCP adapter uses this; so could a REST API."""
    return {
        "rows": [{k: _jsonable(v) for k, v in row.items()} for row in result.rows],
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "as_of": result.as_of.isoformat() if result.as_of else None,
        **({"meta": result.meta} if result.meta else {}),
    }

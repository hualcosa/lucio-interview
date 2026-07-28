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

from mls_agent.bedrock import Bedrock
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


def _executed_sql(cur) -> str:
    """The query as the database actually received it, parameters bound.

    Shown verbatim in the demo's trace panel. Making the generated SQL visible
    is the point: the model chose a tool, not a query, and anyone can check.
    """
    try:
        return " ".join((cur.query or b"").decode().split())
    except Exception:  # noqa: BLE001 - tracing must never break a request
        return ""


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
        cur = conn.execute(sql, params)
        rows, executed_sql = _rows(cur), _executed_sql(cur)
        as_of = _as_of(conn)

    truncated = len(rows) > limit
    return Result(
        rows=rows[:limit],
        as_of=as_of,
        truncated=truncated,
        meta={"retrieval": "structured", "sql": executed_sql},
    )


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
        cur = conn.execute(sql, params)
        rows, executed_sql = _rows(cur), _executed_sql(cur)
        as_of = _as_of(conn)

    return Result(
        rows=rows,
        as_of=as_of,
        meta={
            "retrieval": "aggregate",
            "sql": executed_sql,
            "filters": {"city": city, "state": state},
        },
    )


_bedrock: Bedrock | None = None


def _embed_question(text: str) -> str:
    """Embed the *question*. Exactly one embedding per request.

    This is the only place embedding happens at query time. Documents were
    embedded once when they arrived (RESPONSE.md 3.4).
    """
    global _bedrock
    if _bedrock is None:
        _bedrock = Bedrock()
    return str(_bedrock.embed(text))


def semantic_search(
    auth: AuthContext,
    question: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> Result:
    """Search document text by meaning. Returns the passage AND its citation.

    The citation is not a nicety. An agent advising a client has to be able to
    point at the clause; "the system said yes" is not a defence (RESPONSE.md 3.3).
    """
    if not question.strip():
        raise ValueError("question is required")

    limit = max(1, min(limit, MAX_LIMIT))
    vector = _embed_question(question)

    with app_conn(auth.brokerage_id) as conn:
        conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        cur = conn.execute(
            """
                SELECT c.listing_id,
                       d.title      AS document,
                       d.doc_type,
                       c.ordinal    AS passage_number,
                       c.passage,
                       round((1 - (c.embedding <=> %s::halfvec))::numeric, 4) AS similarity
                  FROM chunks c
                  JOIN documents d ON d.document_id = c.document_id
                 ORDER BY c.embedding <=> %s::halfvec
                 LIMIT %s
                """,
            (vector, vector, limit),
        )
        rows, executed_sql = _rows(cur), _executed_sql(cur)
        as_of = _as_of(conn)

    return Result(rows=rows, as_of=as_of, meta={"retrieval": "semantic", "sql": executed_sql})


def hybrid_search(
    auth: AuthContext,
    question: str,
    *,
    city: str | None = None,
    state: str | None = None,
    status: str | None = "for_sale",
    min_price: float | None = None,
    max_price: float | None = None,
    beds: int | None = None,
    min_beds: int | None = None,
    min_days_on_market: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Result:
    """Exact filters, then semantic ranking inside the filtered set.

    This is the whole argument in one function. The canonical question —

        "three-bed condos under $500k in Austin that allow short-term rentals"

    decomposes into exact comparisons (price, beds, city) and a clause buried in
    legal prose (short-term rentals). Neither retrieval strategy answers it alone.

    One statement, one query plan, one round trip — possible only because the
    columns and the vectors live in the same database (RESPONSE.md 3.3).
    """
    if not question.strip():
        raise ValueError("question is required")
    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"status must be one of {sorted(VALID_STATUS)}")

    limit = max(1, min(limit, MAX_LIMIT))
    vector = _embed_question(question)

    where: list[str] = []
    params: list[Any] = [vector, vector]  # distance appears twice: select and window

    def add(clause: str, value: Any) -> None:
        if value is not None:
            where.append(clause)
            params.append(value)

    add("l.status = %s", status)
    add("l.state = %s", state)
    add("l.price >= %s", min_price)
    add("l.price <= %s", max_price)
    add("l.bed = %s", beds)
    add("l.bed >= %s", min_beds)
    add("l.list_date <= current_date - %s", min_days_on_market)
    add("l.city %% %s", city)

    # One row per listing: its single best-matching passage, then ranked globally.
    # Without the window, a listing with many chunks would flood the results.
    sql = f"""
        WITH ranked AS (
            SELECT l.listing_id, l.price, l.bed, l.bath, l.city, l.state,
                   l.list_date, (current_date - l.list_date) AS days_on_market,
                   d.title AS document, c.passage,
                   round((1 - (c.embedding <=> %s::halfvec))::numeric, 4) AS similarity,
                   row_number() OVER (
                       PARTITION BY l.listing_id ORDER BY c.embedding <=> %s::halfvec
                   ) AS rn
              FROM listings l
              JOIN chunks c    ON c.listing_id  = l.listing_id
              JOIN documents d ON d.document_id = c.document_id
             {"WHERE " + " AND ".join(where) if where else ""}
        )
        SELECT listing_id, price, bed, bath, city, state, list_date, days_on_market,
               document, passage, similarity
          FROM ranked
         WHERE rn = 1
         ORDER BY similarity DESC
         LIMIT %s
    """
    params.append(limit)

    with app_conn(auth.brokerage_id) as conn:
        conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        cur = conn.execute(sql, params)
        rows, executed_sql = _rows(cur), _executed_sql(cur)
        as_of = _as_of(conn)

    return Result(
        rows=rows,
        as_of=as_of,
        meta={"retrieval": "hybrid", "sql": executed_sql, "filters_applied": len(where)},
    )


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

        cur = conn.execute(
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
        rows = _rows(cur)
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

"""Document and chunk storage: tenancy holds, and the recall trap stays disarmed.

The `iterative_scan` assertion is the important one here. Without that setting a
filtered vector search returns partial results *silently* — no error, no warning,
just fewer rows than exist. It is a one-line database setting and it is the kind
of thing that gets dropped in a migration and noticed six months later.
"""

from __future__ import annotations

import psycopg
import pytest

from mls_agent.db import admin_conn, app_conn, migrate

BROKER, OTHER = 930_001, 930_002
DIMS = 256


def vec(seed: float) -> str:
    """A deterministic unit-ish vector. Bedrock is not needed to test storage."""
    return "[" + ",".join(f"{(seed + i * 0.001) % 1:.6f}" for i in range(DIMS)) + "]"


@pytest.fixture(autouse=True)
def seed():
    migrate()
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.execute(
            """
            INSERT INTO listings (listing_id, brokered_by, status, price, bed, city, state,
                                  updated_at, row_hash)
            VALUES (993001, %(b)s, 'for_sale', 450000, 3, 'Austin', 'Texas', current_date, '\\x00'),
                   (993002, %(o)s, 'for_sale', 460000, 3, 'Austin', 'Texas', current_date, '\\x00')
            """,
            {"b": BROKER, "o": OTHER},
        )
        for lid, broker, body, v in (
            (993001, BROKER, "Leases under 30 days are prohibited.", vec(0.10)),
            (993002, OTHER, "Short-term rentals are permitted.", vec(0.90)),
        ):
            doc_id = conn.execute(
                """INSERT INTO documents
                       (listing_id, brokered_by, doc_type, title, body, content_hash, updated_at)
                   VALUES (%s, %s, 'hoa_rules', 'Rules', %s, %s, current_date)
                   RETURNING document_id""",
                (lid, broker, body, b"\x00"),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO chunks
                       (document_id, listing_id, brokered_by, ordinal, passage, embedding)
                   VALUES (%s, %s, %s, 0, %s, %s)""",
                (doc_id, lid, broker, body, v),
            )
        conn.commit()
    yield
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by IN (%s, %s)", (BROKER, OTHER))
        conn.commit()


class TestTheRecallTrapStaysDisarmed:
    def test_iterative_scan_is_enabled(self):
        """If this fails, filtered vector search silently loses results."""
        with app_conn(BROKER) as conn:
            setting = conn.execute("SHOW hnsw.iterative_scan").fetchone()[0]
        assert setting in ("relaxed_order", "strict_order"), (
            f"hnsw.iterative_scan is {setting!r}; filtered searches will return "
            "partial results with no error"
        )

    def test_the_index_is_hnsw_over_halfvec(self):
        with admin_conn() as conn:
            defn = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
            ).fetchone()[0]
        assert "hnsw" in defn and "halfvec_cosine_ops" in defn, defn


class TestTenancy:
    def test_a_principal_sees_only_its_own_documents(self):
        with app_conn(BROKER) as conn:
            rows = conn.execute("SELECT listing_id FROM documents").fetchall()
        assert {r[0] for r in rows} == {993001}

    def test_chunks_are_scoped_too(self):
        """Chunks carry their own tenant column rather than joining to find it."""
        with app_conn(OTHER) as conn:
            rows = conn.execute("SELECT listing_id FROM chunks").fetchall()
        assert {r[0] for r in rows} == {993002}

    def test_similarity_search_cannot_reach_another_tenant(self):
        """A vector query aimed squarely at someone else's passage returns nothing.

        Similarity does not bypass row-level security — the filter is applied by
        the database, not by the query author.
        """
        with app_conn(BROKER) as conn:
            rows = conn.execute(
                "SELECT listing_id FROM chunks ORDER BY embedding <=> %s::halfvec LIMIT 5",
                (vec(0.90),),  # this is OTHER's vector
            ).fetchall()
        assert {r[0] for r in rows} == {993001}

    def test_a_session_without_a_principal_sees_no_documents(self):
        from mls_agent.db import APP_URL

        with psycopg.connect(APP_URL) as conn:
            assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


class TestHybridShape:
    def test_columns_and_vectors_join_in_one_query(self):
        """The claim in RESPONSE.md 3.3, as a single statement.

        Exact predicates on the listing, semantic ordering on the passage, one
        round trip — which is only possible because both live in one database.
        """
        with app_conn(BROKER) as conn:
            rows = conn.execute(
                """
                SELECT l.listing_id, l.price, c.passage
                  FROM listings l
                  JOIN chunks c ON c.listing_id = l.listing_id
                 WHERE l.price < 500000 AND l.bed = 3 AND l.city = 'Austin'
                 ORDER BY c.embedding <=> %s::halfvec
                 LIMIT 5
                """,
                (vec(0.10),),
            ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == 993001

    def test_deleting_a_document_removes_its_chunks(self):
        """Orphaned vectors are answers to questions about documents that no
        longer exist. The cascade is not a convenience."""
        with admin_conn() as conn:
            conn.execute("DELETE FROM documents WHERE listing_id = 993001")
            remaining = conn.execute(
                "SELECT count(*) FROM chunks WHERE listing_id = 993001"
            ).fetchone()[0]
            conn.commit()
        assert remaining == 0

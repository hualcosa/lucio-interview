"""Embed once, on change only.

The headline assertion is `test_a_second_identical_run_embeds_nothing`. That is
the fix to the flaw the brief listed, stated as a test rather than a claim.

Bedrock is stubbed here — the behaviour under test is *when* embedding happens,
not what the vectors contain. The real model is exercised by the pipeline run,
not by the unit tests, because paying Amazon to assert a control flow would be
silly.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from mls_agent import documents
from mls_agent.bedrock import EMBED_DIMS
from mls_agent.db import admin_conn, migrate

BROKER = 840_001


class StubBedrock:
    """Counts calls. That count is the entire point of this module."""

    def __init__(self):
        self.embed_calls = 0

        class _Usage:
            embed_tokens = 0
            usd = 0.0

        self.usage = _Usage()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += len(texts)
        self.usage.embed_tokens += sum(len(t) // 4 for t in texts)
        return [[0.01] * EMBED_DIMS for _ in texts]


@pytest.fixture(autouse=True)
def seed():
    migrate()
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by = %s", (BROKER,))
        conn.execute(
            """INSERT INTO listings (listing_id, brokered_by, status, price, bed, city,
                                     state, updated_at, row_hash)
               VALUES (9994001, %s, 'for_sale', 400000, 2, 'Austin', 'Texas',
                       current_date, '\\x00')""",
            (BROKER,),
        )
        conn.commit()
    yield
    with admin_conn() as conn:
        conn.execute("DELETE FROM listings WHERE brokered_by = %s", (BROKER,))
        conn.commit()


LONG_BODY = "\n\n".join(
    f"{i}.1 Clause {i}. " + ("Lorem ipsum dolor sit amet consectetur. " * 12) for i in range(1, 5)
)


def write_docs(path: Path, body: str, listing_id: int = 9994001) -> Path:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["listing_id", "brokered_by", "doc_type", "title", "body"])
        w.writeheader()
        w.writerow(
            {
                "listing_id": str(listing_id),
                "brokered_by": str(float(BROKER)),
                "doc_type": "hoa_rules",
                "title": "Rules",
                "body": body,
            }
        )
    return path


class TestEmbedOnce:
    def test_a_first_run_embeds_everything(self, tmp_path):
        stub = StubBedrock()
        stats = documents.ingest_documents(write_docs(tmp_path / "d.csv", LONG_BODY), bedrock=stub)

        assert stats.documents_new == 1
        assert stats.chunks_written > 1
        assert stub.embed_calls == stats.chunks_written

    def test_a_second_identical_run_embeds_nothing(self, tmp_path):
        """The fix to the listed flaw, as an assertion.

        The draft re-embedded the corpus on every *query*. Here an unchanged
        document does no work even on a full nightly re-run.
        """
        path = write_docs(tmp_path / "d.csv", LONG_BODY)
        documents.ingest_documents(path, bedrock=StubBedrock())

        second = StubBedrock()
        stats = documents.ingest_documents(path, bedrock=second)

        assert second.embed_calls == 0
        assert stats.documents_unchanged == 1
        assert stats.chunks_written == 0
        assert stats.embed_usd == 0.0

    def test_a_changed_document_is_re_embedded(self, tmp_path):
        documents.ingest_documents(write_docs(tmp_path / "d.csv", LONG_BODY), bedrock=StubBedrock())

        third = StubBedrock()
        stats = documents.ingest_documents(
            write_docs(tmp_path / "d.csv", LONG_BODY + "\n\n5.1 A new clause."), bedrock=third
        )

        assert stats.documents_changed == 1
        assert third.embed_calls > 0

    def test_replacing_a_document_leaves_no_stale_chunks(self, tmp_path):
        """Old vectors answer questions about text that no longer exists."""
        documents.ingest_documents(write_docs(tmp_path / "d.csv", LONG_BODY), bedrock=StubBedrock())
        documents.ingest_documents(
            write_docs(tmp_path / "d.csv", "1.1 Short replacement."), bedrock=StubBedrock()
        )

        with admin_conn() as conn:
            passages = conn.execute(
                "SELECT passage FROM chunks WHERE listing_id = 9994001"
            ).fetchall()

        assert len(passages) == 1
        assert "Lorem" not in passages[0][0]

    def test_documents_for_unknown_listings_are_skipped_not_fatal(self, tmp_path):
        stats = documents.ingest_documents(
            write_docs(tmp_path / "d.csv", LONG_BODY, listing_id=999_999_999),
            bedrock=StubBedrock(),
        )
        assert stats.documents_new == 0


class TestChunking:
    def test_clauses_stay_with_their_exceptions(self):
        """Splitting "prohibited" from "except for units acquired before 2015"
        across two chunks would retrieve a passage that means the opposite."""
        text = (
            "1.1 Short-term rentals are PROHIBITED, except as follows:\n\n"
            "a) Units acquired before 2015 may be leased up to four times a year."
        )
        assert len(documents.chunk_text(text)) == 1

    def test_long_documents_are_split(self):
        chunks = documents.chunk_text(LONG_BODY)
        assert len(chunks) > 1
        assert all(len(c) >= documents.MIN_CHARS for c in chunks)

    def test_no_runt_final_chunk(self):
        chunks = documents.chunk_text(LONG_BODY + "\n\nx.")
        assert chunks[-1].endswith("x.")
        assert len(chunks[-1]) >= documents.MIN_CHARS

    def test_empty_input_yields_nothing(self):
        assert documents.chunk_text("   \n\n  ") == []

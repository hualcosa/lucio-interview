"""Document ingest: chunk and embed, once, on change only.

This is the fix to the flaw the brief actually listed. The draft regenerated
every embedding on every query; here an embedding is a property of the text, so
it is computed when the text arrives and never again until the text changes.

The mechanism is a content digest per document. A document whose body hashes the
same is not re-read, not re-chunked and not re-embedded — the run does no work
at all for it. Running this twice in a row costs nothing the second time, and
there is a test that asserts exactly that.

Only the *question* is embedded at query time. See RESPONSE.md 3.4.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from mls_agent.bedrock import Bedrock
from mls_agent.db import admin_conn

# HOA clauses are short. Chunks big enough to keep a clause and its exception
# together — splitting "prohibited" from "except for units acquired before 2015"
# across two chunks would be actively misleading.
TARGET_CHARS = 900
MIN_CHARS = 200


@dataclass
class DocStats:
    documents_seen: int = 0
    documents_new: int = 0
    documents_changed: int = 0
    documents_unchanged: int = 0
    chunks_written: int = 0
    tokens_embedded: int = 0
    embed_usd: float = 0.0
    duration_ms: int = 0

    @property
    def documents_embedded(self) -> int:
        return self.documents_new + self.documents_changed


def chunk_text(text: str) -> list[str]:
    """Split on blank lines, then greedily pack paragraphs up to TARGET_CHARS.

    Paragraph boundaries rather than a fixed window, because these documents are
    clause-structured and the clause is the unit of meaning.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > TARGET_CHARS and len(current) >= MIN_CHARS:
            chunks.append(current)
            current = para
        else:
            current = candidate

    if current:
        # Avoid a runt final chunk: fold it back into the previous one.
        if chunks and len(current) < MIN_CHARS:
            chunks[-1] = f"{chunks[-1]}\n\n{current}"
        else:
            chunks.append(current)

    return chunks


def _digest(body: str) -> bytes:
    return hashlib.blake2b(body.encode(), digest_size=16).digest()


def ingest_documents(
    csv_path: Path, *, bedrock: Bedrock | None = None, embed: bool = True
) -> DocStats:
    """Load documents and embed only what changed."""
    started = time.perf_counter()
    stats = DocStats()
    bedrock = bedrock or Bedrock()

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    stats.documents_seen = len(rows)

    with admin_conn() as conn:
        # Documents reference listings, so anything whose listing never loaded is
        # skipped rather than failing the run.
        known = {
            r[0]
            for r in conn.execute(
                "SELECT listing_id FROM listings WHERE listing_id = ANY(%s)",
                ([int(r["listing_id"]) for r in rows],),
            ).fetchall()
        }

        for row in rows:
            listing_id = int(row["listing_id"])
            if listing_id not in known:
                continue

            body = row["body"]
            content_hash = _digest(body)

            existing = conn.execute(
                "SELECT document_id, content_hash FROM documents "
                "WHERE listing_id = %s AND doc_type = %s",
                (listing_id, row["doc_type"]),
            ).fetchone()

            if existing and existing[1] == content_hash:
                stats.documents_unchanged += 1
                continue  # the whole point: no read, no chunk, no embed

            doc_id = conn.execute(
                """
                INSERT INTO documents
                    (listing_id, brokered_by, doc_type, title, body, content_hash, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, current_date)
                ON CONFLICT (listing_id, doc_type) DO UPDATE
                   SET title = EXCLUDED.title, body = EXCLUDED.body,
                       content_hash = EXCLUDED.content_hash, updated_at = EXCLUDED.updated_at
                RETURNING document_id
                """,
                (
                    listing_id,
                    int(float(row["brokered_by"])),
                    row["doc_type"],
                    row["title"],
                    body,
                    content_hash,
                ),
            ).fetchone()[0]

            if existing:
                stats.documents_changed += 1
                # Stale chunks describe text that no longer exists.
                conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            else:
                stats.documents_new += 1

            passages = chunk_text(body)
            vectors = bedrock.embed_many(passages) if embed else [None] * len(passages)

            for ordinal, (passage, vector) in enumerate(zip(passages, vectors, strict=True)):
                conn.execute(
                    """INSERT INTO chunks
                           (document_id, listing_id, brokered_by, ordinal, passage, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        doc_id,
                        listing_id,
                        int(float(row["brokered_by"])),
                        ordinal,
                        passage,
                        str(vector) if vector else None,
                    ),
                )
                stats.chunks_written += 1

        conn.commit()

    stats.tokens_embedded = bedrock.usage.embed_tokens
    stats.embed_usd = bedrock.usage.usd
    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", type=Path, nargs="?", default=Path("data/documents/documents.csv"))
    ap.add_argument("--no-embed", action="store_true", help="load text without calling Bedrock")
    args = ap.parse_args()

    if not args.csv_path.exists():
        sys.exit(f"{args.csv_path} not found. Run scripts/make_documents.py first.")

    stats = ingest_documents(args.csv_path, embed=not args.no_embed)

    print(f"  documents seen      {stats.documents_seen:,}")
    print(f"  new                 {stats.documents_new:,}")
    print(f"  changed             {stats.documents_changed:,}")
    print(f"  unchanged (skipped) {stats.documents_unchanged:,}")
    print(f"  chunks written      {stats.chunks_written:,}")
    print(f"  tokens embedded     {stats.tokens_embedded:,}")
    print(f"  embedding cost      ${stats.embed_usd:.4f}")
    print(f"  duration            {stats.duration_ms:,} ms")

    if stats.documents_unchanged and not stats.documents_embedded:
        print("\n  Nothing changed, so nothing was embedded. That is the whole point.")


if __name__ == "__main__":
    main()

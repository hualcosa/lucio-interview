"""The draft design, reimplemented faithfully, measured against the revised one.

`naive_query` is not a straw man. It is what the brief describes: read the whole
nightly export from S3 on every user question, hold it in memory, scan it, and
regenerate embeddings. The only liberty taken is reading from local disk instead
of S3, which makes the naive path look *better* than it is — no network transfer.

Embedding cost is calculated from token counts rather than incurred. Spending
$9 to prove it costs $9 would be a poor use of the client's money, and the
arithmetic is not in dispute.

    uv run python -m mls_agent.benchmark
"""

from __future__ import annotations

import argparse
import csv
import json
import resource
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mls_agent.bedrock import EMBED_USD_PER_1M_TOKENS
from mls_agent.db import admin_conn
from mls_agent.domain import AuthContext, search_listings

DUMP = Path("data/dumps/2026-07-26/listings.csv")
RESULTS = Path("results/benchmark.json")

LAMBDA_MEMORY_CEILING_MB = 10_240  # AWS hard limit

# Lambda at 10GB, us-east-1.
LAMBDA_GB_SECOND_USD = 0.0000166667

# Roughly what a listing's text amounts to once the draft has concatenated its
# fields for embedding.
TOKENS_PER_RECORD = 150


@dataclass
class Measurement:
    approach: str
    scale: int
    latency_ms: float
    peak_memory_mb: float
    rows_returned: int
    lambda_cost_usd: float
    embedding_cost_usd: float
    exceeds_lambda_ceiling: bool

    @property
    def total_cost_usd(self) -> float:
        return self.lambda_cost_usd + self.embedding_cost_usd


def _peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def naive_query(limit: int, *, max_price: float, beds: int) -> Measurement:
    """The draft: load the entire export, scan it in memory, re-embed everything.

    Memory is what kills this, and it is not the file size. A CSV parsed into
    Python objects expands several times over — every field becomes a `str`
    object with its own header, and every row a `dict`.
    """
    started = time.perf_counter()
    before_mb = _peak_mb()

    records: list[dict[str, str]] = []
    with DUMP.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i >= limit:
                break
            records.append(row)  # the whole point: it all stays resident

    matches = [
        r
        for r in records
        if r.get("price") and float(r["price"]) < max_price and r.get("bed") == str(beds)
    ]

    elapsed = time.perf_counter() - started
    # ru_maxrss is a cumulative high-water mark for the process, so subtract the
    # mark taken before this run to attribute memory to this scale alone.
    peak_mb = max(_peak_mb() - before_mb, 0.0)

    # The draft regenerates every embedding on every query. Calculated, not spent.
    embed_tokens = len(records) * TOKENS_PER_RECORD
    embed_usd = embed_tokens * EMBED_USD_PER_1M_TOKENS / 1_000_000

    gb_seconds = (LAMBDA_MEMORY_CEILING_MB / 1024) * elapsed

    del records
    return Measurement(
        approach="naive",
        scale=limit,
        latency_ms=round(elapsed * 1000, 1),
        peak_memory_mb=round(peak_mb, 1),
        rows_returned=len(matches),
        lambda_cost_usd=round(gb_seconds * LAMBDA_GB_SECOND_USD, 6),
        embedding_cost_usd=round(embed_usd, 4),
        exceeds_lambda_ceiling=peak_mb > LAMBDA_MEMORY_CEILING_MB,
    )


def indexed_query(scale: int, broker: int, *, max_price: float, beds: int) -> Measurement:
    """The revised design: one indexed query against the serving database."""
    auth = AuthContext(brokerage_id=broker, subject="benchmark")

    started = time.perf_counter()
    result = search_listings(auth, max_price=max_price, beds=beds, limit=25)
    elapsed = time.perf_counter() - started

    # Small function, short duration; 512MB is a realistic allocation.
    gb_seconds = 0.5 * elapsed

    return Measurement(
        approach="indexed",
        scale=scale,
        latency_ms=round(elapsed * 1000, 1),
        peak_memory_mb=round(_peak_mb(), 1),
        rows_returned=len(result.rows),
        lambda_cost_usd=round(gb_seconds * LAMBDA_GB_SECOND_USD, 8),
        embedding_cost_usd=0.0,  # documents were embedded once, at ingest
        exceeds_lambda_ceiling=False,
    )


def project_memory(measurements: list[Measurement]) -> dict[str, float | int | None]:
    """Where does the naive path cross Lambda's ceiling?

    The brief says 3M records growing 5% a month. Extrapolating from measured
    bytes-per-record answers the question that actually matters: not "does it
    work today" but "how long until it does not".
    """
    naive = [m for m in measurements if m.approach == "naive" and m.scale > 0]
    if len(naive) < 2:
        return {}

    biggest = max(naive, key=lambda m: m.scale)
    mb_per_record = biggest.peak_memory_mb / biggest.scale

    ceiling_records = int(LAMBDA_MEMORY_CEILING_MB / mb_per_record)

    months = None
    if ceiling_records > 3_000_000:
        # 3M growing 5%/month: 3M * 1.05^n = ceiling
        from math import log

        months = round(log(ceiling_records / 3_000_000) / log(1.05), 1)

    return {
        "mb_per_1k_records": round(mb_per_record * 1000, 2),
        "records_at_lambda_ceiling": ceiling_records,
        "months_from_3M_until_ceiling": months,
        "memory_at_3M_records_mb": round(mb_per_record * 3_000_000),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scales",
        type=int,
        nargs="+",
        default=[10_000, 100_000, 500_000, 1_000_000, 2_221_849],
    )
    args = ap.parse_args()

    if not DUMP.exists():
        raise SystemExit(f"{DUMP} not found — run `make dumps` first")

    with admin_conn() as conn:
        broker = conn.execute(
            "SELECT brokered_by FROM listings GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()[0]
        loaded = conn.execute("SELECT count(*) FROM listings").fetchone()[0]

    print(f"database holds {loaded:,} listings; benchmarking brokerage {broker}\n")
    print(f"{'scale':>10}  {'approach':9}  {'latency':>11}  {'peak RSS':>10}  {'$/query':>10}")
    print("-" * 60)

    measurements: list[Measurement] = []
    for scale in args.scales:
        for m in (
            naive_query(scale, max_price=500_000, beds=3),
            indexed_query(scale, broker, max_price=500_000, beds=3),
        ):
            measurements.append(m)
            flag = "  ⚠ OVER LAMBDA CEILING" if m.exceeds_lambda_ceiling else ""
            print(
                f"{m.scale:>10,}  {m.approach:9}  {m.latency_ms:>9,.0f}ms  "
                f"{m.peak_memory_mb:>8,.0f}MB  ${m.total_cost_usd:>9.4f}{flag}"
            )

    projection = project_memory(measurements)

    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "measurements": [asdict(m) for m in measurements],
                "projection": projection,
                "notes": {
                    "naive_reads_local_disk": "flatters the draft: no S3 transfer included",
                    "embedding_cost": "calculated from token counts, not incurred",
                    "lambda_ceiling_mb": LAMBDA_MEMORY_CEILING_MB,
                },
            },
            indent=2,
        )
    )

    print(f"\nwrote {RESULTS}")
    if projection:
        print("\nExtrapolating from measured bytes per record:")
        print(f"  memory at 3M records          {projection['memory_at_3M_records_mb']:,} MB")
        print(f"  records at Lambda's 10GB cap  {projection['records_at_lambda_ceiling']:,}")
        if projection.get("months_from_3M_until_ceiling") is not None:
            print(f"  months from 3M at +5%/month   {projection['months_from_3M_until_ceiling']}")


if __name__ == "__main__":
    main()

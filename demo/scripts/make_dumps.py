#!/usr/bin/env python3
"""Turn the raw dataset into two consecutive 'nightly exports' so CDC has real work.

The public dataset is a snapshot: one file, no listing id, no list date. A real MLS export
has both. We add them deterministically rather than inventing content:

  listing_id   row position in the canonical file, held stable across dumps
  list_date    derived from a hash of the row, spread over the last ~2 years
  updated_at   the dump date

Everything else is untouched, real data. No text is synthesized because the design carries
no embeddings — see RESPONSE.md 3.3.

Day 2 applies realistic churn (~2% total) so the nightly diff has something to find:
price moves, listings going under contract, new listings, and withdrawals.

    uv run python scripts/make_dumps.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import sys
from datetime import date, timedelta
from pathlib import Path

RAW = Path("data/realtor-raw.csv")
OUT = Path("data/dumps")

# Churn rates for day 2. Roughly what a brokerage actually sees overnight.
PRICE_CHANGE_RATE = 0.010
STATUS_CHANGE_RATE = 0.003
NEW_LISTING_RATE = 0.005
WITHDRAWN_RATE = 0.002

SEED = 20260727  # reproducible: same input, same dumps, every run
LIST_DATE_EPOCH = date(2026, 7, 26)  # fixed: a listing's list date does not move

OUT_FIELDS = [
    "listing_id",
    "brokered_by",
    "status",
    "price",
    "bed",
    "bath",
    "acre_lot",
    "street",
    "city",
    "state",
    "zip_code",
    "house_size",
    "prev_sold_date",
    "list_date",
    "updated_at",
]


def derive_list_date(listing_id: int) -> date:
    """Stable pseudo-random list date within ~2 years of a FIXED anchor.

    Anchored to LIST_DATE_EPOCH rather than to the dump date. Deriving it from the
    dump date would age every listing by a day between dumps, changing every row
    hash and making the diff report 100% churn — which is exactly what the circuit
    breaker caught the first time this ran.
    """
    h = hashlib.blake2b(str(listing_id).encode(), digest_size=4).digest()
    return LIST_DATE_EPOCH - timedelta(days=int.from_bytes(h, "big") % 730)


def read_raw(limit: int | None) -> list[dict[str, str]]:
    if not RAW.exists():
        sys.exit(f"{RAW} not found. Run `make data` first.")

    rows = []
    with RAW.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if limit and i >= limit:
                break
            row["listing_id"] = str(i + 1)
            rows.append(row)
    return rows


def write_dump(rows: list[dict[str, str]], dump_date: date) -> Path:
    out_dir = OUT / dump_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "listings.csv"

    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            lid = int(row["listing_id"])
            w.writerow(
                row
                | {
                    "list_date": derive_list_date(lid).isoformat(),
                    "updated_at": dump_date.isoformat(),
                }
            )
    return path


def apply_churn(rows: list[dict[str, str]], rng: random.Random) -> tuple[list[dict], dict]:
    """Return day-2 rows plus a count of what changed, for asserting the diff later."""
    out: list[dict[str, str]] = []
    stats = {"price_changed": 0, "status_changed": 0, "withdrawn": 0, "new": 0}

    for row in rows:
        roll = rng.random()

        if roll < WITHDRAWN_RATE:
            stats["withdrawn"] += 1
            continue

        row = dict(row)
        if roll < WITHDRAWN_RATE + PRICE_CHANGE_RATE and row.get("price"):
            # Real price moves are small and usually downward on stale inventory.
            try:
                price = float(row["price"])
            except ValueError:
                pass
            else:
                row["price"] = str(round(price * rng.uniform(0.90, 1.05), 2))
                stats["price_changed"] += 1
        elif (
            roll < WITHDRAWN_RATE + PRICE_CHANGE_RATE + STATUS_CHANGE_RATE
            and row.get("status") == "for_sale"
        ):
            row["status"] = "sold"
            stats["status_changed"] += 1

        out.append(row)

    # New listings: clone existing rows onto fresh ids so the diff sees inserts.
    next_id = max(int(r["listing_id"]) for r in rows) + 1
    for _ in range(int(len(rows) * NEW_LISTING_RATE)):
        clone = dict(rng.choice(rows))
        clone["listing_id"] = str(next_id)
        clone["status"] = "for_sale"
        out.append(clone)
        next_id += 1
        stats["new"] += 1

    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="only read N rows (for quick local runs)")
    ap.add_argument("--day1", default="2026-07-26")
    ap.add_argument("--day2", default="2026-07-27")
    args = ap.parse_args()

    day1, day2 = date.fromisoformat(args.day1), date.fromisoformat(args.day2)

    print(f"reading {RAW}...")
    rows = read_raw(args.limit)
    print(f"  {len(rows):,} rows")

    p1 = write_dump(rows, day1)
    print(f"wrote {p1} ({len(rows):,} rows)")

    churned, stats = apply_churn(rows, random.Random(SEED))
    p2 = write_dump(churned, day2)

    total = sum(stats.values())
    print(f"wrote {p2} ({len(churned):,} rows)")
    print(f"  churn: {stats} = {total:,} records ({total / len(rows):.2%})")


def _self_check() -> None:
    """Smallest thing that fails if the churn or date logic breaks."""
    rows = [
        {"listing_id": str(i), "price": "100000", "status": "for_sale"} for i in range(1, 10001)
    ]
    churned, stats = apply_churn(rows, random.Random(1))

    assert stats["withdrawn"] > 0 and stats["new"] > 0, stats
    assert len(churned) == len(rows) - stats["withdrawn"] + stats["new"], "row count mismatch"

    changed = sum(stats.values())
    assert 0.01 < changed / len(rows) < 0.05, f"churn {changed / len(rows):.2%} outside 1-5%"

    # Same id must yield the same list date, or every row looks changed.
    assert derive_list_date(42) == derive_list_date(42)
    assert (LIST_DATE_EPOCH - derive_list_date(42)).days < 730

    print("self-check ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        main()

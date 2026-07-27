#!/usr/bin/env python3
"""Generate synthetic HOA rules documents for a subset of condo listings.

Why HOA rules and nothing else: they produce the question that proves the whole
routing argument —

    "three-bed condos under $500k in Austin THAT ALLOW SHORT-TERM RENTALS"

Price, bedrooms and city are exact comparisons. "Allows short-term rentals" is
buried in conditional legal prose. Neither retrieval path answers it alone.

Every document is generated from one of three known policies, and the policy is
recorded in a manifest. That manifest is **ground truth for the eval** and is
deliberately never loaded into the database — the database only ever sees the
prose, which is the situation a real brokerage is actually in.

    uv run python scripts/make_documents.py --limit 300
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

from mls_agent.bedrock import Bedrock

DUMPS = Path("data/dumps")
OUT = Path("data/documents")

SEED = 20260727
VARIANTS_PER_POLICY = 8

# The three policies. The conditional one is the point of the exercise: it is
# what no boolean column can represent without losing the thing that matters.
POLICIES = {
    "permitted": (
        "Short-term rentals are clearly PERMITTED, with only routine registration "
        "or a nominal administrative fee."
    ),
    "prohibited": (
        "Short-term rentals are clearly PROHIBITED. Minimum lease terms of six or "
        "twelve months are required with no exceptions."
    ),
    "conditional": (
        "Short-term rentals are PROHIBITED IN GENERAL but with a real exception — "
        "for example units acquired before a cutoff year, or a capped number of "
        "lettings per year, or subject to board approval. The exception must be "
        "specific enough that a careful reader could not answer 'yes' or 'no' "
        "without reading the clause."
    ),
}

PROMPT = """Write an excerpt from the rules and regulations of a US condominium
association. This is realistic legal prose for a real-estate document, roughly
300-400 words.

Cover these sections, each with a heading:
- Leasing and Occupancy  ({policy})
- Pets and Animals
- Assessments and Reserves (mention whether a special assessment is anticipated)
- Parking and Storage

Style: dry, clause-numbered, the way an actual HOA document reads. Use concrete
specifics — dollar amounts, day counts, percentages, years. Do not include a
preamble, a title, or any commentary. Output only the document text.

Association name: {name}
"""

STREETS = [
    "Riverside Oaks",
    "Cedar Point",
    "Lakeview",
    "Millbrook",
    "Hillcrest",
    "Bayside",
    "Ashford",
    "Windermere",
    "Kingsley",
    "Fairmont",
]
SUFFIX = ["Condominiums", "Residences", "Commons", "Lofts", "Place", "Tower"]


def generate_variants(bedrock: Bedrock, rng: random.Random) -> dict[str, list[str]]:
    """A handful of real variants per policy, reused across many listings.

    Generating one document per listing would cost roughly 300x more for no
    additional demonstrative value — the same lesson as embed-once-at-ingest.
    """
    variants: dict[str, list[str]] = {}
    for policy, instruction in POLICIES.items():
        variants[policy] = []
        for i in range(VARIANTS_PER_POLICY):
            name = f"{rng.choice(STREETS)} {rng.choice(SUFFIX)}"
            text = bedrock.generate(PROMPT.format(policy=instruction, name=name))
            variants[policy].append(text.strip())
            print(f"  {policy:12} {i + 1}/{VARIANTS_PER_POLICY}  ({len(text)} chars)")
    return variants


CITIES = ("Austin", "Houston")


def pick_listings(limit: int, rng: random.Random) -> list[dict[str, str]]:
    """Choose which listings get a document set.

    Two decisions worth stating, because both affect whether the demo shows
    anything:

    1.  Condos are not labelled in this dataset, so small lot size is the proxy
        for "not a detached house". Crude, documented, harmless here.

    2.  Documents are concentrated in a handful of brokerages rather than
        scattered. Spread across all 110k brokers, every tenant would hold two
        documents and row-level security would make every demo query return
        almost nothing. Concentrating is also what reality looks like: a
        brokerage has its own inventory, and its own paperwork for it.
    """
    latest = sorted(DUMPS.glob("*/listings.csv"))[-1]
    by_broker: dict[str, list[dict[str, str]]] = {}

    with latest.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["city"] not in CITIES or row["status"] != "for_sale":
                continue
            try:
                if float(row["acre_lot"] or 1) > 0.06:
                    continue
            except ValueError:
                continue
            by_broker.setdefault(row["brokered_by"], []).append(row)

    if not by_broker:
        sys.exit("no candidate listings found; run scripts/make_dumps.py first")

    # Enough brokers that tenant isolation is demonstrable, few enough that each
    # one holds a meaningful book.
    ranked = sorted(by_broker.values(), key=len, reverse=True)
    pool: list[dict[str, str]] = []
    for listings in ranked:
        pool.extend(listings)
        if len(pool) >= limit * 2 and len({r["brokered_by"] for r in pool}) >= 3:
            break

    chosen = rng.sample(pool, min(limit, len(pool)))

    cities = {c: sum(1 for r in chosen if r["city"] == c) for c in CITIES}
    brokers = len({r["brokered_by"] for r in chosen})
    print(f"  cities: {cities} across {brokers} brokerages")
    return chosen


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true", help="skip Bedrock, use stub text")
    args = ap.parse_args()

    rng = random.Random(SEED)
    bedrock = Bedrock()

    print("generating document variants...")
    if args.dry_run:
        variants = {p: [f"[stub {p} {i}]" for i in range(VARIANTS_PER_POLICY)] for p in POLICIES}
    else:
        variants = generate_variants(bedrock, rng)

    listings = pick_listings(args.limit, rng)
    print(f"selected {len(listings):,} condo listings")

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []

    with (OUT / "documents.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["listing_id", "brokered_by", "doc_type", "title", "body"])
        w.writeheader()

        for row in listings:
            policy = rng.choice(list(POLICIES))
            body = rng.choice(variants[policy])
            title = f"Rules and Regulations — {row['city']} Condominium Association"

            w.writerow(
                {
                    "listing_id": row["listing_id"],
                    "brokered_by": row["brokered_by"],
                    "doc_type": "hoa_rules",
                    "title": title,
                    "body": body,
                }
            )
            # Ground truth. Never loaded into the database — the database only
            # gets the prose, which is the position a real brokerage is in.
            manifest.append(
                {
                    "listing_id": int(row["listing_id"]),
                    "city": row["city"],
                    "price": float(row["price"] or 0),
                    "bed": row["bed"],
                    "short_term_rental_policy": policy,
                }
            )

    (OUT / "ground_truth.json").write_text(json.dumps(manifest, indent=2))

    counts: dict[str, int] = {}
    for m in manifest:
        counts[m["short_term_rental_policy"]] = counts.get(m["short_term_rental_policy"], 0) + 1

    print(f"\nwrote {OUT / 'documents.csv'} ({len(manifest):,} documents)")
    print(f"wrote {OUT / 'ground_truth.json'}")
    print(f"  policy split: {counts}")
    print(f"  bedrock usage: {bedrock.usage}")


if __name__ == "__main__":
    main()

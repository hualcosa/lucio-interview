"""Does routing by data type actually matter? Measure it, both directions.

The review claims structured facts belong in SQL and prose belongs in vector
search. That is easy to assert and easy to doubt, so this runs the same
questions down both paths and reports what each gets right.

Three categories, and each fails differently when misrouted:

  structured   "under $500,000" is a comparison. Semantic search returns
               listings that are *about* being affordable, which is not the
               same thing and is worse because it looks right.

  document     "which buildings allow short-term rentals" cannot be expressed
               as a filter at all — there is no column. The structured tool
               has no parameter for it, which is itself the finding.

  hybrid       needs both halves. Either path alone answers half the question
               and silently drops the other.

    uv run python -m mls_agent.eval_routing
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mls_agent import domain
from mls_agent.db import admin_conn
from mls_agent.domain import AuthContext

GROUND_TRUTH = Path("data/documents/ground_truth.json")
RESULTS = Path("results/routing_eval.json")

# The dataset's condo inventory is concentrated in Houston, so that is the demo
# city. The review uses Austin illustratively; nothing turns on which.
CITY = "Houston"


@dataclass
class Question:
    id: str
    category: str
    text: str
    right_tool: str
    args: dict[str, Any]
    # What a correct answer must satisfy. Checked per returned row.
    check: str
    wrong_tool: str | None = None
    wrong_args: dict[str, Any] = field(default_factory=dict)
    # Set where a LOW score is the finding rather than a defect.
    demonstrates: str = ""


QUESTIONS: list[Question] = [
    # ---- structured only -------------------------------------------------
    Question(
        "s1",
        "structured",
        f"Three-bed listings under $500,000 in {CITY}",
        "search_listings",
        {"city": CITY, "beds": 3, "max_price": 500_000},
        check="price_under_500k_and_3_bed",
        wrong_tool="semantic_search",
        wrong_args={"question": f"three bedroom homes under $500,000 in {CITY}"},
    ),
    Question(
        "s2",
        "structured",
        "Listings that have been on the market more than 90 days",
        "search_listings",
        {"city": CITY, "min_days_on_market": 90},
        check="over_90_days",
        wrong_tool="semantic_search",
        wrong_args={"question": "listings that have been sitting unsold for a long time"},
    ),
    Question(
        "s3",
        "structured",
        "Listings between $200,000 and $300,000",
        "search_listings",
        {"city": CITY, "min_price": 200_000, "max_price": 300_000},
        check="price_200k_300k",
        wrong_tool="semantic_search",
        wrong_args={"question": "moderately priced listings around two to three hundred thousand"},
    ),
    Question(
        "s4",
        "structured",
        "How many active listings, and what is the median price?",
        "market_stats",
        {"city": CITY},
        check="has_median",
    ),
    # ---- document only ---------------------------------------------------
    Question(
        "d1",
        "document",
        "Which buildings permit short-term rentals?",
        "semantic_search",
        {"question": "short-term rentals are permitted and allowed"},
        check="policy_permitted",
        demonstrates=(
            "POLARITY. 'Short-term rentals are permitted' and 'short-term rentals "
            "are prohibited' sit almost on top of each other in embedding space — "
            "they are the same topic. Retrieval finds the right clause and cannot "
            "tell you which way it points. The model must read the passage; that "
            "is why the citation is returned and why retrieval is an input to the "
            "answer rather than the answer."
        ),
    ),
    Question(
        "d2",
        "document",
        "Which buildings prohibit short-term rentals outright?",
        "semantic_search",
        {"question": "short-term rentals are strictly prohibited, minimum lease"},
        check="policy_prohibited",
    ),
    Question(
        "d3",
        "document",
        "Is a special assessment anticipated in any of our buildings?",
        "semantic_search",
        {"question": "special assessment anticipated reserve shortfall"},
        check="mentions_assessment",
    ),
    Question(
        "d4",
        "document",
        "What are the pet restrictions?",
        "semantic_search",
        {"question": "pets dogs cats weight limit restrictions"},
        check="mentions_pets",
    ),
    # ---- hybrid ----------------------------------------------------------
    Question(
        "h1",
        "hybrid",
        f"Condos under $500,000 in {CITY} that allow short-term rentals",
        "hybrid_search",
        {"question": "short-term rentals are permitted", "city": CITY, "max_price": 500_000},
        check="under_500k_and_permitted",
        demonstrates=(
            "Same polarity limit as d1, now inside a hybrid query. The exact half "
            "(price, city) is perfect; the prose half retrieves the right clause "
            "without resolving which way it reads."
        ),
        wrong_tool="semantic_search",
        wrong_args={"question": f"condos under $500,000 in {CITY} that allow short-term rentals"},
    ),
    Question(
        "h2",
        "hybrid",
        "Listings over 90 days old whose rules prohibit short-term rentals",
        "hybrid_search",
        {"question": "short-term rentals prohibited", "city": CITY, "min_days_on_market": 90},
        check="over_90_days_and_prohibited",
        wrong_tool="semantic_search",
        wrong_args={"question": "old listings that prohibit short-term rentals"},
    ),
    Question(
        "h3",
        "hybrid",
        "Two-bed listings whose building anticipates a special assessment",
        "hybrid_search",
        {"question": "special assessment anticipated", "city": CITY, "beds": 2},
        check="two_bed",
        wrong_tool="semantic_search",
        wrong_args={"question": "two bedroom listings with an upcoming special assessment"},
    ),
]


def build_checks(truth: dict[int, dict]) -> dict[str, Any]:
    """Each check answers: is THIS returned row actually a correct answer?"""

    def policy(row) -> str:
        return truth.get(row.get("listing_id"), {}).get("short_term_rental_policy", "unknown")

    return {
        "price_under_500k_and_3_bed": lambda r: (
            r.get("price") is not None and r["price"] < 500_000 and r.get("bed") == 3
        ),
        "over_90_days": lambda r: (r.get("days_on_market") or 0) > 90,
        "price_200k_300k": lambda r: (
            r.get("price") is not None and 200_000 <= r["price"] <= 300_000
        ),
        "has_median": lambda r: r.get("median_price") is not None,
        "policy_permitted": lambda r: policy(r) == "permitted",
        "policy_prohibited": lambda r: policy(r) in ("prohibited", "conditional"),
        "mentions_assessment": lambda r: "assessment" in r.get("passage", "").lower(),
        "mentions_pets": lambda r: "pet" in r.get("passage", "").lower(),
        "under_500k_and_permitted": lambda r: (
            r.get("price") is not None and r["price"] < 500_000 and policy(r) == "permitted"
        ),
        "over_90_days_and_prohibited": lambda r: (
            (r.get("days_on_market") or 0) > 90 and policy(r) in ("prohibited", "conditional")
        ),
        "two_bed": lambda r: r.get("bed") == 2,
    }


def run_tool(auth: AuthContext, tool: str, args: dict) -> tuple[list[dict], float]:
    fn = {
        "search_listings": domain.search_listings,
        "market_stats": domain.market_stats,
        "semantic_search": domain.semantic_search,
        "hybrid_search": domain.hybrid_search,
    }[tool]

    args = dict(args)
    if tool == "market_stats":
        args.pop("limit", None)  # aggregates return one row by construction

    started = time.perf_counter()
    if tool in ("semantic_search", "hybrid_search"):
        result = fn(auth, args.pop("question"), **args)
    else:
        result = fn(auth, **args)
    return domain.serialise(result)["rows"], (time.perf_counter() - started) * 1000


def pick_demo_broker() -> int:
    with admin_conn() as conn:
        row = conn.execute(
            "SELECT brokered_by FROM documents GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise SystemExit("no documents loaded — run `python -m mls_agent.documents` first")
    return row[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10, help="rows per query")
    args = ap.parse_args()

    truth = {m["listing_id"]: m for m in json.loads(GROUND_TRUTH.read_text())}
    checks = build_checks(truth)

    broker = pick_demo_broker()
    auth = AuthContext(brokerage_id=broker, subject="eval@brokerage.com")
    print(f"brokerage {broker}\n")

    report = []
    for q in QUESTIONS:
        entry: dict[str, Any] = {"id": q.id, "category": q.category, "question": q.text}
        if q.demonstrates:
            entry["demonstrates"] = q.demonstrates
        check = checks[q.check]

        rows, ms = run_tool(auth, q.right_tool, {**q.args, "limit": args.limit})
        correct = sum(1 for r in rows if check(r))
        entry["right"] = {
            "tool": q.right_tool,
            "rows": len(rows),
            "correct": correct,
            "precision": round(correct / len(rows), 3) if rows else None,
            "ms": round(ms),
        }

        if q.wrong_tool:
            wrong_rows, wrong_ms = run_tool(
                auth, q.wrong_tool, {**q.wrong_args, "limit": args.limit}
            )
            wrong_correct = sum(1 for r in wrong_rows if check(r))
            entry["wrong"] = {
                "tool": q.wrong_tool,
                "rows": len(wrong_rows),
                "correct": wrong_correct,
                "precision": round(wrong_correct / len(wrong_rows), 3) if wrong_rows else None,
                "ms": round(wrong_ms),
            }

        report.append(entry)

        right = entry["right"]
        line = f"{q.id:3} {q.category:11} {q.right_tool:16} {right['correct']}/{right['rows']}"
        if "wrong" in entry:
            w = entry["wrong"]
            line += f"   vs {w['tool']}: {w['correct']}/{w['rows']}"
        print(line)
        if q.demonstrates:
            print(f"      ^ finding, not a defect: {q.demonstrates.split('.')[0]}.")

    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps({"brokerage": broker, "questions": report}, indent=2))

    print(f"\nwrote {RESULTS}")
    _summarise(report)


def _summarise(report: list[dict]) -> None:
    print("\n" + "=" * 62)
    for category in ("structured", "document", "hybrid"):
        rows = [r for r in report if r["category"] == category]
        right = [r["right"]["precision"] for r in rows if r["right"]["precision"] is not None]
        wrong = [
            r["wrong"]["precision"]
            for r in rows
            if "wrong" in r and r["wrong"]["precision"] is not None
        ]
        avg = lambda xs: f"{sum(xs) / len(xs):.0%}" if xs else "n/a"  # noqa: E731
        print(f"{category:11} correct path {avg(right):>5}   misrouted {avg(wrong):>5}")
    print("=" * 62)


if __name__ == "__main__":
    main()

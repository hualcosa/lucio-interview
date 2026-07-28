"""Demo harness. Deliberately not a chat window.

A chat box would hide exactly what needs showing. This exposes a fixed bank of
questions in three categories, and for each one it reports what actually
happened: which tool was selected, the arguments it was given, the SQL the
database received, the passages retrieved with their citations, timings.

There is one free-form box. Its job is to be **refused**. The model may pick a
vetted tool and fill typed arguments; it may not write a query, name a
brokerage, or reach anything not on the list. Watching that containment hold is
the demonstration — an open chat would demonstrate the opposite.

    uv run python -m mls_agent.demo_server     # http://localhost:8000
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from mls_agent import domain, mcp_adapter
from mls_agent.bedrock import Bedrock
from mls_agent.db import admin_conn
from mls_agent.domain import AuthContext

STATIC = Path(__file__).parent / "static"

CITY = "Houston"

QUESTION_BANK = [
    {
        "id": "s1",
        "category": "Structured",
        "blurb": "Exact facts. No embeddings involved.",
        "question": f"Three-bed listings under $500,000 in {CITY}",
        "tool": "search_listings",
        "args": {"city": CITY, "beds": 3, "max_price": 500_000, "limit": 10},
    },
    {
        "id": "s2",
        "category": "Structured",
        "blurb": "A date comparison. Similarity has no notion of 'more than 90 days'.",
        "question": "Listings sitting on the market more than 90 days",
        "tool": "search_listings",
        "args": {"city": CITY, "min_days_on_market": 90, "limit": 10},
    },
    {
        "id": "s3",
        "category": "Structured",
        "blurb": "An aggregate. Vector search cannot compute a median.",
        "question": "How many active listings, and what is the median price?",
        "tool": "market_stats",
        "args": {"city": CITY},
    },
    {
        "id": "d1",
        "category": "Document",
        "blurb": "No column holds this. It lives in the building's rules.",
        "question": "What do our buildings say about short-term rentals?",
        "tool": "semantic_search",
        "args": {"question": "short-term rentals minimum lease term", "limit": 5},
    },
    {
        "id": "d2",
        "category": "Document",
        "blurb": "Retrieved by meaning, returned with its citation.",
        "question": "Is a special assessment anticipated anywhere?",
        "tool": "semantic_search",
        "args": {"question": "special assessment anticipated reserve shortfall", "limit": 5},
    },
    {
        "id": "d3",
        "category": "Document",
        "blurb": "Pet rules are prose, not a boolean.",
        "question": "What are the pet restrictions?",
        "tool": "semantic_search",
        "args": {"question": "pets dogs cats weight limit restrictions", "limit": 5},
    },
    {
        "id": "h1",
        "category": "Hybrid",
        "blurb": "Price is exact; 'allows short-term rentals' is buried in legal prose. "
        "Neither path answers this alone.",
        "question": f"Condos under $500,000 in {CITY} whose rules address short-term rentals",
        "tool": "hybrid_search",
        "args": {
            "question": "short-term rentals permitted or prohibited",
            "city": CITY,
            "max_price": 500_000,
            "limit": 8,
        },
    },
    {
        "id": "h2",
        "category": "Hybrid",
        "blurb": "Stale inventory, and why: the structured half finds them, "
        "the documents explain them.",
        "question": "Listings over 90 days old whose building anticipates a special assessment",
        "tool": "hybrid_search",
        "args": {
            "question": "special assessment anticipated",
            "city": CITY,
            "min_days_on_market": 90,
            "limit": 8,
        },
    },
]


def _auth() -> AuthContext:
    """The demo principal.

    In deployment this is built from a verified Cognito token. Here it is fixed —
    but note it is still constructed server-side and still enforced by row-level
    security. The browser cannot influence it.
    """
    with admin_conn() as conn:
        broker = conn.execute(
            "SELECT brokered_by FROM documents GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()[0]
    return AuthContext(brokerage_id=broker, subject="demo@brokerage.example")


AUTH = _auth()


def _run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    payload = mcp_adapter.dispatch(AUTH, tool, args)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    return {
        "payload": payload,
        "trace": {
            "tool": tool,
            "arguments": args,
            "brokerage": AUTH.brokerage_id,
            "principal": AUTH.subject,
            "elapsed_ms": elapsed_ms,
            "rows": payload.get("row_count"),
            "as_of": payload.get("as_of"),
        },
    }


async def questions(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "brokerage": AUTH.brokerage_id,
            "questions": [
                {k: v for k, v in q.items() if k != "args"} | {"args": q["args"]}
                for q in QUESTION_BANK
            ],
        }
    )


async def ask(request: Request) -> JSONResponse:
    body = await request.json()
    question = next((q for q in QUESTION_BANK if q["id"] == body.get("id")), None)
    if question is None:
        return JSONResponse({"error": "unknown question"}, status_code=404)

    try:
        return JSONResponse(_run(question["tool"], dict(question["args"])))
    except Exception as exc:  # noqa: BLE001 - surfaced to the panel deliberately
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=400)


TOOL_CHOICE_PROMPT = """You route questions to tools for a real-estate brokerage.

Available tools and their exact parameters:
{tools}

Reply with ONLY a JSON object: {{"tool": "<name>", "arguments": {{...}}}}
If no tool fits, reply {{"tool": null, "reason": "<why>"}}.

Rules you cannot break:
- Use only the parameters listed. Anything else will be rejected.
- You cannot specify which brokerage, office or owner. That is decided elsewhere.
- You cannot write SQL.

Question: {question}"""


async def freeform(request: Request) -> JSONResponse:
    """The containment demo.

    The model chooses a tool and arguments. Everything it returns is validated
    against the published schema before anything touches the database, so an
    invented tool, an unexpected argument, or an attempt to name a brokerage is
    rejected here rather than executed.
    """
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    trace: dict[str, Any] = {"stage": "model", "question": question}
    started = time.perf_counter()

    tool_summary = json.dumps(
        [
            {"name": t.name, "parameters": sorted(t.inputSchema["properties"])}
            for t in mcp_adapter.TOOLS
        ],
        indent=1,
    )

    bedrock = Bedrock()
    raw = bedrock.generate(
        TOOL_CHOICE_PROMPT.format(tools=tool_summary, question=question),
        max_tokens=400,
        temperature=0.0,
    )
    trace["model_raw"] = raw.strip()
    trace["model_ms"] = round((time.perf_counter() - started) * 1000)

    try:
        chosen = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            {"rejected": "the model did not return a usable tool choice", "trace": trace}
        )

    tool = chosen.get("tool")
    args = chosen.get("arguments") or {}
    trace["tool"] = tool
    trace["arguments"] = args

    # --- guardrails, in order --------------------------------------------
    spec = next((t for t in mcp_adapter.TOOLS if t.name == tool), None)
    if spec is None:
        return JSONResponse(
            {
                "rejected": f"no such tool: {tool!r}",
                "why": "The model may only choose from the published list.",
                "trace": trace,
            }
        )

    allowed = set(spec.inputSchema["properties"])
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        return JSONResponse(
            {
                "rejected": f"unexpected arguments: {unexpected}",
                "why": (
                    "Every tool declares additionalProperties: false. An argument "
                    "outside the schema is refused, not ignored."
                ),
                "trace": trace,
            }
        )

    limit = args.get("limit")
    if isinstance(limit, int) and limit > domain.MAX_LIMIT:
        args["limit"] = domain.MAX_LIMIT
        trace["limit_capped_to"] = domain.MAX_LIMIT

    try:
        result = _run(tool, args)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"rejected": f"{type(exc).__name__}: {exc}", "trace": trace}, status_code=200
        )

    result["trace"] = trace | result["trace"]
    return JSONResponse(result)


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/questions", questions),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/freeform", freeform, methods=["POST"]),
    ]
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

"""MCP protocol adapter. Deliberately thin.

This module translates tool calls into domain calls and shapes the results. It
holds no business logic, no SQL, and no authorisation decisions — it *carries* an
identity that was verified elsewhere and hands it down.

That thinness is the argument in RESPONSE.md 3.6. Everything below could be
replaced by a REST controller, a scheduled job, or a second protocol without the
domain layer noticing.

Targets MCP spec `2025-11-25` (stable). The `2026-07-28` revision removes
sessions and makes requests self-contained, which suits this design — nothing
here keeps per-connection state. Migration notes are in `MIGRATION.md`.

Tools are coarse, vetted and parameterised. There is no tool that accepts SQL.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as types
from mcp.server import Server

from mls_agent import domain
from mls_agent.domain import AuthContext

SERVER_NAME = "mls-agent"
PROTOCOL_VERSION = "2025-11-25"


# ---------------------------------------------------------------------------
# Tool definitions
#
# Each schema is a contract with the model: these arguments, these types, this
# range. Note what is absent from every one of them — any way to name a
# brokerage. Scope comes from the verified token, never from the model
# (RESPONSE.md 3.5).
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="search_listings",
        description=(
            "Search property listings by exact criteria. Use this for any question "
            "involving price, bedrooms, bathrooms, location, status or how long a "
            "listing has been on the market. All filters are exact comparisons."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name; tolerant of misspelling"},
                "state": {"type": "string"},
                "zip_code": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": sorted(domain.VALID_STATUS),
                    "default": "for_sale",
                },
                "min_price": {"type": "number", "minimum": 0},
                "max_price": {"type": "number", "minimum": 0},
                "beds": {"type": "integer", "minimum": 0, "description": "Exact bedroom count"},
                "min_beds": {"type": "integer", "minimum": 0},
                "baths": {"type": "integer", "minimum": 0},
                "min_days_on_market": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Only listings that have been listed at least this long",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": domain.MAX_LIMIT,
                    "default": domain.DEFAULT_LIMIT,
                },
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="market_stats",
        description=(
            "Aggregate statistics for a market: listing count, median price, price "
            "range, average days on market. Use this for 'how many', 'what is the "
            "typical' or 'on average' questions rather than fetching rows and counting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "state": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": sorted(domain.VALID_STATUS),
                    "default": "for_sale",
                },
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="flag_listing",
        description=(
            "Flag a listing for human review — for example a stale price or a "
            "suspected data error. This PREPARES an action and returns it awaiting "
            "approval. It does not change the listing. Tell the user their request "
            "is pending review, not that it is done."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "listing_id": {"type": "integer"},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["listing_id", "reason"],
            "additionalProperties": False,
        },
    ),
]

HANDLERS: dict[str, Callable[..., domain.Result]] = {
    "search_listings": domain.search_listings,
    "market_stats": domain.market_stats,
    "flag_listing": domain.flag_listing,
}


def dispatch(auth: AuthContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route one tool call. The entire adapter, essentially.

    Separated from the protocol server so it can be tested — and reused by a
    Lambda handler, a REST route, or an evaluation harness — without a session.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name}")

    if name == "flag_listing":
        result = handler(auth, arguments["listing_id"], arguments["reason"])
    else:
        result = handler(auth, **arguments)

    payload = domain.serialise(result)

    # Freshness is stated in prose as well as in the payload. Models reliably
    # read the text; they do not reliably notice a field (RESPONSE.md 3.7).
    if payload.get("as_of"):
        payload["_note"] = (
            f"Data is current as of the {payload['as_of']} overnight load and may be "
            f"up to 24 hours old. Say so if the answer is time-sensitive."
        )
    if payload.get("truncated"):
        payload["_note_truncated"] = (
            "More rows matched than were returned. Narrow the filters rather than "
            "presenting this as the complete set."
        )
    return payload


def build_server(auth_for_request: Callable[[], Awaitable[AuthContext]]) -> Server:
    """Wire the protocol surface.

    `auth_for_request` is injected rather than imported so the transport owns
    identity extraction. In deployment it validates a Cognito JWT; in tests it
    returns a fixed principal. The adapter never decides who the caller is.
    """
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
        auth = await auth_for_request()
        try:
            payload = dispatch(auth, name, arguments or {})
        except PermissionError as exc:
            # Deliberately indistinguishable from "not found": telling a caller
            # that a record exists but is not theirs is itself a disclosure.
            return _text({"error": "not_found", "detail": str(exc)})
        except ValueError as exc:
            return _text({"error": "invalid_arguments", "detail": str(exc)})

        return _text(payload)

    return server


def _text(payload: dict[str, Any]) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

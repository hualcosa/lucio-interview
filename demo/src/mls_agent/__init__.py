"""Working demo for the architecture review in ../RESPONSE.md.

Layering is deliberate and is itself part of the argument:

    domain/     business logic. Knows nothing about MCP. Independently testable.
    ingest/     CSV diff and upsert. One-time backfill and nightly delta share this.
    mcp/        thin protocol adapter. Translates tool schemas to domain calls.
    naive/      faithful reimplementation of the draft design, for the benchmark.

Nothing in `domain/` may import from `mcp/`. That boundary is the point (RESPONSE.md 3.6).
"""

__version__ = "0.1.0"

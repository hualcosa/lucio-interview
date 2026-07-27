# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A take-home exercise for a **Principal Solutions Architect** hiring process. The full prompt is in `email.md` (from the hiring manager, Lucio) and is the source of truth — read it before doing anything here.

As of now the repo contains **only `email.md`**. There is no code, no build system, no test suite, and no git repository. Do not invent build/lint/test commands; if tooling is added later, document the actual commands here.

The primary deliverable is an **architecture critique and redesign**, not a production system. Optimize output for a reviewer reading it in a few minutes: written response + a 3–5 minute screen recording narrating the reasoning.

## Deliverables (from `email.md`)

1. **Written response** (doc, diagram, or plain text): what's wrong with the junior engineer's draft architecture, plus the proposed fixed version — what changes, what stays, and *why*.
2. **3–5 min screen recording** (Loom or similar) walking through the reasoning out loud, specifically the **two or three decisions to defend hardest under client pushback**.
3. Two extra sections in the written response:
   - A **real** MCP integration (or similar) previously built, and what it did.
   - How the **first two weeks** would be structured leading a small team building this.

## The scenario

Client in a traditional non-tech industry (pick one: real estate, healthcare, or legal). They want an AI agent that queries and acts on their data via natural language, exposed over **MCP**.

### Draft architecture under review (the junior engineer's proposal — all of this is the problem statement, not a target state)

- Legacy system exports a full data dump to S3 nightly.
- A single Lambda reads the **whole** S3 dump on **every** user query, searches it in memory, returns results to the agent.
- No authentication layer — "we'll add it later once the demo works."
- All vector-search embeddings regenerated **from scratch on every query**.
- MCP server and business logic live in the **same** Lambda.

### Hard constraints any proposal must satisfy

- **~3M records**, growing **~5%/month** (compounding — call out the 12-/24-month figures).
- **Raw records never leave the client's AWS account or region** (compliance). This constrains embedding/LLM provider choice and any managed third-party service.
- **Capped monthly infra budget** — must be affordable at this scale, not a blank check. Cost reasoning should be explicit, with rough numbers.
- **Nightly batch export only** — no real-time API into the legacy system. Data freshness is bounded by this; don't propose designs that assume live reads.
- **Conversational query latency** (a few seconds).

Note the tension worth surfacing: the agent must "**act on**" their data, but the legacy system is read-only batch — writes need a defined path (queue/outbox/write-back window or system-of-record boundary), not hand-waving.

## Working conventions for this repo

- Keep artifacts few and reviewer-friendly. Prefer a single written response file plus, at most, one diagram; avoid sprawling multi-file structures that a reviewer has to navigate.
- Diagrams: prefer Mermaid in Markdown (renders on GitHub and in Artifacts) over binary image formats, so it stays diffable.
- Every architectural claim should tie back to a named constraint above (cost, compliance/residency, latency, batch-only ingest, scale). Recommendations without that linkage are the main failure mode of this exercise.
- Cover both the **critique** and the **fix** — the prompt asks for problems *and* the corrected design; a redesign alone under-delivers.
- The recording script is a deliverable too: if drafting talking points, keep them to what fits in 3–5 minutes spoken (~450–750 words).

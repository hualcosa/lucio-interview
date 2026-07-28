# Architecture review — a natural-language agent over legacy real-estate data, via MCP

A brokerage wants its staff to ask questions of their own data in plain English, and to act on
the answers. The data lives in an MLS that exports a nightly file and offers no live API. A
junior engineer drafted an architecture for it. This repository is the review of that draft,
the proposed replacement, and a working implementation that measures both.

**Start with [`RESPONSE.md`](RESPONSE.md)** — it is the deliverable, and it stands on its own.
Everything else here exists to support a claim it makes.

---

## What is in here

| | |
|---|---|
| **[`RESPONSE.md`](RESPONSE.md)** | The review. Assumptions, what the draft gets right, six problems with what replaces each, the revised architecture with costs and safeguards, and a two-week plan. Builds to a 16-page PDF. |
| **[`PRESENTATION.md`](PRESENTATION.md)** | Narration script and timing for the accompanying five-minute recording. |
| **[`demo/`](demo/)** | The architecture, built and measured. See [`demo/README.md`](demo/README.md). |
| **[`demo/results/`](demo/results/)** | Committed measurements — the benchmark and the routing evaluation, with the method beside them. |
| [`email.md`](email.md) | The original brief, kept verbatim so the review can be read against what was asked. |
| [`context.md`](context.md), [`plan.md`](plan.md) | Working notes. Reasoning that did not survive into the review, and the task breakdown. Not part of the deliverable. |
| [`assets/`](assets/), [`scripts/`](scripts/) | The architecture diagram, and the build scripts for the PDF and the deck. |

## The argument in one paragraph

The draft treats the nightly export file as though the file were the database — reloading and
scanning all of it on every question, re-embedding the corpus each time, with no authentication
and with the MCP protocol and the business logic sharing one Lambda. Four of its five problems
are that single missing boundary between *receiving* data and *serving* it. The fix keeps the
nightly export exactly as it is and puts a database behind it, routes questions by the kind of
data they concern — columns queried exactly, documents searched semantically, both in one
PostgreSQL — and carries the caller's verified identity all the way down to row-level security.

> **Natural language is the interface. It is not the execution engine.**

## The numbers are measured, not asserted

Both designs were implemented and run against 2.2 million real listing records.

| Records | Draft design | Revised design |
|---:|---|---|
| 100,000 | 610 ms · $0.30/query | 33 ms |
| 1,000,000 | 5,483 ms · $3.00/query | 31 ms |
| **2,221,849** | **11,991 ms · $6.67/query** | **33 ms** |

Retrieval only; the model adds one to three seconds to both, which is why it is excluded.
Raw output and method are in [`demo/results/`](demo/results/), regenerable with `make bench`.

One measurement contradicted the review and the correction was kept: memory was expected to be
the wall, and it is not — the real argument is latency and cost. See §3.2.

## Building the documents

```bash
./scripts/build_pdf.sh RESPONSE.md                                  # -> RESPONSE.pdf
uv run --with python-pptx --with pillow python scripts/build_deck.py  # -> PRESENTATION.pptx
```

Both need [pandoc](https://pandoc.org/), [mermaid-cli](https://github.com/mermaid-js/mermaid-cli)
and `wkhtmltopdf` on `PATH`. The Mermaid diagrams are extracted from `RESPONSE.md` itself rather
than duplicated, so the deck cannot drift from the document. The generated `.pdf` and `.pptx`
are build artifacts and are not tracked.

## Running the implementation

```bash
cd demo
make install && make up && make test
```

Full instructions, including the AWS deployment and its guardrails, are in
[`demo/README.md`](demo/README.md).

## Scope, stated honestly

The implementation exists to support the review, not to be a product. Two pieces are built and
proven by tests but not deployed: the Cognito authorization server in front of the identity
chain, and the AWS stacks beyond the budget guardrail. The token → session variable → row-level
security chain itself **is** implemented and tested — `demo/tests/test_rls.py` asserts that one
principal cannot read another's rows.

# Working demo

The architecture argued for in [`../RESPONSE.md`](../RESPONSE.md), built and measured.

The point is not that it works — it is that **the draft design's failure is demonstrated
rather than asserted**. `benchmark.py` carries a faithful reimplementation of the junior
engineer's approach alongside the revised one, run against the same data.

## Layout

```
src/mls_agent/
  domain.py        business logic. Knows nothing about MCP. Independently testable.
  mcp_adapter.py   thin protocol adapter. Tool schemas → domain calls.
  ingest.py        CSV diff and upsert, with the circuit breaker.
  documents.py     chunk and embed listing documents, once, on arrival.
  bedrock.py       the only module that talks to a model.
  db.py            connection handling and the RLS session variable.
  benchmark.py     draft design vs revised, at five scales.
  eval_routing.py  does a question reach the right retrieval path?
  demo_server.py   a small UI for walking through queries with traceability.
  migrations/      schema, indexes, row-level security, pgvector.
infra/             AWS CDK in TypeScript. Guardrails, asserted in tests.
tests/             pytest. Concentrated on domain and on RLS.
```

**`domain.py` may not import from `mcp_adapter.py`.** That boundary is the argument in §3.6
of the review, made enforceable rather than aspirational — and the reverse import is what
`test_domain.py` exercises without ever loading a protocol.

## Running it locally

Local Postgres is free and fast; iterating against Aurora burns capacity-hours.

```bash
make install     # uv sync + npm install
make up          # start Postgres on :5433
make test        # pytest + CDK assertion tests
make down        # stop and delete local data
```

## Deploying

**The budget alarm deploys before anything that can spend.** That ordering is enforced by
`bin/app.ts` refusing to synthesize without a notification address.

```bash
export BUDGET_EMAIL=you@example.com
export AWS_PROFILE=...            # or a default SSO profile

make synth                        # inspect the template first
make deploy
```

Prerequisites that are **not** automatable and will block a deploy:

- Bedrock model access enabled in-console for the target region
- `cdk bootstrap` run once for the account/region pair

Teardown, which should happen before any long idle period:

```bash
make destroy
```

## Cost

Designed to be run ephemerally: deploy, measure, capture results, destroy. Estimated total
for a full run is **\$15–25**, dominated by Aurora capacity-hours rather than by data volume —
2.2M records is a 40 MB file and costs almost nothing to store or load.

Guardrails in the stack: monthly budget with forecast alerting, a ceiling on database
capacity, Lambda concurrency limits, S3 lifecycle rules, and no NAT Gateway. All asserted in
`infra/test/`, because a control nobody verifies quietly disappears by the third sprint.

## Data

Real structured records from the public [USA Real Estate
Dataset](https://www.kaggle.com/datasets/ahmedshahriarsakib/usa-real-estate-dataset)
(~2.2M realtor.com listings), pulled from a HuggingFace mirror so no Kaggle account is
needed.

The dataset has **no free-text fields**, which is exactly the point the review turns on: the
structured half of a real MLS export looks like this, and embedding it would destroy the
precision the client is paying for.

The document half is therefore synthesised — roughly 300 HOA rule sets and disclosures,
generated with `make docs` and embedded once on arrival, giving 1,315 passages. They exist so
that hybrid search can be demonstrated on the kind of prose that genuinely needs it: *"does
this building allow short-term rentals"* is buried in a paragraph written by a lawyer, and no
column will ever hold it.

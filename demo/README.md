# Working demo

The architecture argued for in [`../RESPONSE.md`](../RESPONSE.md), built and measured.

The point is not that it works — it is that **the draft design's failure is demonstrated
rather than asserted**. `src/mls_agent/naive/` is a faithful reimplementation of the junior
engineer's approach, benchmarked against the revised one on the same data, on the same
infrastructure.

## Layout

```
src/mls_agent/
  domain/    business logic. Knows nothing about MCP. Independently testable.
  ingest/    CSV diff and upsert. Backfill and nightly delta share this code.
  mcp/       thin protocol adapter. Tool schemas → domain calls.
  naive/     the draft design, reimplemented, for the benchmark.
infra/       AWS CDK in TypeScript.
```

**`domain/` may not import from `mcp/`.** That boundary is the argument in §3.6 of the
review, made enforceable rather than aspirational.

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
for a full run is **$15–25**, dominated by Aurora capacity-hours rather than by data volume —
2.2M records is a 40 MB file and costs almost nothing to store or load.

Guardrails in the stack: monthly budget with forecast alerting, a ceiling on database
capacity, Lambda concurrency limits, S3 lifecycle rules, and no NAT Gateway. All asserted in
`infra/test/`, because a control nobody verifies quietly disappears by the third sprint.

## Data

Real structured records from the public [USA Real Estate
Dataset](https://www.kaggle.com/datasets/ahmedshahriarsakib/usa-real-estate-dataset)
(~2.2M realtor.com listings), pulled from a HuggingFace mirror so no Kaggle account is
needed.

The dataset has **no free-text fields**, which is precisely why the structured-only
assumption in §1.3 of the review is coherent for it — and why the design carries no
embeddings.

.PHONY: help install data dumps docs up down psql test lint fmt bench eval serve synth diff deploy destroy
.DEFAULT_GOAL := help

DB_URL ?= postgresql://mls:mls@localhost:5433/mls
export DB_URL

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python and CDK dependencies
	uv sync
	cd infra && npm install

data: data/realtor-raw.csv  ## Download the raw dataset (~170MB)
data/realtor-raw.csv:
	@mkdir -p data
	curl -sL -o $@ "https://huggingface.co/datasets/jason1966/ahmedshahriarsakib_usa-real-estate-dataset/resolve/main/realtor-data.zip.csv"

dumps: data  ## Generate two consecutive nightly exports (~2min, 470MB)
	uv run python scripts/make_dumps.py

up:  ## Start local Postgres
	docker compose up -d --wait

down:  ## Stop local Postgres and delete its data
	docker compose down -v

psql:  ## Open a shell against local Postgres
	docker compose exec db psql -U mls -d mls

test:  ## Run Python tests and CDK assertion tests
	uv run pytest
	cd infra && npm test

lint:  ## Lint
	uv run ruff check .
	cd infra && npx tsc --noEmit

fmt:  ## Format
	uv run ruff format .
	uv run ruff check --fix .

synth:  ## Synthesize the CloudFormation template
	cd infra && npx cdk synth

diff:  ## Diff deployed stack against local
	cd infra && npx cdk diff

deploy:  ## Deploy to AWS. Budget alarm goes up first; see infra/README.md
	cd infra && npx cdk deploy --all

destroy:  ## Tear everything down. Run this before any long idle period.
	cd infra && npx cdk destroy --all

bench:  ## Run the naive-vs-revised benchmark
	uv run python -m mls_agent.benchmark

eval:  ## Run the routing eval
	uv run python -m mls_agent.eval_routing

docs:  ## Generate and ingest HOA documents (calls Bedrock)
	uv run python scripts/make_documents.py --limit 300
	uv run python -m mls_agent.documents

serve:  ## Run the demo UI on http://localhost:8000
	uv run python -m mls_agent.demo_server

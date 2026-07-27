.PHONY: help install up down psql test lint fmt synth diff deploy destroy
.DEFAULT_GOAL := help

DB_URL ?= postgresql://mls:mls@localhost:5433/mls
export DB_URL

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python and CDK dependencies
	uv sync
	cd infra && npm install

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

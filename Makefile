.DEFAULT_GOAL := help
.PHONY: help install db db-stop seed serve web test lint format web-lint web-build eval-offline eval-smoke eval-real ledger docker-build docker-up clean

DB_URL ?= postgresql://tally:tally@127.0.0.1:55460/tally
export TALLY_DATABASE_URL ?= $(DB_URL)
export TEST_DATABASE_URL ?= postgresql://tally:tally@127.0.0.1:55460/tally_test
FREE_MODEL ?= nvidia/nemotron-3-super-120b-a12b:free
FREE_FALLBACKS ?= nvidia/nemotron-3-ultra-550b-a55b:free,dots-studio/dots-3-note-preview:free

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Python dependencies (uv) and the web app's (npm)
	uv sync
	cd web && npm ci --no-audit --no-fund

db:  ## Local PostgreSQL 17 on port 55460 (with a test database)
	docker run -d --name tally-db -e POSTGRES_USER=tally -e POSTGRES_PASSWORD=tally -e POSTGRES_DB=tally \
		-p 127.0.0.1:55460:5432 postgres:17-alpine
	@until docker exec tally-db pg_isready -U tally >/dev/null 2>&1; do sleep 1; done
	docker exec tally-db psql -U tally -c "CREATE DATABASE tally_test" || true

db-stop:  ## Remove the local database container
	docker rm -f tally-db

seed:  ## Generate the Lumora demo database, the read-only role and its grants
	uv run tally seed

serve:  ## API on http://localhost:8000 (offline demo model unless TALLY_LLM_PROVIDER is set)
	uv run tally serve --port 8000

web:  ## Next.js dev server on http://localhost:3000
	cd web && npm run dev

test:  ## Python tests (no API keys; database tests need TEST_DATABASE_URL)
	uv run pytest

lint:  ## Ruff lint + format check + mypy (strict)
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

format:  ## Auto-format and fix lint issues
	uv run ruff format src tests
	uv run ruff check --fix src tests

web-lint:  ## ESLint + TypeScript type check
	cd web && npm run lint && npm run typecheck

web-build:  ## Production build of the web app
	cd web && npm run build

eval-offline:  ## The whole benchmark with the offline model (checks the gold SQL and the guards; no API calls)
	uv run tally eval run --name fake --provider fake

eval-smoke:  ## List free OpenRouter models and smoke-test a few (needs OPENROUTER_API_KEY)
	uv run tally eval free-models
	uv run tally eval smoke --models "$(FREE_MODEL),$(FREE_FALLBACKS)"

eval-real:  ## Full benchmark, ablations and model comparison with free models (needs OPENROUTER_API_KEY, ~450 calls)
	uv run tally eval run --name main --provider openrouter --model $(FREE_MODEL) --fallbacks $(FREE_FALLBACKS)
	uv run tally eval run --name ablation_full_schema --subset --no-answers --retrieval full --provider openrouter --model $(FREE_MODEL) --fallbacks $(FREE_FALLBACKS)
	uv run tally eval run --name ablation_no_semantic_layer --subset --no-answers --no-semantic-layer --provider openrouter --model $(FREE_MODEL) --fallbacks $(FREE_FALLBACKS)
	uv run tally eval compare
	uv run tally eval ledger

ledger:  ## Summarize the call ledger into results/calls_summary.json
	uv run tally eval ledger

docker-build:  ## Build the API and web images
	docker compose build

docker-up:  ## Everything in Docker: web :3000, API :8000, PostgreSQL with the demo data
	docker compose up --build

clean:  ## Remove caches (keeps results/)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis web/.next

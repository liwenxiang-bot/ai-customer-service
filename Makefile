# AI Customer Service — developer workflows.
# Most targets assume you are in the repo root.

BACKEND := backend
PY := $(BACKEND)/.venv/bin/python
PIP := $(BACKEND)/.venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup deps-up deps-down migrate bootstrap dev-backend dev-worker \
        dev-embed dev-admin dev-widget build-widget build-admin test eval lint \
        stack-up stack-down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv + install all deps (backend, widget, admin)
	cd $(BACKEND) && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
	cd widget && pnpm install
	cd admin && pnpm install
	@test -f .env || cp .env.example .env
	@echo "✅ setup done. Edit .env (LLM_API_KEY etc.), then: make deps-up migrate bootstrap"

deps-up: ## Start infra deps (postgres+pgvector, redis, minio)
	docker compose up -d postgres redis minio

deps-down: ## Stop infra deps
	docker compose down

migrate: ## Run DB migrations
	cd $(BACKEND) && .venv/bin/alembic upgrade head

bootstrap: ## Seed default admin / AI config / channels
	cd $(BACKEND) && .venv/bin/python -m scripts.bootstrap

dev-backend: ## Run the API server (hot reload)
	cd $(BACKEND) && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-worker: ## Run the ARQ worker
	cd $(BACKEND) && .venv/bin/arq app.tasks.worker.WorkerSettings

dev-embed: ## Run the local Chinese embedding server (bge-small-zh, :8100)
	cd $(BACKEND) && .venv/bin/python -m scripts.local_embedding_server

dev-admin: ## Run the admin frontend (Vite dev, :5173)
	cd admin && pnpm dev

dev-widget: ## Run the widget dev preview (:5174)
	cd widget && pnpm dev

build-widget: ## Build the embeddable widget into widget/dist
	cd widget && pnpm build

build-admin: ## Build the admin app into admin/dist
	cd admin && pnpm build

test: ## Run backend tests
	cd $(BACKEND) && .venv/bin/python -m pytest -q

eval: ## Run the RAG retrieval evaluation
	cd $(BACKEND) && .venv/bin/python -m scripts.rag_eval

lint: ## Lint the backend
	cd $(BACKEND) && .venv/bin/ruff check app

stack-up: ## Build & run the FULL stack in docker (backend+worker+admin+deps)
	docker compose --profile app up -d --build

stack-down: ## Stop the full stack
	docker compose --profile app down

clean: ## Remove build artifacts
	rm -rf widget/dist admin/dist $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache

# ACVM developer & operator entry points. Requires: uv, docker (compose ≥ 2.24), node 24 for the console.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

UV          ?= uv
RUN         := $(UV) run --no-sync
COMPOSE_DIR := deploy/compose
DEV_COMPOSE := docker compose --project-directory $(COMPOSE_DIR) --env-file $(COMPOSE_DIR)/dev.env \
               -f $(COMPOSE_DIR)/docker-compose.yml -f $(COMPOSE_DIR)/docker-compose.dev.yml
PROD_COMPOSE := docker compose --project-directory $(COMPOSE_DIR) -f $(COMPOSE_DIR)/docker-compose.yml
# ACVM_SECRET__* must be real environment variables (EnvSecretStore reads os.environ), so export ./.env first
DOTENV      := set -a; if [ -f .env ]; then . ./.env; fi; set +a;
REGISTRY    ?= ghcr.io/tburn-labs
VERSION     ?= $(shell git describe --tags --always 2>/dev/null || echo dev)

.PHONY: help
help: ## list targets
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ─────────────────────────── development ───────────────────────────
.PHONY: install
install: ## uv sync (with dev group) + console npm ci; creates .env from .env.example if missing
	$(UV) sync --frozen
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
	cd console && npm ci

.PHONY: lint
lint: ## ruff + dashboard drift check
	$(RUN) ruff check src tests deploy/grafana
	$(RUN) python deploy/grafana/build_dashboards.py --check

.PHONY: format
format: ## ruff format + autofix
	$(RUN) ruff check --fix src tests deploy/grafana
	$(RUN) ruff format src tests deploy/grafana

.PHONY: typecheck
typecheck: ## mypy (strict)
	$(RUN) mypy src

.PHONY: test
test: ## unit tests (no live infrastructure, no ffmpeg)
	$(RUN) pytest tests/unit -m "not integration and not render" -q

.PHONY: test-cov
test-cov: ## unit tests with coverage (pytest-cov fetched ephemerally, not a project dependency)
	$(UV) run --frozen --with pytest-cov pytest tests/unit -m "not integration and not render" \
	  --cov=acvm --cov-report=term-missing:skip-covered --cov-report=xml

.PHONY: test-render
test-render: ## render tests (needs ffmpeg + Noto CJK fonts on PATH, or run inside the render image)
	$(RUN) pytest -m render -q

.PHONY: test-ops
test-ops: ## ops/config/migration tests only
	$(RUN) pytest tests/unit/test_ops_*.py -q

.PHONY: dashboards
dashboards: ## regenerate deploy/grafana/dashboards/*.json
	$(RUN) python deploy/grafana/build_dashboards.py

# ─────────────────────────── local stack (docker compose dev override) ───────────────────────────
.PHONY: up
up: ## build + start the full stack locally (vault dev mode, dry-run publishing, Coupang sandbox)
	@[ -f .env ] || cp .env.example .env
	$(DEV_COMPOSE) up -d --build
	@echo "api http://localhost:8000  console http://localhost:3000  grafana http://localhost:3001  temporal-ui http://localhost:8088"

.PHONY: down
down: ## stop the local stack (volumes kept; `make down V=1` also drops them)
	$(DEV_COMPOSE) down $(if $(V),--volumes,)

.PHONY: ps
ps: ## local stack status
	$(DEV_COMPOSE) ps

.PHONY: logs
logs: ## follow logs (S=service)
	$(DEV_COMPOSE) logs -f --tail=200 $(S)

.PHONY: vault-dev-bootstrap
vault-dev-bootstrap: ## exercise deploy/vault/bootstrap.sh against the dev Vault
	$(DEV_COMPOSE) exec -e VAULT_TOKEN=dev-root-token vault sh /vault/acvm/bootstrap.sh init

# ─────────────────────────── application commands ───────────────────────────
.PHONY: migrate
migrate: ## alembic upgrade head against ACVM_DATABASE_URL (from .env)
	$(DOTENV) $(RUN) acvm db upgrade

.PHONY: migrate-sql
migrate-sql: ## print the PostgreSQL DDL (offline) for review
	$(RUN) python -m acvm.db.migrations --url postgresql+asyncpg://acvm@localhost/acvm sql

.PHONY: seed
seed: ## load seed data
	$(DOTENV) $(RUN) acvm seed

.PHONY: demo
demo: ## end-to-end demo (dry-run)
	$(DOTENV) $(RUN) acvm demo

.PHONY: console
console: ## Next.js console dev server (http://localhost:3000)
	cd console && npm run dev

# ─────────────────────────── images & production helpers ───────────────────────────
.PHONY: images
images: ## build api/worker/render images locally
	for t in api worker render; do \
	  docker buildx build -f deploy/docker/Dockerfile --target $$t \
	    --build-arg VERSION=$(VERSION) --build-arg VCS_REF=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown) \
	    -t $(REGISTRY)/acvm-$$t:$(VERSION) --load . ; \
	done

.PHONY: config-check
config-check: ## validate compose files statically (no daemon needed)
	$(DEV_COMPOSE) config -q
	docker compose --project-directory $(COMPOSE_DIR) --env-file $(COMPOSE_DIR)/dev.env \
	  -f $(COMPOSE_DIR)/docker-compose.yml config -q --no-env-resolution
	$(RUN) pytest tests/unit/test_ops_configs.py -q

.PHONY: prod-migrate
prod-migrate: ## (server) run the one-shot migration container
	$(PROD_COMPOSE) run --rm acvm-migrate

.PHONY: backup
backup: ## (server) run all backups now
	deploy/backup/backup.sh all

.PHONY: restore-drill
restore-drill: ## (server) restore the latest backup into a throw-away container and verify RPO/RTO
	deploy/backup/restore.sh drill

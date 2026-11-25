# XTM One – shortcuts for the dev stack (docker-compose.yml + docker-compose.dev.yml)
#
#   make up                       build and run the stack in the foreground
#   make logs s=platform          follow one service's logs
#   make up PROFILES="tools ha"   enable optional profiles
#   make up ENGINE=docker         force the container engine
#
# ENGINE defaults to docker when it is on the PATH, podman otherwise.
# ARGS is appended to the command of every target (compose, pytest, Playwright).

ifeq ($(origin ENGINE),undefined)
ENGINE := $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
endif

COMPOSE_FLAGS = -f docker-compose.yml -f docker-compose.dev.yml \
	$(foreach p,$(PROFILES),--profile $(p))
COMPOSE = $(ENGINE) compose $(COMPOSE_FLAGS)

# Service(s) a target acts on; empty means every service, except where noted.
s ?=

.DEFAULT_GOAL := help

.PHONY: help ports up start stop down restart build pull logs ps sh psql redis-cli config compose reset test e2e migrate edition mark-ce license setup-hooks

help: ## List the targets
	@echo "Engine: $(ENGINE)"
	@awk 'BEGIN {FS = ":.*## "} /^[a-z0-9-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

ports: ## Check that the host ports the stack publishes are free
	@scripts/dev-ports.sh $(ENGINE) "$(s)" $(COMPOSE_FLAGS)

up: ports ## Build and run the stack in the foreground
	$(COMPOSE) up --build $(ARGS) $(s)

start: ports ## Build and run the stack in the background
	$(COMPOSE) up -d --build $(ARGS) $(s)

stop: ## Stop the containers, keeping them
	$(COMPOSE) stop $(ARGS) $(s)

down: ## Remove the containers and network, keeping the volumes
	$(COMPOSE) down $(ARGS)

restart: ## Restart the containers
	$(COMPOSE) restart $(ARGS) $(s)

build: ## Build the images
	$(COMPOSE) build $(ARGS) $(s)

pull: ## Pull the images
	$(COMPOSE) pull $(ARGS) $(s)

logs: ## Follow the logs (default: platform worker frontend)
	$(COMPOSE) logs -f $(ARGS) $(or $(s),platform worker frontend)

ps: ## List the containers
	$(COMPOSE) ps $(ARGS)

sh: ## Open a shell in a service (default: platform)
	$(COMPOSE) exec $(ARGS) $(or $(s),platform) sh

psql: ## Open a PostgreSQL shell
	$(COMPOSE) exec $(ARGS) postgres psql -U copilot copilot

redis-cli: ## Open a Redis shell
	$(COMPOSE) exec $(ARGS) redis redis-cli

config: ## Print the merged compose configuration
	$(COMPOSE) config $(ARGS)

compose: ## Run any compose command: make compose ARGS="top platform"
	$(COMPOSE) $(ARGS)

reset: ## Remove the containers AND the volumes (database, Redis, bucket)
	$(COMPOSE) down -v $(ARGS)

# The tests run on the host, as CI runs them: the backend from backend/.venv,
# synced with uv when pyproject.toml or uv.lock changes; Playwright from
# frontend/, against the running dev stack.
BACKEND_VENV := backend/.venv
E2E_BASE_URL ?= $(or $(shell sed -n 's/^E2E_BASE_URL=//p' frontend/.env.e2e 2>/dev/null | tr -d '"\r'),http://localhost:3100)

# The backend's uv project environment, synced to uv.lock with its dev group
# (pytest); uv takes Python from backend/.python-version. A backend/.venv that
# links to another checkout's venv is that checkout's: never synced from here.
$(BACKEND_VENV)/.synced: backend/pyproject.toml backend/uv.lock
	@if [ -L $(BACKEND_VENV) ]; then \
		echo "$(BACKEND_VENV) links to another checkout's venv: not syncing it to this checkout's uv.lock."; \
	else \
		cd backend && uv sync --locked && touch .venv/.synced; \
	fi

frontend/node_modules/.yarn-state.yml: frontend/package.json frontend/yarn.lock
	cd frontend && corepack enable && yarn install --immutable && yarn playwright install chromium

test: $(BACKEND_VENV)/.synced ## Run the backend unit tests (ARGS="tests/test_x.py -k name")
	cd backend && SECRET_KEY="$${SECRET_KEY:-ci-test-secret-key-not-for-production}" \
		.venv/bin/python -m pytest -m "not integration" -n auto --dist loadgroup $(ARGS)

# The dev stack's database as the host reaches it: DATABASE_URL (environment,
# then .env), else the URL docker-compose.yml builds from DB_PASSWORD, on
# DEV_POSTGRES_PORT.
dotenv = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tr -d '"\r')
DEV_DATABASE_URL = $(or $(DATABASE_URL),$(call dotenv,DATABASE_URL),postgresql+asyncpg://copilot:$(or $(call dotenv,DB_PASSWORD),copilot_dev)@localhost:$(or $(call dotenv,DEV_POSTGRES_PORT),5432)/copilot)

migrate: $(BACKEND_VENV)/.synced ## Run Alembic (ARGS="upgrade head", ARGS="heads", ARGS="revision --autogenerate -m 'add my table'": harden the output, see migrations-safety.md)
	cd backend && DATABASE_URL="$(DEV_DATABASE_URL)" .venv/bin/python -m alembic -c alembic/alembic.ini $(ARGS)

# Enterprise Edition (dev-docs/reference/editions.md): which edition a file is,
# before it is written. One path per call; spaces are fine.
edition: $(BACKEND_VENV)/.synced ## Say which edition a file or directory is, written or not yet, fail on one to classify: make edition P="backend/app/x/y.py"
	@test -n "$(P)" || { echo 'Usage: make edition P="path/to/file"' >&2; exit 2; }
	@cd backend && .venv/bin/python scripts/license_headers.py --from "$(CURDIR)" --which "$(P)"

mark-ce: $(BACKEND_VENV)/.synced ## Decide a file is Community Edition: start it with the CE marker: make mark-ce P="backend/app/x/y.py"
	@test -n "$(P)" || { echo 'Usage: make mark-ce P="path/to/file"' >&2; exit 2; }
	@cd backend && .venv/bin/python scripts/license_headers.py --from "$(CURDIR)" --mark-ce "$(P)"

license: $(BACKEND_VENV)/.synced ## Tag the Enterprise Edition files with their license header (ARGS="--check" only lists)
	cd backend && .venv/bin/python scripts/license_headers.py $(or $(ARGS),--fix)

# core.hooksPath is shared by every worktree of the clone and replaces .git/hooks:
# an existing setting is never overwritten.
setup-hooks: ## Have git check each commit: Enterprise Edition license headers, no `security` scope (.githooks)
	@./scripts/setup-hooks.sh

e2e: frontend/node_modules/.yarn-state.yml ## Run the Playwright e2e tests against the dev stack (ARGS="tests_e2e/x.spec.ts")
	@curl -sf -o /dev/null $(E2E_BASE_URL)/api/health/ready || \
		{ echo "No dev stack answers at $(E2E_BASE_URL): start it with 'make start' (or set E2E_BASE_URL)." >&2; exit 1; }
	cd frontend && E2E_BASE_URL=$(E2E_BASE_URL) yarn playwright test --project=core $(ARGS)

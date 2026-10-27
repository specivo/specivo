.PHONY: configure up down logs status create-admin reset-password dev-up dev-down \
       test test-serial test-all test-unit test-integration test-service test-cov lint format \
       migrate migrate-gen migrate-merge seed \
       test-db-up test-db-down test-db-reset test-ci \
       test-e2e test-e2e-headed test-e2e-debug test-e2e-update-snapshots playwright-install \
       install sync lock help download-model backfill-embeddings reindex-fts import-redmine import-data \
       build bundle frontend-build frontend-watch \
       sso-idp-up sso-idp-down sso-idp-reset test-sso-idp test-e2e-sso

# Load environment from .env and .env.local (if they exist).
# .env.local overrides .env. Shell env overrides both.
-include .env
-include .env.local
export

# =============================================================================
# Specivo Makefile
# =============================================================================
#
# Quick start (first time):
#   make help              # List the documented targets
#   make configure         # Interactive setup wizard
#   make up                # Start all services
#   open http://localhost:9933/
#
# Development (build from source with hot-reload):
#   make dev-up            # Build + start with hot-reload (incl. frontend watcher)
#   make dev-down          # Stop dev services
#   make frontend-build    # One-off build of the frontend bundles (never committed)
#
# Testing:
#   make test-db-up        # Start test DB
#   make test              # Run tests (parallel via xdist, excludes serial)
#   make test-serial       # Run serial-only tests (rate limit, shared state)
#   make test-all          # Run both parallel + serial
#   make test-db-down      # Stop test DB
# =============================================================================

UV = uv
RUN = uv run
SPECIVO_PORT ?= 9933

# Compose command for the targets that run inside the api container
# (download-model, backfill-embeddings, reindex-fts, import-*). A source checkout
# has docker-compose.dev.yml and talks to the dev stack; a release bundle has
# only docker-compose.yml and talks to the production stack. PROD=1 uses the
# production stack in a checkout too.
ifeq ($(PROD)$(wildcard docker-compose.dev.yml),docker-compose.dev.yml)
APP_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.dev.yml
else
APP_COMPOSE = docker compose
endif

help:  ## List the documented targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Targets that run in the api container use: $(APP_COMPOSE)"
	@echo "(the dev stack in a checkout; pass PROD=1 for the production stack)."

# -----------------------------------------------------------------------------
# Setup & Lifecycle
# -----------------------------------------------------------------------------

configure:  ## Interactive setup wizard (writes .env and .env.local)
	python3 scripts/configure.py

up:  ## Start all services (production compose file)
	docker compose up -d

down:  ## Stop all services
	docker compose down

logs:  ## Follow the logs of all services
	docker compose logs -f

status:  ## Show the state of all services
	docker compose ps

# Development mode (build from source, hot-reload, direct port 8000).
# The overlay's `frontend` service rebuilds the esbuild bundles into
# ./specivo/static/dist on every change; api waits for its first build.
dev-up:  ## Build from source and start the dev stack with hot-reload (checkout only)
	docker compose -f docker-compose.yml -f docker-compose.dev.yml build api
	GIT_COMMIT=$$(git rev-parse --short HEAD) docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Include the dev overlay so its extra services (the frontend watcher) stop too.
dev-down:  ## Stop the dev stack (checkout only)
	docker compose -f docker-compose.yml -f docker-compose.dev.yml down

# -----------------------------------------------------------------------------
# Admin Management
# -----------------------------------------------------------------------------

create-admin:  ## Create an admin (login=... email=... password=...)
	@test -n "$(login)" || (echo "Usage: make create-admin login=admin email=admin@localhost password=secret" && exit 1)
	docker compose exec api python -m specivo.cli.admin create --login $(login) --email $(email) --password $(password)

reset-password:  ## Reset a user's password (login=... password=...)
	@test -n "$(login)" || (echo "Usage: make reset-password login=admin password=newpass" && exit 1)
	docker compose exec api python -m specivo.cli.admin reset-password --login $(login) --password $(password)

# -----------------------------------------------------------------------------
# Package management (uv)
# -----------------------------------------------------------------------------

install:
	$(UV) sync --all-extras

sync:
	$(UV) sync

lock:
	$(UV) lock

add:
	@echo "Usage: make add p=<package>"
	$(UV) add $(p)

add-dev:
	@echo "Usage: make add-dev p=<package>"
	$(UV) add --group dev $(p)

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------

test:
	$(RUN) pytest -m 'not serial and not e2e'

test-serial:
	$(RUN) pytest -m serial -n 0

test-all:
	$(RUN) pytest -m 'not serial and not e2e'
	$(RUN) pytest -m serial -n 0

test-unit:
	$(RUN) pytest -m unit

test-integration:
	$(RUN) pytest -m integration

test-service:
	$(RUN) pytest -m service

test-cov:
	$(RUN) pytest --cov=specivo --cov-report=html --cov-report=term

# E2E (Playwright) — requires test DB running (make test-db-up) and built
# frontend bundles, which every e2e target builds first.
test-e2e: frontend-build
	$(RUN) pytest tests/e2e/ -m e2e -n 0

test-e2e-headed: frontend-build
	$(RUN) pytest tests/e2e/ -m e2e -n 0 --headed --slowmo=300

test-e2e-debug: frontend-build
	PWDEBUG=1 $(RUN) pytest tests/e2e/ -m e2e -n 0 --headed

test-e2e-update-snapshots: frontend-build
	UPDATE_SNAPSHOTS=1 $(RUN) pytest tests/e2e/test_visual_regression.py -m e2e -n 0

playwright-install:
	$(RUN) playwright install chromium

# -----------------------------------------------------------------------------
# Frontend bundles (esbuild) — generated into specivo/static/dist/, never
# committed. The Docker image and CI build their own; `make dev-up` runs a
# watcher container. frontend-build uses host npm when available, otherwise a
# Node container (force the container with FRONTEND_BUILD=docker).
# -----------------------------------------------------------------------------

FRONTEND_NODE_IMAGE ?= node:22-slim

frontend-build:
	@if [ "$(FRONTEND_BUILD)" != "docker" ] && command -v npm >/dev/null 2>&1; then \
		cd frontend && npm ci --prefer-offline --no-audit --no-fund && npm run build; \
	else \
		docker run --rm \
			-v "$(CURDIR)/frontend:/src/frontend" \
			-v "$(CURDIR)/specivo/static/dist:/src/specivo/static/dist" \
			-v specivo-frontend-node-modules:/src/frontend/node_modules \
			-w /src/frontend $(FRONTEND_NODE_IMAGE) \
			sh -c "npm ci --prefer-offline --no-audit --no-fund && npm run build"; \
	fi

# Host-Node alternative to the dev watcher container (do not run both at once).
frontend-watch:
	cd frontend && npm ci --prefer-offline --no-audit --no-fund && npm run watch

# -----------------------------------------------------------------------------
# Linting & Formatting
# -----------------------------------------------------------------------------

lint:
	$(RUN) ruff check specivo/ tests/
	$(RUN) mypy specivo/

format:
	$(RUN) ruff format specivo/ tests/
	$(RUN) ruff check --fix specivo/ tests/

# -----------------------------------------------------------------------------
# Database (reads DATABASE_URL, SECRET_KEY, REDIS_URL from env)
# -----------------------------------------------------------------------------

migrate:
	$(RUN) alembic upgrade head

migrate-gen:
	@echo "Usage: make migrate-gen m='migration message'"
	$(RUN) alembic revision --autogenerate -m "$(m)"

migrate-merge:
	$(RUN) alembic merge heads -m "merge_heads"

seed:
	$(RUN) python -m specivo.cli.seed

# -----------------------------------------------------------------------------
# Test infrastructure
# -----------------------------------------------------------------------------

test-db-up:
	docker compose -f docker-compose.test.yml up -d --wait

test-db-down:
	docker compose -f docker-compose.test.yml down

# Reset test DB: destroy and recreate (clears all leftover data)
test-db-reset:
	docker compose -f docker-compose.test.yml down -v
	docker compose -f docker-compose.test.yml up -d --wait
	$(RUN) alembic upgrade head

# CI: start test DB, run tests, stop DB
test-ci:
	docker compose -f docker-compose.test.yml up -d --wait
	$(RUN) pytest -m 'not e2e' || (docker compose -f docker-compose.test.yml down; exit 1)
	docker compose -f docker-compose.test.yml down

# -----------------------------------------------------------------------------
# i18n / Translations
# -----------------------------------------------------------------------------

messages-extract:
	$(RUN) pybabel extract -F babel.cfg -k _l -k gettext_lazy -o specivo/locale/specivo.pot .

messages-update:
	$(RUN) pybabel update -i specivo/locale/specivo.pot -d specivo/locale -D specivo

messages-compile:
	$(RUN) pybabel compile -d specivo/locale -D specivo

# -----------------------------------------------------------------------------
# Build & Package
# -----------------------------------------------------------------------------

build:
	docker build -t specivo/specivo:dev .

bundle:
	@VERSION=$$(cat VERSION 2>/dev/null || echo "dev"); \
	echo "Building image..."; \
	docker build -t specivo/specivo:$$VERSION .; \
	echo "Saving image to tar..."; \
	docker save specivo/specivo:$$VERSION -o specivo-image.tar; \
	echo "Creating bundle..."; \
	mkdir -p _bundle/nginx _bundle/scripts _bundle/docs; \
	cp specivo-image.tar _bundle/; \
	cp docker-compose.yml _bundle/; \
	cp .env.example _bundle/; \
	cp .env.local.example _bundle/ 2>/dev/null || true; \
	cp Makefile _bundle/; \
	cp -r nginx/* _bundle/nginx/; \
	cp scripts/configure.py _bundle/scripts/; \
	cp scripts/entrypoint.sh _bundle/scripts/; \
	cp scripts/download-model.sh _bundle/scripts/; \
	cp docs/INSTALL.md _bundle/docs/ 2>/dev/null || true; \
	echo "$$VERSION" > _bundle/VERSION; \
	tar czf specivo-$$VERSION-bundle.tar.gz -C _bundle .; \
	rm -rf _bundle specivo-image.tar; \
	echo "Bundle created: specivo-$$VERSION-bundle.tar.gz"

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

# These run inside the running api container (see APP_COMPOSE above), so they
# work from a release bundle as well as from a checkout.

download-model:  ## Download the embedding model (~487 MB, checksum-verified) into the data dir (ARGS=--force)
	$(APP_COMPOSE) exec api python -m specivo.cli.download_model $(ARGS)

backfill-embeddings:  ## Rebuild search chunks and embed them (ARGS='--reembed --require-model')
	$(APP_COMPOSE) exec api python -m specivo.cli.backfill_embeddings $(ARGS)

reindex-fts:  ## Rebuild FTS vectors (optionally PROJECT=KEY) after a language change
	$(APP_COMPOSE) exec api python -m specivo.cli.reindex_fts $(if $(PROJECT),--project $(PROJECT),)

import-redmine:  ## Import a Redmine instance (ARGS='--source-db-url ... --dry-run'; see --help)
	$(APP_COMPOSE) exec api python -m specivo.cli.import_data --source redmine $(ARGS)

import-data:  ## Import from any registered source (SOURCE=name ARGS='...'; SOURCE=name ARGS=--help for its options)
	$(APP_COMPOSE) exec api python -m specivo.cli.import_data $(if $(SOURCE),--source $(SOURCE),--list-sources) $(ARGS)

# --- Redmine test fixture (importer development only) ----------------------
# PROFILE=pg (default) or PROFILE=mysql — the importer supports both databases
# Redmine runs on, so both are testable.
REDMINE_PROFILE ?= $(if $(PROFILE),$(PROFILE),pg)
REDMINE_APP := $(if $(filter mysql,$(REDMINE_PROFILE)),redmine-my,redmine-pg)
REDMINE_COMPOSE := docker compose -f docker-compose.redmine.yml --profile $(REDMINE_PROFILE)

redmine-fixture-up:  ## Start the Redmine 7.0.1 fixture (PROFILE=pg|mysql; checkout only)
	@mkdir -p .redmine-fixture/files-pg .redmine-fixture/files-mysql
	$(REDMINE_COMPOSE) up -d --wait

redmine-fixture-seed:  ## Fill the fixture with importer test content (PROFILE=pg|mysql; checkout only)
	$(REDMINE_COMPOSE) exec $(REDMINE_APP) bundle exec rails runner /seed/seed.rb

redmine-fixture-down:  ## Stop the fixture and discard its data (PROFILE=pg|mysql; checkout only)
	$(REDMINE_COMPOSE) down -v

redmine-fixture-reset: redmine-fixture-down redmine-fixture-up redmine-fixture-seed  ## Recreate and reseed the fixture (checkout only)

# --- SSO test IdP (SSO integration/E2E tests only) ---------------------------
# A disposable Keycloak. All state is
# in RAM and every credential is a fixed test value (docker-compose.sso-test.yml).
SSO_IDP_COMPOSE := docker compose -f docker-compose.sso-test.yml

sso-idp-up:  ## Start the disposable Keycloak test IdP on :8033 (checkout only)
	$(SSO_IDP_COMPOSE) up -d --wait

sso-idp-down:  ## Stop the test IdP and discard its data (checkout only)
	$(SSO_IDP_COMPOSE) down -v

sso-idp-reset: sso-idp-down sso-idp-up  ## Recreate the Keycloak test IdP from its realm seed (checkout only)

test-sso-idp:  ## Run the integration tests against the test IdP (make sso-idp-up; checkout only)
	$(RUN) pytest -m 'sso_idp and not e2e' -n 0

# Keycloak calls the E2E server's back-channel logout endpoint from its
# container (host.docker.internal:9944). Docker Desktop forwards that to the
# host's loopback; on Linux it arrives on the bridge, so the server must listen
# on every interface there. Override with E2E_SERVER_BIND=...
SSO_E2E_BIND := $(if $(filter Linux,$(shell uname -s)),0.0.0.0,127.0.0.1)

test-e2e-sso: frontend-build sso-idp-up  ## Start the test IdP, run the SSO browser tests (checkout only)
	E2E_SERVER_BIND=$${E2E_SERVER_BIND:-$(SSO_E2E_BIND)} $(RUN) pytest tests/e2e/test_sso_login.py -m 'e2e and sso_idp' -n 0

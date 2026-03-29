.PHONY: configure up down logs status create-admin reset-password dev-up dev-down \
       test test-serial test-all test-unit test-integration test-service test-cov lint format \
       migrate migrate-gen migrate-merge seed \
       test-db-up test-db-down test-ci \
       install sync lock download-model \
       build bundle

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
#   make configure         # Interactive setup wizard
#   make up                # Start all services
#   open http://localhost:9933/
#
# Development (build from source with hot-reload):
#   make dev-up            # Build + start with hot-reload
#   make dev-down          # Stop dev services
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

# -----------------------------------------------------------------------------
# Setup & Lifecycle
# -----------------------------------------------------------------------------

configure:
	python3 scripts/configure.py

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

status:
	docker compose ps

# Development mode (build from source, hot-reload, direct port 8000)
dev-up:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

dev-down:
	docker compose down

# -----------------------------------------------------------------------------
# Admin Management
# -----------------------------------------------------------------------------

create-admin:
	@test -n "$(login)" || (echo "Usage: make create-admin login=admin email=admin@localhost password=secret" && exit 1)
	docker compose exec api python -m specivo.cli.admin create --login $(login) --email $(email) --password $(password)

reset-password:
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
	$(RUN) pytest -m 'not serial'

test-serial:
	$(RUN) pytest -m serial -n 0

test-all:
	$(RUN) pytest -m 'not serial'
	$(RUN) pytest -m serial -n 0

test-unit:
	$(RUN) pytest -m unit

test-integration:
	$(RUN) pytest -m integration

test-service:
	$(RUN) pytest -m service

test-cov:
	$(RUN) pytest --cov=specivo --cov-report=html --cov-report=term

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

# CI: start test DB, run tests, stop DB
test-ci:
	docker compose -f docker-compose.test.yml up -d --wait
	$(RUN) pytest || (docker compose -f docker-compose.test.yml down; exit 1)
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
	cp docs/INSTALL.md _bundle/docs/ 2>/dev/null || true; \
	echo "$$VERSION" > _bundle/VERSION; \
	tar czf specivo-$$VERSION-bundle.tar.gz -C _bundle .; \
	rm -rf _bundle specivo-image.tar; \
	echo "Bundle created: specivo-$$VERSION-bundle.tar.gz"

# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

download-model:  ## Download bundled e5-small embedding model (~393 MB)
	bash scripts/download-model.sh specivo/static/models

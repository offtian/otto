# Otto — task runner
# Run `just` to see all available recipes.
# Works from any subdirectory.

set fallback  # search parent directories for this justfile
set dotenv-load  # load .env automatically

# Setup
# -----

# Install dependencies (creates .venv + uv.lock)
install:
    uv sync
    just verify-install

# Verify the virtualenv can import otto
verify-install:
    .venv/bin/python -c "import otto; print(otto.__file__)"

# Update lockfile
lock:
    uv lock

# Development
# -----------

# Run the application
run:
    uv run python -m otto

# Docker Compose
# --------------

# Start core dev infrastructure (Postgres + Jaeger)
infra:
    docker compose up -d

# Start the full dev stack (+ LLM gateway, Confluence MCP, containerized app)
stack:
    docker compose --profile gateway --profile mcp --profile app up -d --build

# Expose the app publicly for Slack and tail the URL
tunnel:
    docker compose --profile tunnel up -d
    docker compose logs -f tunnel

# Stop and remove all dev services
infra-down:
    docker compose down

# Testing
# -------

# Run all tests
test *ARGS:
    uv run pytest tests/ -x -vv {{ ARGS }}

# Run unit tests
test-unit *ARGS:
    uv run pytest tests/unit/ -x -vv {{ ARGS }}

# Run integration tests
test-integration *ARGS:
    uv run pytest tests/integration/ -x -vv {{ ARGS }}

# Run functional / end-to-end tests
test-functional *ARGS:
    uv run pytest tests/functional/ -x -vv {{ ARGS }}

# Run tests with coverage report
test-coverage:
    uv run pytest tests/ --cov=otto --cov-report=term-missing --cov-report=html

# Code Quality
# ------------

# Run all linters (ruff + mypy + import-linter)
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/
    uv run mypy src/
    uv run lint-imports

# Auto-fix lint issues and format
fmt:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/

# MyPy type-check
typecheck:
    uv run mypy src/

# Import-linter check
check-imports:
    uv run lint-imports

# Database
# --------

# Run pending migrations
run-db-migrations:
    uv run python -m alembic -c src/otto/data/alembic.ini upgrade head

# Generate a new migration
build-migration MESSAGE:
    uv run python -m alembic -c src/otto/data/alembic.ini revision --autogenerate -m "{{ MESSAGE }}"

# Roll back one migration
downgrade-db-migration:
    uv run python -m alembic -c src/otto/data/alembic.ini downgrade -1

# Housekeeping
# ------------

# Remove caches and build artifacts
clean:
    find src/ tests/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    rm -rf .mypy_cache .pytest_cache .ruff_cache .import_linter_cache htmlcov .coverage dist *.egg-info

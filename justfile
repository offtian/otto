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
    # macOS: uv installs .pth files with the UF_HIDDEN flag set, and
    # CPython 3.13 skips hidden .pth files — clear it or imports break.
    chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true
    .venv/bin/python -c "import otto; print(otto.__file__)"

# Update lockfile
lock:
    uv lock

# Development
# -----------

# Run the application
run:
    # PYTHONPATH: the editable .pth is unreliable on macOS (UF_HIDDEN + py3.13)
    PYTHONPATH=src uv run python -m otto

# Docker Compose
# --------------

# Start core dev infrastructure (Postgres + Grafana LGTM)
infra:
    docker compose up -d

# Start the full dev stack (+ LLM gateway, Confluence + SailPoint MCP, Langfuse, app)
stack:
    docker compose --profile gateway --profile mcp --profile sailpoint --profile langfuse --profile app up -d --build

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

# Run integration tests (needs `just infra` Postgres + migrations applied)
test-integration *ARGS:
    RUN_INTEGRATION=1 uv run pytest tests/integration/ -x -vv {{ ARGS }}

# Run functional / end-to-end tests
test-functional *ARGS:
    uv run pytest tests/functional/ -x -vv {{ ARGS }}

# Run tests with coverage report
test-coverage:
    uv run pytest tests/ --cov=otto --cov-report=term-missing --cov-report=html

# Run golden-case evals against the configured LLM (skipped in `just test`)
eval *ARGS:
    RUN_EVALS=1 uv run pytest tests/evals/ -vv {{ ARGS }}

# Code Quality
# ------------

# Run all linters (ruff + mypy + import-linter)
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/
    uv run mypy src/
    # PYTHONPATH: the editable .pth is unreliable on macOS (UF_HIDDEN + py3.13)
    PYTHONPATH=src uv run lint-imports

# Auto-fix lint issues and format
fmt:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/

# MyPy type-check
typecheck:
    uv run mypy src/

# Import-linter check
check-imports:
    PYTHONPATH=src uv run lint-imports

# Database
# --------

# Run pending migrations
run-db-migrations:
    # PYTHONPATH: the editable .pth is unreliable on macOS (UF_HIDDEN + py3.13),
    # and alembic's env.py imports otto — without this it fails ModuleNotFoundError.
    PYTHONPATH=src uv run python -m alembic -c src/otto/data/alembic.ini upgrade head

# Generate a new migration
build-migration MESSAGE:
    PYTHONPATH=src uv run python -m alembic -c src/otto/data/alembic.ini revision --autogenerate -m "{{ MESSAGE }}"

# Roll back one migration
downgrade-db-migration:
    PYTHONPATH=src uv run python -m alembic -c src/otto/data/alembic.ini downgrade -1

# Print the approval audit report from the durable store (2.7)
audit-report:
    PYTHONPATH=src uv run python -m otto.interfaces.audit_report

# Silence Otto at runtime (C1 kill switch — takes effect within the cache TTL, no restart)
otto-off:
    docker compose exec -T postgres psql -U postgres -d otto -c "INSERT INTO runtime_flags (name, value) VALUES ('otto_enabled', 'false') ON CONFLICT (name) DO UPDATE SET value = 'false'"

# Re-enable Otto's replies at runtime
otto-on:
    docker compose exec -T postgres psql -U postgres -d otto -c "INSERT INTO runtime_flags (name, value) VALUES ('otto_enabled', 'true') ON CONFLICT (name) DO UPDATE SET value = 'true'"

# Purge dev-chat sessions idle longer than DAYS days (C3 — session PII);
# chats with a pending approval are spared, messages cascade with the session.
purge-chats DAYS="30":
    docker compose exec -T postgres psql -U postgres -d otto -c "DELETE FROM agent_sessions WHERE updated_at < now() - interval '{{ DAYS }} days' AND pending_state IS NULL"

# Fake payloads (local trace testing)
# -----------------------------------

app_url := "http://localhost:8000"

# POST one fixtures/jira/ ticket at the running app (fresh id each fire, so it
# never trips the redelivery dedup). Requires JIRA_WEBHOOK_SECRET set in .env.
jira-fire FILE:
    #!/usr/bin/env bash
    set -euo pipefail
    body="$(sed "s/@@ID@@/test-$(uuidgen)/g" fixtures/jira/{{FILE}})"
    curl -sS -X POST "{{app_url}}/jira/webhook?secret=${JIRA_WEBHOOK_SECRET:?set JIRA_WEBHOOK_SECRET in .env}" \
        -H 'content-type: application/json' --data-binary "$body" \
        -o /dev/null -w '{{FILE}} -> HTTP %{http_code}\n'

# Fire the whole set: knowledge question, access request, follow-up, resolution
jira-fire-all: (jira-fire "question-vpn.json") (jira-fire "access-snowflake.json") (jira-fire "comment-followup.json") (jira-fire "resolved.json")

# Prove the agent works end-to-end (model + Agents SDK + tracing), no HTTP.
# Usage: just agent-smoke  |  just agent-smoke "I need Snowflake reporting access"
agent-smoke *QUESTION:
    PYTHONPATH=src uv run python dev/smoke_agent.py {{ quote(QUESTION) }}

# Chat with the agent in the browser — mimics the Slack integration
# (same agent, telemetry, and HITL approval round-trip), no Slack needed.
chat:
    PYTHONPATH=src uv run streamlit run src/otto/interfaces/chat_app.py

# Housekeeping
# ------------

# Remove caches and build artifacts
clean:
    find src/ tests/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    rm -rf .mypy_cache .pytest_cache .ruff_cache .import_linter_cache htmlcov .coverage dist *.egg-info

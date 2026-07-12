# Otto

Otto — firmwide Slack-native tech-support agent: OpenAI Agents SDK, HITL approvals, MCP integrations (Confluence, SailPoint).

## Quick Start

```bash
# Install dependencies (uv creates .venv and uv.lock)
just install

# Copy and fill in environment variables
cp .env.example .env

# Install git hooks (ruff fix + format on commit)
uv run pre-commit install

# Run the application
just run
```

Commit `uv.lock` — CI installs with `uv sync --locked`.

## Documentation

| Doc | What it is |
| --- | --- |
| [docs/PRD.md](docs/PRD.md) | Product requirements & phased delivery plan (living) |
| [docs/implementation-plan.md](docs/implementation-plan.md) | Staged implementation plan with approval gates & acceptance criteria (living) |
| [docs/decision-log.md](docs/decision-log.md) | Dated record of audit findings and direction-setting decisions (append-only) |

## Development

```bash
just test            # all tests
just test-unit       # unit tests only
just lint            # ruff + mypy + import-linter
just fmt             # auto-fix + format

just run-db-migrations           # alembic upgrade head
just build-migration "message"   # autogenerate a migration
```

The data layer follows the sentinel split: `data/models.py` (SQLModel) is the
schema source of truth consumed by alembic autogenerate; runtime queries go
through the [`databases`](https://www.encode.io/databases/) singleton in
`data/db.py` (`connect_db()` / `disconnect_db()` at entry points).

## Project Structure

```
src/otto/
├── main.py          # Entry point (python -m otto)
├── settings.py      # Env values (pydantic-settings, module-level singleton)
├── config.py        # Composition root — wires adapters from settings (get_config())
├── interfaces/      # API routes, CLI commands, webhooks — no business logic
├── application/     # Use-case orchestration
├── evals/           # Evaluation harnesses (LLM evals, quality gates)
├── domain/          # Pure business logic — never reads config/settings
├── vendors/         # External SDK wrappers (Slack, PagerDuty, ...)
├── data/            # models.py (SQLModel) + alembic migrations + databases-lib singleton
└── utils/           # Cross-cutting helpers (structlog setup, ...)
```

Layer boundaries are enforced by [import-linter](https://import-linter.readthedocs.io/)
contracts in `pyproject.toml` — a module may import anything below it in the
stack, never above. Linting is [ruff](https://docs.astral.sh/ruff/) with
`select = ["ALL"]` and a documented ignore list, plus strict
[mypy](https://mypy.readthedocs.io/).

## Configuration

All configuration via environment variables (see [.env.example](.env.example)).

| Variable       | Description                                       | Default                                                    |
| -------------- | ------------------------------------------------- | ---------------------------------------------------------- |
| `DEBUG`        | Enable debug behaviour                            | `false`                                                    |
| `LOG_LEVEL`    | `DEBUG` / `INFO` / `WARNING` / `ERROR`            | `INFO`                                                     |
| `DATABASE_URL` | SQLAlchemy-flavoured Postgres URL (`+asyncpg`)    | `postgresql+asyncpg://localhost:5432/otto` |

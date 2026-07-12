# CLAUDE.md

See @AGENTS.md for coding conventions, patterns, and testing rules.

## Essential Commands

```bash
just install         # Install dependencies with uv
just test            # Run all tests
just test-unit       # Run unit tests only
just lint            # Ruff + mypy + import-linter
just fmt             # Auto-fix + format with ruff
just run-db-migrations           # Alembic upgrade head
just build-migration "message"   # Autogenerate a migration
```

Run a single test file with `uv run pytest tests/unit/path/test_file.py -x -vv`.

## Architecture

- src layout; layered architecture enforced by import-linter contracts in `pyproject.toml`.
  Layers top → bottom (a module may import anything below it, never above):
  `main` → `interfaces` → `application` → `evals` → `config` → `domain` → `vendors` → `data` → `utils` → `settings`
- `settings.py` — env *values* (pydantic-settings; module-level `settings` singleton, imported directly — the one sanctioned direct-object import)
- `config.py` — composition root: wires *objects* (adapters, clients) from settings; access via the cached `get_config()` singleton
- `domain/` never imports `config` or `settings` — pass values in as parameters (enforced by import-linter)
- structlog exclusively — stdlib `logging` is forbidden by import-linter; only `utils/logs.py` may touch it. Use `logs.log_event(...)` / `logs.log_exception(...)`
- Data layer: `data/models.py` (SQLModel, schema source of truth) + alembic migrations; runtime queries go through the `databases` lib singleton in `data/db.py` (`get_db()`, `connect_db()`/`disconnect_db()` at entry points)

## Testing

- `tests/unit/` mirrors `src/` structure — isolated, no DB/network
- `tests/integration/` — tests needing real infrastructure (DB, queues)
- `tests/functional/` — end-to-end, named after the use-case; only patch third-party calls
- Every test method uses full-sentence `# Given / # When / # Then` comments (see `.claude/rules/testing.md`)

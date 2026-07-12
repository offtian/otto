# AGENTS.md

Coding conventions for AI agents working in this repository.

**Rules are loaded automatically** from `.claude/rules/` based on file paths:

- `python.md` — imports (module-level only, import modules not objects), attrs, naming, structlog, error handling
- `application.md` — layered architecture, exception handling, system clock, keyword args
- `testing.md` — GWT comments (mandatory), test structure, variable naming, time handling
- `git.md` — commit messages, PR conventions

## Quick Reference

### Import Pattern (most common violation)

```python
# WRONG
from otto.domain.orders.entities import Order

# CORRECT
from otto.domain.orders import entities as order_entities
order = order_entities.Order(...)
```

The one sanctioned exception is the settings singleton:

```python
from otto.settings import settings
```

### Settings vs Config

- `settings.py` owns configuration **values** (env vars). Add fields to `Settings`.
- `config.py` owns the **objects** built from those values (adapters, clients).
  Wire them into `Configuration` and read them via `config.get_config()` from
  the application/interfaces layers only.

### Logging

- `structlog` exclusively — stdlib `logging` forbidden (enforced by import-linter)
- `logs.log_event("event_name", params={...})` for events
- `logs.log_exception(exc, params={...})` for errors

### Layer Boundaries

Enforced by import-linter (`uv run lint-imports`, part of `just lint`):
new top-level modules under `src/otto/` must be added to the
`layers` contract in `pyproject.toml` — the contract is exhaustive on purpose.

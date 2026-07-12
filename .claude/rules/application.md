---
paths:
  - "src/**/*.py"
---

# Application & Architecture Conventions

## Layered Architecture
- `interfaces/` — routes, pipelines, agents, CLI commands, webhooks. Translate interface events into domain calls. NO business logic here.
- `application/` — orchestrate use-case journeys. Single entry point per use-case.
- `domain/` — reusable business logic, agnostic of which use-case calls it.
- `data/` — persistence models only. Models should be thin with no business logic.

## Domain Layer Structure
- Package by domain category: `domain/$category/`
- Read-only: `queries.py` (or `queries/` package)
- Write: `operations.py` (or `operations/` package)
- For queries: categorise by the type of object returned
- For operations: categorise by the primary object being operated on

## Application Layer Structure
- Public functions MUST use keyword-only args: `def do_thing(*, foo, bar):`
- Public functions MUST have docstrings with params, return types, and raisable exceptions
- Prefix private modules with `_` (e.g. `_trigger.py`, `_documents.py`)

## Exception Handling
- Distinguish anticipated vs unanticipated exceptions
- Anticipated: handle gracefully, don't log to Sentry
- Unanticipated: log to Sentry with `logger.exception("descriptive message")`
- NEVER format exception message into the log message — pass data as logger args
- Exception classes must be importable from the same module as the function that raises them
- Prefer defining exceptions in the module where they're raised, avoid shared `exceptions.py`

## System Clock
- Minimise `now()`/`today()` calls in domain layer
- Compute dates at interface layer, pass them in as explicit parameters
- Never use `def fn(*, base_date=None):` defaulting to today — require the caller to pass it

## Keyword Arguments
- Always use kwargs when calling functions where positional args aren't obvious
- Always use keyword-only args (`*`) for public domain/application functions

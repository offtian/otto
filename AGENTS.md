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

Imports go at module top, without exception.

A class field must not share its name with a module its annotation references
(`specialists: tuple[specialists.Profile, ...] = ()` fails at import — the
value is assigned before the annotation is evaluated). Alias the import.

### Settings vs Config

- `settings.py` owns configuration **values** (env vars). Add fields to `Settings`.
- `config.py` owns the **objects** built from those values (adapters, clients).
  Wire them into `Configuration` and read them via `config.get_config()` from
  the application/interfaces layers only.

### Ports

Every seam the application layer talks through is a `Protocol` in the domain
module of its concept — `Inbox`, `Outbox`, `FollowUpStore`, `MemoryStore`,
`RepoHost`, `ChangeReviewer`, and so on.

- The contract lives on the port: what each method returns and what it raises.
- Implementations sit beside it in `domain/` (the Postgres stores) or in
  `vendors/`, which satisfies a port structurally and never imports domain.
- Type `Configuration` fields and callers against the port, never the
  implementation.
- A module's docstring states its contract in plain sentences and lists its
  row states (`pending` / `sent` / `dead`).

### Logging

- `structlog` exclusively — stdlib `logging` forbidden (enforced by import-linter)
- `logs.log_event("event_name", params={...})` for events
- `logs.log_exception(exc, params={...})` for errors
- Never log what derives from a conversation — request text, memory passages,
  recall keywords, tool output. Log ids and counts (NFR2).

### Layer Boundaries

Enforced by import-linter (`uv run lint-imports`, part of `just lint`):
new top-level modules under `src/otto/` must be added to the
`layers` contract in `pyproject.toml` — the contract is exhaustive on purpose.

## How a Request Moves

```
router → dispatch.accept → dispatch.handle_safely → flows.SUPPORT → delivery.deliver
         (durable inbox)                            (classify →     (outbox)
                                                     access | general)
```

- **A run's reply leaves through `delivery.deliver(key=...)`**, never straight
  through `cfg.slack` or `cfg.jira`. The key allows one reply per key, ever: the
  request id for an answer or its apology; `approval:<id>`, `pending:<id>` and
  `expired:<id>` for approval outcomes. Courtesy notes (`delivery.send`) and
  approval cards are the only exceptions.
- **A reply is Markdown.** Agents write Markdown and `delivery.send` renders
  it for the channel (`utils/markup`), escaping it for Slack. What Otto posts
  itself through `cfg.slack` — cards, notes under them — is already mrkdwn.
- **A request can arrive later.** A due follow-up and a pull request event
  (`POST /github/webhook`) are turned into a `SupportRequest` on the original
  conversation and enter through `dispatch.accept` like any other — on behalf
  of the original requester, with a note saying nobody wrote it.
- **One run per conversation at a time.** `handle_safely` serializes them; do
  not start a run on a conversation any other way.
- **The always-on stores are assists, not gates.** Inbox, outbox, follow-ups
  and memory fail open: log the error and carry on without them.
- **Memory holds case notes, never transcripts.** The only way in is
  consolidation on a confirmed resolution: the conversation is distilled into
  a de-identified note, or confirms one memory already holds. Notes are found
  by keyword (Postgres full-text search) and, when an embedding model is named,
  also by meaning (pgvector) — an assist to keywords that fails open to them —
  and reach an agent, through the retrieval gate or `search_memory`, framed as
  untrusted. The one record that names a person is `requester_cases` (requester
  id, note id, timestamp): off by default, kept `MEMORY_HISTORY_DAYS`, never
  any text.
- **Anything that makes Otto speak on its own** — reply retries, follow-ups,
  startup replay — checks the kill switch first.
- **Approval-gated tools mount only on top-level agents.** Specialists and the
  reviewer are read-only: a pause inside a nested run can never be approved.

## Extending Otto

- **A specialist or a repository** is an entry in `otto.yaml` (two flat lists),
  mounted on the general agent by `catalog.mount`. No new agent, no routing,
  no orchestration code.
- **A runbook** is a Markdown file in `runbooks/`. Its file name and the
  topic before the dash in its first heading are what a request is matched
  against (`domain/support/runbooks`) to hint the agent, so call it what a
  requester would.
- **A new write** is a function tool with `needs_approval=True`. An MCP tool is
  gated unless `policy.UNGATED_TOOLS` clears it with a sign-off reference.
- **A repository change** goes through `domain/support/changes`: exact-match
  edits, the path allowlist, `stage_proposal`. Nothing writes to a repository
  except the gated `open_pull_request` and `update_pull_request`, and what the
  approval card shows is what gets applied. A pull request is revised only if
  it carries this conversation's signed return address (`find_revisable`) —
  never a branch Otto did not create.
- **A new model role** (classifier, distiller, reviewer, embedder) is a setting
  naming a model on the same gateway — no vendor-specific SDK.

## Database

- `data/models.py` is the schema source of truth. Migrations are hand-written
  and mirror it; render one with
  `alembic -c src/otto/data/alembic.ini upgrade <from>:<to> --sql`.
- Cast interval parameters explicitly — `sa.func.now() + sa.cast(delta, sa.Interval)`.
  Untyped, Postgres reads `now() - $1` as timestamp minus timestamp.
- Integration tests (`RUN_INTEGRATION=1`) need Postgres: `just infra`,
  `just run-db-migrations`, `just test-integration`.
- The database must offer the `vector` extension from migration 0017 on (the
  compose image is `pgvector/pgvector`). A vector travels as text and is cast
  in SQL (`memory._as_vector`): no pgvector package, no driver codec.

# Local Setup & Testing Guide

Everything here runs on your machine with zero firm dependencies (D7): stub
tools stand in for Confluence/SailPoint until you opt into their compose
profiles, and Ollama stands in for the LLM gateway.

## 1. Prerequisites

- **uv** (`brew install uv`) and **Docker Desktop**
- **Ollama** with the configured model: `ollama pull qwen3.6` (see
  `LLM_MODEL` in `.env` — any OpenAI-compatible endpoint works; point
  `LLM_BASE_URL` at LiteLLM or a real gateway to swap)

## 2. Setup

```bash
just install                 # deps + .venv (commit uv.lock)
cp .env.example .env         # fill in what you use; empty MCP URLs = stubs
uv run pre-commit install    # ruff fix + format on commit
just infra                   # Postgres + Grafana LGTM (traces)
just run-db-migrations       # alembic upgrade head
```

`users.yaml` is the dev identity directory: it maps one person's Slack and
Jira ids, carries their team for approval cards, and may carry an approver
`role`. It ships with **Sam Support** (`role: support_user`) so the HITL demo
works untouched. Approver roles can also come from `SUPPORT_USER_IDS` /
`ADMIN_USER_IDS` in `.env`.

Ports after `just infra`: Postgres `5432`, Grafana `http://localhost:3001`,
OTLP `http://localhost:4318` (set as `OTLP_ENDPOINT` to export traces).
Optional profiles via `just stack`: LiteLLM `4000`, Confluence MCP `9000`,
SailPoint mock `9100`, Langfuse `http://localhost:3000`, the app itself
`8000`.

## 3. Fastest way to see it work

```bash
just agent-smoke                      # one agent run, no HTTP: model + tools + tracing
just chat                             # browser dev-chat — the full HITL demo (below)
just run                              # the FastAPI service on :8000
just tunnel                           # expose :8000 publicly for a dev Slack app
```

### The HITL demo (`just chat`)

The dev chat is a **harness + demo vehicle, not a product channel** (D21) —
but its approvals are the real thing (D1): the paused run lands in the
production approval store and the buttons call `resolve_approval`, so the
role check, self-approval block, exactly-once guard, and audit rows all run
exactly as they do for Slack.

1. Ask the "I need access to…" example (or type an access request).
2. Otto gathers system/entitlement/justification, then pauses — a real
   `PendingApproval` row now exists; refresh the browser, it survives.
3. In the approval card, pick **U_STREAMLIT — the requester** and click
   Approve → politely rejected, and `audit_events` gains an
   `unauthorized_role` row. That rejection is the product's core guarantee.
4. Pick **Sam Support (support_user)** → Approve → the gated tool executes,
   the outcome and card close-out land in the chat, `executed_at` is stamped.
5. `just audit-report` shows the decision, the rejected attempt, per-approver
   counts, and turnaround latencies.

## 4. Test tiers

```bash
just test               # unit + functional + (skipped) integration/evals
just test-unit          # mirrors src/, no DB or network
just test-functional    # end-to-end use-cases; only the model and Slack faked
just test-integration   # real Postgres (needs `just infra` + migrations)
just eval               # live golden cases against the configured LLM
just lint               # ruff + mypy + import-linter (layer contracts)
```

What the important suites prove:

- `tests/functional/test_access_request_approval.py` — the HITL invariants:
  pause/resume round-trip (NFR6), exactly-once resolve, fail-closed
  cross-channel identity (B1), crash recovery (B2), every-interruption cards
  (B3), argument-aware dedup (B4), durable attempt audit (B5), and
  injection-cannot-bypass-the-gate (B6, code-side).
- `tests/evals/test_golden_cases.py` — model behaviour (n=10, growing to the
  ≥20 NFR3 gate), including the injection case against a live model.
- `tests/integration/` — the Postgres stores' contracts, including the
  concurrent-resolve zero-double-write test.

## 5. Drills & ops tools

**Kill-switch drill (C1)** — silences Otto at runtime, no restart:

```bash
just otto-off      # runtime_flags row wins over OTTO_ENABLED within
                   # KILL_SWITCH_CACHE_SECONDS (default 10s)
just jira-fire question-vpn.json    # acked, but no reply
just otto-on
```

**Jira webhook drill** — `just run` first, set `JIRA_WEBHOOK_SECRET` (and the
`JIRA_BASE_URL`/`JIRA_USER_EMAIL`/`JIRA_API_TOKEN` trio — the webhook rejects
events while the ticket channel is unconfigured):

```bash
just jira-fire access-snowflake.json   # ticket-origin access request
just jira-fire-all                     # question, access, follow-up, resolved
```

A burst beyond `JIRA_EVENTS_PER_MINUTE` (default 30) is acked, logged as
`jira_rate_limited`, and dropped before it costs an LLM call (C2).

**Escalation team routing (D25)** — optional; empty settings keep the single
triage channel:

```bash
SUPPORT_TEAMS=Network,Identity & Access,Endpoint
TEAM_TRIAGE_CHANNELS=Network:C_NET,Identity & Access:C_IAM
```

An LLM stand-in for the firm classifier assigns each escalation to a team and
posts it to that team's channel; anything unclassified, unmapped, or errored
falls back to the default channel — routing never blocks an escalation.

**Housekeeping:**

```bash
just audit-report     # decisions, rejected attempts, per-approver stats
just purge-chats 30   # drop dev-chat sessions idle >30 days (pending ones spared)
```

Before bumping `openai-agents`, drain pending approvals first — see
[`drain-approvals.md`](drain-approvals.md) (NFR6: a serialized RunState may
not survive an SDK format change).

## 6. Traces

Every run exports through `utils/telemetry.py`: set `OTLP_ENDPOINT` for the
LGTM stack (Grafana → Tempo at `localhost:3001`), `LOGFIRE_TOKEN` for the
Logfire UI, and/or the three `LANGFUSE_*` values (self-hosted via
`--profile langfuse`, dev keys in `compose.yml`). Each conversation is one
trace — turns and approvals parent into the chat's root span, across
restarts. The `otto.thinking_steps` span attribute (tool outputs) exports in
dev only (C3).

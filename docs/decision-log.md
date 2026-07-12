# Decision Log

Running record of direction-setting decisions: what was decided, why, and what
remains open. Newest entries first. The living documents — [PRD](PRD.md) and
[implementation plan](implementation-plan.md) — reflect the *current* state;
this log preserves *how we got there*.

---

## 2026-07-12 — Eval-gate location (D5 closed)

| ID | Decision | Status | Notes |
|---|---|---|---|
| D5 | **Two-tier eval gating.** The always-on required check is **deterministic record/replay (scripted-model) tests** of the agent loop — tool selection, HITL pause/resume — in the normal pytest job (no network, $0, gates orchestration regressions). The **LLM-judged golden evals** (NFR3's ≥90%-at-n≥20) run **on demand**: locally via `just eval` (against Ollama or a key) and as a `workflow_dispatch` CI job that reads an `LLM_API_KEY` repo secret — **not** a per-PR required check. | Closed | Chosen over the plan's per-PR OpenAI-key gate because the prototype now runs local Ollama (CI can't reach it) and D7 makes quality targets directional. Upgrade path if cloud gating is wanted later: flip the `eval` job to run on `pull_request` with path filters + concurrency caps. Deterministic tests carry orchestration regressions regardless. |

## 2026-07-12 — Interface restructure: app + routers + Pydantic schemas

| ID | Decision | Status | Notes |
|---|---|---|---|
| D16 | **Split `interfaces/api.py` into `app.py` (assembly: lifespan, dedup state, `include_router`) + `routers/{slack,jira,health}.py` (one sub-router per channel/concern).** Routes stay thin: verify transport (signature/secret) + dedup, then schedule a background use-case. | Applied | Requested for readability as the interface grew. `main.py` + `just run` target `otto.interfaces.app:app`. |
| D17 | **Pydantic schemas validate every inbound payload at the boundary** (`interfaces/schemas.py`: Slack events, Slack interactions, Jira webhooks). Schemas model only used fields (`extra="ignore"`, all-optional, lenient `parse` → None on malformed), and carry the mapping to channel-neutral domain shapes (`.to_support_request()`, `.resolution_key()`, `.to_*()`). | Applied | Keeps the D-rule boundary: **application never sees transport JSON** — only mapped domain objects. `pydantic` added as a direct dep. camelCase wire fields (`accountId`, etc.) get an N815 per-file-ignore. |
| D18 | **Background dispatch + its input DTOs live in `application/dispatch.py`**, not the interface. The FR8 "run-safely, apologize-at-origin" wrappers are use-case orchestration, so they belong below the interface; the interface parses payloads into these inputs and schedules them. | Applied | `interfaces` → `application` only (import-linter clean). The interaction DTOs (`ApprovalDecision`/`FeedbackVote`/`ResolveClick`) are use-case inputs, so they moved down with the wrappers; schemas map into them. |

## 2026-07-12 — Resolution signals: native D6 + Slack Q&A (T2 closed)

Wiring D6's resolution metric (plan step 1.3). The decision-free signals
landed first, then T2 for the Slack side.

| ID | Decision | Status | Notes |
|---|---|---|---|
| — | **Native D6 signals emit a single `request_resolved` log event** with a `signal` discriminator: `ticket_status` (a Jira `issue_updated` transition into the fixed `done` status category, deduped on changelog id, recorded even under the kill switch since it is pure telemetry) and `access_granted` (the approve path — a SailPoint submission). | Applied | `interfaces/api.py` `_resolution_from_update` + `application/support.py`. No open decision — D6 names both. Ticket-status uses `statusCategory.key == "done"` (workspace-agnostic), not status-name matching. |
| T2 | **Slack knowledge-Q&A signal = a "Did this help?" Yes/No vote on the answer**, not the ✅ reaction. Delegated to Claude's UX read. **Yes** → `request_resolved{signal: helpful_vote}`; **No** → escalate to the triage channel (`TicketBackend`) + `feedback_negative`, no resolution. | Closed | Chosen over the reaction for discoverability (nobody reacts ✅ to a bot untrained) and a failure path (reaction is positive-only, so a wrong answer left the user stuck and un-measured — the A5 failure). Ceiling (`ponytail:` in `_post_answer`): the vote trails every non-paused Slack answer, so it also rides mid-runbook steps — upgrade is an agent-flagged terminal answer. Vote is not gated to the requester (DMs are single-user; directional at prototype scale, D7). |

## 2026-07-12 — Ticket channel landed + requester identity

Two follow-ups after the walking skeleton, same day:

| ID | Decision | Status | Notes |
|---|---|---|---|
| D11 | **Closed** — live Jira intake is implemented in Phase 0 after all (the plan's 0.2/0.4/0.5 Jira halves): `vendors/jira.py`, `/jira/webhook` (secret = HMAC `X-Hub-Signature` *or* `?secret=` URL token, fail-closed), own-actor loop filter via a lazily cached `myself` lookup (fail-closed: unknown bot identity ⇒ events dropped, no loop possible), ticket-history reconstruction, comment replies. | Superseded | The deferral note stands as history; only the 0.1 dev accounts (T7/T9) and the 0.11 demo remain external. |
| D15 | **Requester identity is a first-class domain concept**: `domain/identity` defines `User` (name, team, Slack user id, Jira account id) and a `UserDirectory` loaded from `users.yaml` (path in settings; missing file = empty directory). Used for: **cross-channel self-approval exclusion** (T3 — a ticket requester cannot approve their own request via their Slack id), requester **name + team on approval cards** (the approver is the access-control decider; this is their context), and requester team in the **agent input** so it can sanity-check access requests against the team. | Confirmed | Surfaced by the D8 two-channel reality: requester ids were channel-local (Slack id vs Jira accountId), so the T3 guard silently did not hold for ticket-origin requests. **Roles stay in settings (D3)** — the directory carries identity + team only; step 2.3 merges both into Postgres. **Entitlement policy stays SailPoint's job** — the directory is a lookup, not a shadow identity system. Unmapped ids degrade to today's raw-id behavior. |

## 2026-07-12 — Phase 0 implementation (ponytail pass)

The walking skeleton landed after a lazy-review of the PRD/plan. Scope cuts
taken during implementation, each reversible and none touching a locked
decision's substance:

| ID | Decision | Status | Notes |
|---|---|---|---|
| D11 | **Live Jira intake defers to Phase 1**; D8's structural half — the channel-neutral origin (`SlackThread \| TicketRef` on `SupportRequest`/`PendingApproval`) — is implemented now, before shapes bake. | Confirmed | Halves Phase 0's external surface (one dev account, not two). `/jira/webhook`, `vendors/jira.py`, and ticket-history reconstruction become the first Phase 1 steps; `_post_reply`/`_conversation_input` carry explicit `TicketRef` seams. |
| D12 | **Phase 0 evals are 3 plain pytest golden cases** (`tests/evals/`, env-guarded, `just eval`); the YAML runner + LLM judge wait until the set approaches n=20 and D5 is decided. | Confirmed | NFR3's own text says n=3 is a smoke test. No machinery ahead of the gate it serves. |
| D13 | **Agent definition lives in `domain/support/agent.py`** (instructions, tools, `SupportContext`, `build_agent`); `application/` holds only use-cases (`handle_support_request`, `resolve_approval`, `notify_failure`). | Confirmed | `interfaces/agents/` was considered and rejected: `resolve_approval` must rebuild the agent to deserialize `RunState`, and application cannot import interfaces. Domain takes model/MCP servers as parameters — never reads config (contract-enforced). |
| D14 | **Background execution stays FastAPI `BackgroundTasks`** — no task queue (hand-rolled or otherwise) in the prototype. | Confirmed | The in-memory approval store requires same-process resume anyway; a queue becomes *possible* only after Phase 2's Postgres store and *necessary* only at multi-replica (Phase 3). The swap point is one `background.add_task` call site. Reach for arq/Celery then, never build one. |
| T3 | **Self-approval prohibited** — implemented as the recommended default (`_may_resolve` in `application/support.py`). | Applied | One-line flip if the open decision lands the other way. |

**Verified against `openai-agents` 0.18.2 during implementation:** on resume,
the context must be supplied via `RunState.from_string(..., context_override=...)`.
Passing `context=` to `Runner.run` replaces the state's context wrapper — which
is where `approve()`/`reject()` decisions live — and the run silently
re-interrupts forever. The functional suite pins this behavior.

## 2026-07-12 — Inbound ticket channel (scoping follow-up)

A gap surfaced after the v0.2 docs landed: Jira/ServiceNow were modeled only as
an **outbound** escalation target (`TicketBackend`), but employees also *submit*
tickets — ticketing is an **inbound entry point** alongside Slack. Folded into
PRD v0.3 and the implementation plan.

| ID | Decision | Status | Notes |
|---|---|---|---|
| D8 | **Inbound ticket intake is a first-class MVP entry point** (Phase 0), alongside Slack mentions/DMs. | Confirmed | Requires channel-neutral domain shapes: `SupportRequest`/`PendingApproval` carried Slack-only fields (`channel_id`, `thread_ts`) despite claiming interface-agnosticism — fixed in Phase 0 before the shape bakes into the audited `ApprovalRecord` table. |
| D9 | **Jira first** for the prototype; ServiceNow adapter at graduation behind the same channel seam. | Confirmed | The `mcp-atlassian` image in `compose.yml` already speaks Jira; a free Jira Cloud instance covers dev. |
| D10 | **Ticket write posture: comments safe, transitions per policy.** Otto comments on tickets freely as its reply channel (kill-switch covered); status transitions (resolve/close/assign) go through the per-tool sensitivity policy, **default-gated** until explicitly classified. | Confirmed | A ticket comment is a write to a system of record — unlike a Slack message — so the posture had to be explicit rather than inherited from the Slack framing. |

Derived requirements: ticket comment history is the D2 analog (reconstructed
per event, capped, untrusted); Otto's own webhook events must be filtered by
actor to prevent reply loops; ticket resolved/closed status becomes a **native
D6 resolution signal** — the D6-gap (third signal) now applies to the Slack
knowledge-Q&A path only.

## 2026-07-12 — Blind-spot audit + scoping interview

A structured blind-spot audit was run over the repo and PRD v0.1, followed by
an interview resolving the direction-defining questions it surfaced. PRD v0.2
and `docs/implementation-plan.md` fold these outcomes in.

### Audit findings (condensed)

| # | Severity | Finding | Outcome |
|---|---|---|---|
| A1 | High | The "read-only, safe" Confluence path is not read-only: `vendors/mcp.py` mounts the MCP server with no tool filter and no `require_approval`, and the dev image (`mcp-atlassian`) ships write tools. Violates "no autonomous writes without HITL" by default. | **Open — early MVP task.** Enforce read-only (tool allowlist / read-only mode) + a test asserting the mounted toolset has no write tools. |
| A2 | High | FR3 (interactive runbook walkthroughs) requires multi-turn context, which v0.1 excluded from MVP — internal contradiction. | **Resolved by D2** (thread-history reconstruction). |
| A3 | High | Critical path is organizational (Slack app approval, gateway credentials, MCP endpoints) and the plan priced it at zero. | **Dissolved by D7** (prototype status). Kept as graduation notes. |
| A4 | High | "Events API → FastAPI" silently assumed inbound HTTPS would be permitted. | **Resolved by D1** (confirmed acceptable). |
| A5 | High | Deflection metric measured *answering*, not *resolving* — a wrong answer the user gives up on counted as deflected. | **Partially resolved by D6**; third signal for knowledge Q&A still open. |
| A6 | Medium | Logfire SaaS receives chat content in spans (`instrument_openai_agents()` default); vendor approval / data residency was never asked. | **Resolved by D4** for the prototype. PII/trace scrubbing before any real-data pilot remains a task. |
| A7 | Medium | NFR3's eval gate had no enforcement point: CI is GitHub-hosted with no LLM access. | **Open — decision D5** (options identified, default recommended). |
| A8 | Medium | No failure path: Slack event ack'd, background task can die silently — user gets no reply, nobody is alerted, no kill switch. | **Open — MVP requirement added** (catch-all error reply + alerting). |
| A9 | Medium | `PendingApproval.run_state_json` holds the full conversation: PII at rest with no retention policy once persisted, format coupled to a fast-moving 0.x SDK (an `openai-agents` bump can strand pending approvals), no expiry. | **Open — scoped to the durable-store phase.** Pin the SDK; drain pending approvals before upgrades. |
| — | Deferrable | 90% eval threshold is meaningless at n=3 golden cases (meaningful at ≥20); escalation link hard-codes `slack.com/archives` (Enterprise Grid); PRD claimed `ApprovalRecord` was "already modeled" (only `ExampleRecord` exists). | Thresholds noted in PRD v0.2; doc-drift fixed in v0.2; Grid link is a graduation note. |

### Interview answers → decisions

| ID | Decision | Status | Notes |
|---|---|---|---|
| D1 | Slack **Events API → FastAPI** transport stands; inbound HTTPS acceptable (cloudflared tunnel in dev). | Confirmed | Retires A4. |
| D2 | **FR3 stays in MVP.** Statelessness solved by **thread-history reconstruction**: rebuild agent input from `conversations.replies` per event. History length capped; thread content treated as untrusted (prompt-injection surface). | Confirmed | Retires A2; adds two derived requirements. |
| D3 | **Approver authorization moves into MVP** as a user-role model: only `support_user` or `admin` may approve/deny. MVP storage: Slack user ID lists in settings; Postgres roles table later. | Confirmed | Pulls part of v0.1's Phase 2 forward. |
| D4 | **Logfire + OTLP dual sink** approved for the prototype. | Confirmed | Retires A6's vendor question; scrubbing task stays. |
| D5 | **Where gated evals run.** Recommended default: GitHub Actions eval job with `OPENAI_API_KEY` secret, path-filtered triggers + `workflow_dispatch`, required check; plus deterministic record/replay tests in the normal pytest job. Alternatives: committed fingerprinted eval report verified hermetically in CI; scheduled/label-triggered non-blocking runs. | **Open** | Needs a pick before the eval-harness phase. |
| D6 | **Resolved =** successful SailPoint submission **or** a support agent explicitly marks resolved. | Confirmed, with a gap | Knowledge-Q&A answers count as nothing under this definition. Proposed third signal (requester ✅ reaction / "did this help?" button) **open**. |
| D7 | This is a **personal prototype / idea formulation**, not (yet) a firm deliverable. Firm-perimeter items are graduation notes. | Confirmed | Retires A3; reframes calendar estimates as sequence. |
| — | **Self-approval exclusion** — requester may not approve their own request even with `support_user`/`admin` role. | **Open** (recommended: yes) | Two lines now, awkward retrofit after audit-trail semantics exist. |

### Verified technical claims

Checked against the installed `openai-agents` 0.18.2 source, so the HITL
design is not resting on documentation folklore:

- `MCPServerStreamableHttp(require_approval="always")` is a real parameter.
- `RunState` (`agents/run_state.py`) supports JSON pause/resume.
- Run results expose `interruptions` (`ToolApprovalItem`) for the approve/deny loop.

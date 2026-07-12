# Otto — Firmwide Tech-Support Agent

**PRD & Phased Delivery Plan** · Draft v0.1 · 2026-07-12 · Owner: Ollie Tian

---

## 1. Problem & Vision

Tech-support requests today land in Slack channels, DMs, and tickets, and are
answered by humans repeating the same lookups: knowledge-base articles, access
requests, runbook walk-throughs, and triage/routing. Otto is a **Slack-native,
API-based support agent** that resolves the repetitive tier of this work
autonomously, escalates the rest with full context, and never executes a
sensitive action without a human decision.

**One-liner:** *@otto is the first responder for all firm tech support.*

## 2. Users

| Persona | Interaction | Needs |
|---|---|---|
| Employee | @otto in channels, DMs | Fast answers, guided fixes, access requests without portal-diving |
| Support engineer | Triage channel | Clean escalations with context; approve/deny sensitive actions in one click |
| Support lead | Dashboards | Deflection metrics, eval scores, audit trail of approvals |
| Platform/security | Config & audit | HITL guarantees, trace/PII controls, least-privilege integrations |

## 3. Goals / Non-Goals

**Goals (product):**

1. Answer support questions from the firm knowledge base (Confluence) with citations.
2. Walk users through runbooks for common issues (VPN, password, device).
3. Automate access requests (SailPoint) — **always** gated by human approval.
4. Escalate anything unresolved to humans with a structured summary.
5. Every sensitive action is human-approved, logged, and traceable end-to-end.

**Goals (platform):**

- OpenAI Agents SDK core; models via the internal LLM gateway (OpenAI-compatible).
- API-first: Slack is the first client of a FastAPI service, not the architecture.
- Integrations via MCP so new systems are config, not code.
- OTel-native observability (Logfire UI + OTLP to firm collector).
- Offline eval harness with golden datasets gating releases.

**Non-goals (for now):**

- Not an HR/facilities/general chatbot — tech support only.
- No autonomous writes to any system of record without HITL.
- No fine-tuning / custom models; prompt + tools + evals only.
- No web UI (the API leaves room for one later).
- No multi-workspace / Enterprise Grid rollout until Phase 3.

## 4. Locked Decisions (from scoping interview)

| Decision | Choice | Rationale |
|---|---|---|
| Framework | OpenAI Agents SDK (Python) | Native tools/handoffs/HITL (`needs_approval`, `RunState` pause-resume) |
| Slack transport | Events API → FastAPI | API-first; same service can serve other clients |
| HITL model | Approve **sensitive tools only** | Safe reads flow freely; SailPoint/writes pause for Block Kit approve/deny |
| Model access | Internal LLM gateway | OpenAI-compatible `base_url`; Chat Completions transport |
| Ticketing (MVP) | Slack triage channel | `TicketBackend` protocol; ServiceNow/Jira adapters later, zero agent changes |
| Observability | Logfire SDK + OTLP export | Logfire UI for agent traces **and** vendor-neutral OTLP to firm collector |
| Evals | Offline golden-set harness | YAML cases + LLM-judge on the gateway; CI-runnable, no SaaS dependency |
| Name | **Otto** | @otto — “auto(mation)”, friendly persona |

## 5. Product Requirements (MVP)

### Functional

- **FR1 — Entry points:** Otto responds to `@otto` mentions in channels and to DMs; replies stay in-thread.
- **FR2 — Knowledge Q&A:** Otto searches Confluence (MCP) before answering; answers cite the source page; if nothing is found it says so and offers escalation. *(Stub tool when MCP unset.)*
- **FR3 — Runbooks:** Otto lists/reads markdown runbooks and walks the user through steps interactively.
- **FR4 — Access requests:** Otto collects system, entitlement, and business justification, then submits via SailPoint (MCP). The run **pauses**; an approve/deny card is posted to the triage channel; the run resumes only after a decision. *(Stub tool when MCP unset — same approval flow.)*
- **FR5 — Escalation:** When Otto can't resolve (or the user asks for a human), it posts a structured escalation (subject, summary, urgency, requester, thread link) to the triage channel and tells the user.
- **FR6 — HITL resume:** Approve → the paused tool executes and Otto reports the outcome in-thread. Deny → Otto explains the denial gracefully. The card is updated with resolver + outcome.

### Non-functional

- **NFR1:** Slack events ack < 3s (background task execution; Slack retry dedup).
- **NFR2:** All agent runs, tool calls, and LLM generations traced (Logfire + OTLP); no chat text in logs at INFO.
- **NFR3:** Golden-set eval pass rate ≥ 90% before any prompt/tool change merges.
- **NFR4:** Secrets only via env; Slack request-signature verification on every endpoint.
- **NFR5:** Layered architecture (import-linter enforced); new integrations = vendors adapter + config wiring only.

### Out of MVP (explicitly)

Durable approval store (in-memory MVP; Postgres slot ready), approver
authorization groups, multi-turn session memory, streaming replies,
ServiceNow/Jira, specialist sub-agents, online evals.

## 6. Architecture

```
 Slack (Events API + Interactivity)
      │ HTTPS (signed)
      ▼
 FastAPI  /slack/events · /slack/interactions · /healthz     [interfaces]
      │ background task
      ▼
 handle_support_request / resolve_approval                   [application]
      │
      ▼
 Otto (Agents SDK): instructions + tools                     [application/agents]
      ├─ search_knowledge ──► Confluence MCP (safe)
      ├─ request_access ────► SailPoint MCP (require_approval=always)
      ├─ list/read_runbook ─► runbooks/*.md (safe)
      └─ escalate_to_human ─► TicketBackend ► Slack triage channel
      │
      ├─ interruptions? ► PendingApproval(RunState JSON) ► approval card
      │                                                     ▲ approve/deny
      └─ final answer ► Slack thread                        resumes run

 LLM: internal gateway (OpenAI-compatible)                   [vendors/llm]
 Traces: Logfire SDK ─► Logfire UI + OTLP collector          [utils/telemetry]
 Evals: golden YAML + LLM judge (`just eval`)                [evals]
```

**HITL sequence:** sensitive tool call → `RunState` serialized to the approval
store → Block Kit card in triage channel → human clicks → state deserialized,
`approve()`/`reject()` → run resumes → outcome posted in the requester's thread.

**Layering** (import-linter enforced): `main → interfaces → application →
evals → config → domain → vendors → data → utils → settings`.

## 7. Phased Plan

### Phase 0 — Walking skeleton *(this week; in progress)*

Everything runs end-to-end **with zero external dependencies** (stub tools, in-memory store).

- [x] Repo from cookiecutter (uv, ruff ALL, mypy strict, import-linter, justfile)
- [ ] FastAPI app: `/slack/events` (signature check, challenge, dedup, <3s ack), `/slack/interactions`, `/healthz`
- [ ] Otto agent: instructions + 4 capability tools (knowledge/access stubs, runbooks, escalate)
- [ ] HITL loop: `needs_approval` → pause → approval card → resume (in-memory store)
- [ ] Telemetry: Logfire + optional OTLP exporter; `instrument_openai_agents()`
- [ ] Eval harness: 3 golden cases + LLM judge, `just eval`
- [ ] `compose.dev.yml` (Postgres + Jaeger), sample runbooks, `.env.example`
- **Exit:** `just lint && just test` green; DM → stubbed answer → approval card → resume works against a dev Slack workspace.

### Phase 1 — Knowledge MVP pilot *(weeks 1–3)*

Real value, lowest risk: read-only knowledge + human escalation.

- Confluence MCP connected (real KB, citations enforced in instructions)
- Runbooks populated from top-10 support macros
- Deployed behind firm ingress; Slack app installed in pilot channels
- Logfire + firm OTLP collector live; golden set grown to ≥ 20 cases; weekly eval run in CI
- **Exit criteria:** ≥ 30% of pilot-channel questions answered without human touch; eval pass ≥ 90%; support team signs off on escalation quality.

### Phase 2 — Access automation + durable HITL *(weeks 4–7)*

The approval flow earns its keep.

- SailPoint MCP live with `require_approval="always"`
- Postgres approval store (`ApprovalRecord` table already modeled) — approvals survive restarts; full audit trail (who approved what, when)
- Approver authorization: only members of a configured group can click approve/deny
- Approval expiry + reminders; per-tool sensitivity policy in config
- **Exit criteria:** first 50 access requests processed with zero unapproved writes; audit report generated from the store.

### Phase 3 — Ticketing, specialists & scale *(weeks 8+)*

- ServiceNow **or** Jira adapter behind `TicketBackend` (config swap)
- Specialist agents via Agents SDK handoffs (knowledge / access / hardware) once single-agent instructions get crowded
- Multi-turn session memory per Slack thread (Agents SDK sessions + Postgres)
- Streaming/progressive Slack updates; online evals sampled from traces; rate limiting; Enterprise Grid rollout
- **Exit criteria:** Otto is the default first responder in all support channels.

## 8. Success Metrics

| Metric | Target (Phase 1) | Source |
|---|---|---|
| Deflection rate (resolved w/o human) | ≥ 30% | traces + escalation count |
| Time-to-first-response | < 15s | OTel spans |
| Eval pass rate | ≥ 90% | `just eval` in CI |
| Approval turnaround (Phase 2) | < 30min median | approval store timestamps |
| Unapproved sensitive writes | **0, always** | audit log |

## 9. Risks & Open Questions

| # | Risk / question | Mitigation / owner |
|---|---|---|
| 1 | **Who may approve?** Any triage-channel member (MVP) vs. entitlement-checked group | Phase 2 requirement; needs security sign-off |
| 2 | PII/chat content in traces | Logfire scrubbing + OTel attribute filtering before Phase 1 prod |
| 3 | Gateway rate limits / model quality on gateway-routed models | Load-test in Phase 1; eval set doubles as regression net |
| 4 | Confluence MCP server availability (firm-hosted vs Atlassian remote) | Decide during Phase 1 setup; stub keeps dev unblocked |
| 5 | In-memory approvals lost on restart (MVP) | Accepted for pilot; Phase 2 makes durable |
| 6 | Slack retry storms / duplicate events | ts-based dedup now; Redis dedup if multi-replica later |
| 7 | Prompt injection via Confluence page content | Instructions hardening + read-only MVP; revisit before Phase 2 writes |

## 10. Delivery checklist for sign-off

- [ ] PRD reviewed & phases agreed (this doc)
- [ ] Phase 0 skeleton merged
- [ ] Dev Slack workspace + app manifest created
- [ ] Gateway credentials issued for Otto service account
- [ ] Confluence/SailPoint MCP endpoints identified (Phase 1/2)

# Otto — Firmwide Tech-Support Agent

**PRD & Phased Delivery Plan** · Draft v0.3 · 2026-07-12 · Owner: Ollie Tian

> **v0.3** adds the inbound ticket channel (D8–D10): employees who file Jira
> tickets get Otto as first responder too — comments as replies, transitions
> HITL-gated. **v0.2** folded in the blind-spot audit and scoping interview.
> See [`docs/decision-log.md`](decision-log.md) for what changed and why;
> step-level execution detail lives in
> [`docs/implementation-plan.md`](implementation-plan.md).

---

## 0. Project Status

Otto is currently a **personal prototype for idea formulation**, not (yet) a
firm deliverable (decision D7). Everything inside the repo perimeter — code,
compose stack, stubs, evals — proceeds on that basis. Items that need the firm
(Slack app approval, gateway credentials, MCP endpoints, security sign-offs,
ingress) are tracked as **graduation notes** (§10), and phase estimates are a
**sequence**, not a calendar.

## 1. Problem & Vision

Tech-support requests today land in Slack channels, DMs, and tickets, and are
answered by humans repeating the same lookups: knowledge-base articles, access
requests, runbook walk-throughs, and triage/routing. Otto is an **API-based
support agent** that meets employees where the request arrives — Slack mentions
and DMs, and tickets filed in Jira (ServiceNow at graduation) — resolves the
repetitive tier autonomously, escalates the rest with full context, and never
executes a sensitive action without a human decision.

**One-liner:** *@otto is the first responder for all firm tech support — in
Slack and in the ticket queue.*

## 2. Users

| Persona | Interaction | Needs |
|---|---|---|
| Employee | @otto in channels, DMs; tickets filed in Jira | Fast answers, guided fixes, access requests without portal-diving — in whichever channel they already use |
| Support engineer | Triage channel | Clean escalations with context; approve/deny sensitive actions in one click |
| Support lead | Dashboards | Resolution metrics, eval scores, audit trail of approvals |
| Platform/security | Config & audit | HITL guarantees, trace/PII controls, least-privilege integrations |

## 3. Goals / Non-Goals

**Goals (product):**

1. Answer support questions from the firm knowledge base (Confluence) with citations — over a **verified read-only** toolset.
2. Walk users through runbooks for common issues (VPN, password, device), with multi-turn context rebuilt from the conversation (Slack thread or ticket comment history).
3. Automate access requests (SailPoint) — **always** gated by human approval, and only role-authorized humans may approve.
4. Work tickets employees file in Jira: reply as comments; ticket status transitions only through the HITL sensitivity policy (D10).
5. Escalate anything unresolved to humans with a structured summary.
6. Every sensitive action is human-approved, logged, and traceable end-to-end. Every failure is visible — no silent drops.

**Goals (platform):**

- OpenAI Agents SDK core; models via an internal LLM gateway (OpenAI-compatible, Chat Completions transport).
- API-first: Slack and Jira are the first two clients of a FastAPI service, not the architecture.
- Integrations via MCP so new systems are config, not code.
- OTel-native observability (Logfire UI + OTLP to any collector — dual sink approved for the prototype, D4).
- Offline eval harness with golden datasets gating releases.

**Non-goals (for now):**

- Not an HR/facilities/general chatbot — tech support only.
- No autonomous writes to any system of record without HITL (ticket **comments** are the sanctioned reply channel per D10; **transitions** are policy-gated).
- No fine-tuning / custom models; prompt + tools + evals only.
- No web UI (the API leaves room for one later).
- No **persistent** session memory — multi-turn context comes from conversation reconstruction (D2), not a session store, until Phase 3.
- No ServiceNow until graduation (D9); no multi-workspace / Enterprise Grid rollout until Phase 3 (note: the current triage-thread link format hard-codes `slack.com/archives` and is Grid-incompatible — graduation note).

## 4. Decisions

### Locked (scoping interviews, 2026-07-12 — IDs from the decision log)

| Decision | Choice | Rationale |
|---|---|---|
| Framework | OpenAI Agents SDK (Python; 0.18.2 installed and verified — hard-pin `==` is a Phase 0 task per NFR6; pyproject currently declares `>=0.18.2`) | Native tools/handoffs/HITL — `require_approval`, `RunState` JSON pause-resume and `interruptions` verified against the installed SDK source |
| Slack transport (D1) | Events API → FastAPI; inbound HTTPS confirmed acceptable (cloudflared tunnel in dev) | API-first; same service can serve other clients |
| HITL model | Approve **sensitive tools only** | Safe reads flow freely once verified read-only (risk #1); SailPoint/writes pause for Block Kit approve/deny |
| Multi-turn context (D2) | **Conversation reconstruction** per event: Slack `conversations.replies` or Jira comment history; length capped; content treated as **untrusted** | Keeps FR3 in MVP without a session store; resolves the v0.1 contradiction |
| Approver authorization (D3) | **In MVP**: only users with `support_user` or `admin` role may approve/deny; MVP storage = Slack user ID lists in settings (fields to add); Postgres roles table later | Pulled forward from v0.1's Phase 2 |
| Model access | Internal LLM gateway | OpenAI-compatible `base_url`; Chat Completions transport (`vendors/llm.py`); SDK's own trace upload disabled — Logfire/OTel is the tracing path |
| Escalation (MVP) | Slack triage channel | `TicketBackend` protocol (`domain/support/escalation.py`) + `SlackTriageBackend` (`vendors/slack.py`); other backends plug in later, zero agent changes |
| Observability (D4) | Logfire SDK + OTLP dual sink, approved for the prototype | Logfire UI for agent traces **and** vendor-neutral OTLP; either, both, or neither sink per env (`utils/telemetry.py`) |
| “Resolved” definition (D6) | Successful SailPoint submission, a support agent explicitly marks resolved, **or the originating ticket reaches resolved/closed** | Deflection previously measured *answering*, not *resolving*; ticket status is a native signal |
| Project status (D7) | Personal prototype; firm items are graduation notes | Reframes estimates as sequence |
| Ticket intake (D8) | **Inbound tickets are a first-class MVP entry point** alongside Slack; domain shapes are channel-neutral | Employees already file tickets; the API-first service just gains a second client |
| Ticket system (D9) | **Jira first** for the prototype; ServiceNow adapter at graduation behind the same seam | `mcp-atlassian` in `compose.yml` already speaks Jira; free Jira Cloud instance for dev |
| Ticket write posture (D10) | **Comments safe, transitions per policy**: Otto's ticket comments are its reply channel (kill-switch covered); resolve/close/assign transitions go through the per-tool sensitivity policy, default-gated | A ticket comment is a write to a system of record — the posture must be explicit, not inherited from Slack semantics |
| Name | **Otto** | @otto — “auto(mation)”, friendly persona |

### Open (owner: Ollie; decide before the phase that needs them)

| ID | Question | Recommended default |
|---|---|---|
| D5 | Where the gated evals run (CI is GitHub-hosted with no LLM access) | GitHub Actions eval job with an API-key secret, path-filtered triggers + `workflow_dispatch`, required check; deterministic record/replay tests in the normal pytest job |
| D6-gap | Third resolution signal for **Slack** knowledge Q&A (tickets now have a native signal per D6/D8; Slack answers still count as nothing) | Requester ✅ reaction or a “did this help?” button |
| — | Self-approval — may a requester approve their own request even with the right role? | **Prohibit** (recommended; decision open — two lines now, awkward retrofit after audit-trail semantics exist) |

## 5. Product Requirements (MVP)

### Functional

- **FR1 — Entry points:** Otto responds to `@otto` mentions in channels, to DMs, **and to tickets filed in Jira** (webhook-driven). Replies stay in-thread (Slack) or land as ticket comments (Jira). Per event, context is rebuilt from the conversation — `conversations.replies` or the ticket's comment history — capped in length and treated as untrusted input (D2).
- **FR2 — Knowledge Q&A:** Otto searches Confluence (MCP) before answering; answers cite the source page; if nothing is found it says so and offers escalation. *(Stub tool when MCP unset.)* **Read-only must be enforced, not assumed** (audit A1): the Confluence server is currently mounted with no tool filter and the dev image ships write tools — an early MVP task adds a tool allowlist/read-only mode plus a test asserting the mounted toolset contains no write tools.
- **FR3 — Runbooks:** Otto lists/reads markdown runbooks (`settings.runbooks_dir`) and walks the user through steps interactively, carried across turns by FR1's conversation reconstruction.
- **FR4 — Access requests:** Otto collects system, entitlement, and business justification, then submits via SailPoint (MCP, `require_approval="always"` — already applied in `vendors/mcp.py`). The run **pauses**; an approve/deny card is posted to the triage channel; the run resumes only after a decision. *(Stub tool when MCP unset — same approval flow.)* Works from either entry point — the outcome is reported back to the originating thread or ticket.
- **FR5 — Escalation:** When Otto can't resolve (or the user asks for a human), it posts a structured escalation (subject, summary, urgency, requester, link to the thread or ticket) to the triage channel and tells the user in their channel.
- **FR6 — HITL resume:** Approve → the paused tool executes and Otto reports the outcome at the request's origin. Deny → Otto explains the denial gracefully. The card is updated with resolver + outcome. Concurrent clicks resolve exactly once (`ApprovalAlreadyResolved` guard, already in the domain store).
- **FR7 — Approver authorization (D3):** Only `support_user`/`admin` role holders may approve/deny; unauthorized clicks get a polite rejection, no state change, and an audit log event. Approval cards live in the Slack triage channel regardless of where the request originated. Self-approval: prohibited — recommended default, decision open (§4).
- **FR8 — Failure path (audit A8):** If the background handler dies after the intake ack, the user still gets an apologetic error reply at the origin (thread or ticket comment), and the failure is logged/alerted. No silent drops; a kill switch disables Otto's replies without undeploying.
- **FR9 — Ticket channel semantics (D8/D9/D10):** Jira intake via a secret-verified webhook endpoint; Otto's **own** webhook events are filtered by actor (no reply loops) and deduped. Otto's replies are ticket comments (safe per D10). Ticket status transitions are exposed only through the per-tool sensitivity policy — **default-gated** until explicitly classified. Ticket resolved/closed status feeds the D6 resolution metric.

### Non-functional

- **NFR1:** Intake ack fast on every channel — Slack events ack < 3s (background task execution; Slack retry dedup); Jira webhooks ack immediately with the same background dispatch.
- **NFR2:** All agent runs, tool calls, and LLM generations traced (Logfire + OTLP); no chat text in logs at INFO. Note: `logfire.instrument_openai_agents()` records prompt/completion content in spans by default — acceptable for the prototype (D4); scrubbing is mandatory before any real-data pilot.
- **NFR3:** Golden-set eval pass rate ≥ 90% before any prompt/tool change merges. The threshold is meaningful only at ≥ 20 cases — at the initial n=3 it is a smoke test, not a gate. Enforcement point is open (D5).
- **NFR4:** Secrets only via env; Slack request-signature verification and Jira webhook secret verification on every endpoint.
- **NFR5:** Layered architecture (import-linter enforced); new integrations = vendors adapter + config wiring only.
- **NFR6 (audit A9):** `openai-agents` stays pinned; a test round-trips a serialized `run_state_json` so an incompatible SDK upgrade fails `just test`; pending approvals are drained before SDK upgrades.

### Out of MVP (explicitly)

Durable approval store (in-memory MVP; Postgres slot ready via the
`ApprovalStore` protocol), Postgres-backed roles, persistent session memory,
streaming replies, ServiceNow (graduation, D9), specialist sub-agents, online
evals.

## 6. Architecture

```
 Slack (Events API + Interactivity)          Jira (webhooks)
      │ HTTPS (signed)                            │ HTTPS (secret-verified,
      ▼                                           ▼  own-actor filtered)
 FastAPI  /slack/events · /slack/interactions · /jira/webhook · /healthz   [interfaces]
      │ background task · context rebuilt per event:
      │ Slack thread replies / Jira comment history (capped, untrusted)
      ▼
 handle_support_request / resolve_approval (role-checked)    [application]
      │
      ▼
 Otto (Agents SDK): instructions + tools                     [application/agents]
      ├─ search_knowledge ──► Confluence MCP (read-only enforced + tested)
      ├─ request_access ────► SailPoint MCP (require_approval=always)
      ├─ list/read_runbook ─► runbooks/*.md (safe)
      └─ escalate_to_human ─► TicketBackend ► Slack triage channel
      │
      ├─ interruptions? ► PendingApproval(RunState JSON) ► approval card
      │                                          ▲ approve/deny (support_user/admin only)
      └─ final answer ► origin: Slack thread / Jira comment (D10: comments safe;
                        ticket transitions only via the sensitivity policy)

 LLM: internal gateway (OpenAI-compatible)                   [vendors/llm]
 Traces: Logfire SDK ─► Logfire UI + OTLP collector          [utils/telemetry]
 Evals: golden YAML + LLM judge (`just eval`)                [evals]
```

**HITL sequence:** sensitive tool call → `RunState` serialized into
`PendingApproval` (with a **channel-neutral origin reference** — Slack thread or
Jira issue key) in the approval store → Block Kit card in the triage channel →
authorized human clicks → store `resolve()` (exactly-once) → state
deserialized, `approve()`/`reject()` → run resumes → outcome posted at the
origin, card updated with resolver + outcome.

**Layering** (import-linter enforced): `main → interfaces → application →
evals → config → domain → vendors → data → utils → settings`.

## 7. Phased Plan

Phases are a sequence, not a calendar (D7). Step-level detail, gates, and
acceptance criteria per step: [`docs/implementation-plan.md`](implementation-plan.md).

### Phase 0 — Walking skeleton *(in progress)*

Everything runs end-to-end **with zero external dependencies** (stub tools, in-memory store).

- [x] Repo from cookiecutter (uv, ruff ALL, mypy strict, import-linter, justfile)
- [x] `compose.yml` dev stack — Postgres, Jaeger (OTLP), LiteLLM (gateway stand-in, `gateway` profile), Confluence MCP (`mcp` profile), containerized app, Slack tunnel; `just infra` / `just stack` / `just tunnel`
- [x] Settings surface (`settings.py`: LLM gateway, Slack, telemetry, MCP URLs/tokens, runbooks dir; empty MCP URL = stub) — Jira + role + kill-switch fields to add
- [x] Vendor adapters: `vendors/llm.py` (gateway model, SDK tracing off), `vendors/mcp.py` (Confluence; SailPoint with `require_approval="always"`), `vendors/slack.py` (gateway, approval card, triage backend)
- [x] Domain support layer: `SupportRequest`/`Capability`, `PendingApproval` + `ApprovalStore` protocol + `InMemoryApprovalStore`, `TicketBackend` protocol
- [x] Telemetry bootstrap: `utils/telemetry.py` (Logfire + optional OTLP, `instrument_openai_agents()`)
- [ ] Pin `openai-agents==0.18.2` (NFR6; currently `>=0.18.2`)
- [ ] **Channel-neutral domain shapes (D8):** `SupportRequest`/`PendingApproval` gain an origin reference (Slack thread | Jira issue) replacing Slack-only fields
- [ ] Composition root wiring (`config.py` currently wires settings only), incl. `JiraGateway` when configured
- [ ] FastAPI app: `/slack/events` (signature check, challenge, dedup, <3s ack), `/slack/interactions`, **`/jira/webhook` (secret check, own-actor filter, dedup)**, `/healthz`
- [ ] Otto agent: instructions + 4 capability tools (knowledge/access stubs, runbooks, escalate) with conversation reconstruction for both channels (D2)
- [ ] HITL loop: pause → approval card → role-checked resume → outcome at origin (FR6/FR7; in-memory store)
- [ ] Confluence read-only enforcement + no-write-tools test (A1)
- [ ] Failure-path error reply + alerting, both channels (FR8)
- [ ] Eval harness: 3 golden cases + LLM judge; **add the `eval` recipe to the justfile** (referenced in v0.1 but not yet present)
- [ ] Sample runbooks (`runbooks/` does not exist yet), `.env.example` covering every `Settings` field
- **Exit:** `just lint && just test` green; Slack DM → stubbed answer → approval card → role-checked resume; **Jira ticket → comment answer → same approval flow → outcome as ticket comment** — both against dev instances.

### Phase 1 — Knowledge MVP pilot

Real value, lowest risk: read-only knowledge + human escalation, on both channels.

- Confluence MCP connected (real KB; citations enforced in instructions; read-only verification re-run against the real tool list)
- Runbooks populated from top-10 support macros
- PII/trace scrubbing in place before any real-data traffic (graduation gate)
- Golden set grown to ≥ 20 cases (covering both entry points) — the point at which NFR3's 90% gate becomes meaningful; eval gate location decided (D5)
- Slack knowledge-Q&A resolution signal decided (D6-gap); ticket-resolved signal wired natively
- Graduation items if piloting inside the firm: deploy behind ingress, firm OTLP collector, Slack app install in pilot channels
- **Exit criteria:** ≥ 30% of pilot questions **resolved** (per D6) without human touch across both channels; eval pass ≥ 90% at n ≥ 20; support-team sign-off on escalation quality.

### Phase 2 — Access automation + durable HITL

The approval flow earns its keep.

- SailPoint MCP live; verify **every** exposed tool actually pauses (test against the real tool list, not assumptions)
- Postgres approval store: `ApprovalRecord` table **to be added** to `data/models.py` (which today holds only the `ExampleRecord` placeholder — v0.1 overstated this) + migration; approvals survive restarts; full audit trail; atomic conditional resolve
- Approver roles move from settings lists to a Postgres roles table; self-approval exclusion enforced (pending open decision)
- Approval expiry + reminders; per-tool sensitivity policy in config — **including Jira transition tools (resolve/close/assign), default-gated per D10**; `run_state_json` retention policy (A9 — PII at rest)
- **Exit criteria:** first 50 access requests processed with zero unapproved writes; audit report generated from the store.

### Phase 3 — ServiceNow, specialists & scale

- ServiceNow adapter behind the same channel seam at graduation (intake + comment gateway + `TicketBackend` escalation — config swap, zero agent changes)
- Specialist agents via Agents SDK handoffs once eval evidence shows single-agent instructions degrading
- Persistent session memory per conversation (Agents SDK sessions + Postgres; interacts with the PII posture)
- Streaming/progressive updates; online evals sampled from traces; rate limiting; Redis dedup for multi-replica; Enterprise Grid rollout (fix the `slack.com/archives` link format first)
- **Exit criteria:** Otto is the default first responder in all support channels and the ticket queue.

## 8. Success Metrics

Targets are directional while Otto is a prototype (D7); they become gates at graduation.

| Metric | Target (Phase 1) | Source |
|---|---|---|
| Resolution rate (resolved w/o human, per D6) | ≥ 30% | traces + escalation count + resolution signals (incl. ticket status) |
| Time-to-first-response | < 15s | OTel spans |
| Eval pass rate | ≥ 90% at n ≥ 20 cases | `just eval` (gate location: D5) |
| Approval turnaround (Phase 2) | < 30min median | approval store timestamps |
| Unapproved sensitive writes | **0, always** | audit log |

“Resolved” = successful SailPoint submission, explicit resolution by a support
agent, **or the originating ticket reaching resolved/closed** (D6/D8). Slack
knowledge-Q&A still registers under no signal — closing that gap (✅ reaction /
“did this help?”) is a Phase 1 prerequisite for the Slack side of the metric.

## 9. Risks & Open Questions

| # | Risk / question | Status / mitigation |
|---|---|---|
| 1 | **Confluence path is not actually read-only** (A1): no tool filter on the mounted MCP server; the dev image ships write tools | **Open — early MVP task.** Tool allowlist/read-only mode + a test asserting no write tools in the mounted toolset |
| 2 | PII/chat content in traces — `instrument_openai_agents()` records content by default (A6) | Logfire dual-sink approved for the prototype (D4); scrubbing + OTel attribute filtering mandatory before any real-data pilot |
| 3 | Gateway rate limits / model quality on gateway-routed models | Load-test via the LiteLLM stand-in; eval set doubles as regression net — a model swap is one `.env` value |
| 4 | Confluence MCP hosting: firm-hosted `mcp-atlassian` (as in `compose.yml`) vs Atlassian remote | Decide at Phase 1 start; stub keeps dev unblocked |
| 5 | In-memory approvals lost on restart | Accepted for the prototype (card persists in Slack; requester re-asks); Phase 2 makes durable |
| 6 | Slack retry storms / duplicate events | event-id/ts dedup in Phase 0; Redis dedup before multi-replica |
| 7 | Prompt injection — **two** surfaces: Confluence page content **and** reconstructed conversation content (Slack threads *and* Jira comments, D2/D8) | Instructions hardening + adversarial eval cases; history cap; writes stay HITL-gated regardless, so injection cannot cause an unapproved action |
| 8 | `run_state_json` holds the full conversation (A9): PII at rest once persisted; format coupled to a 0.x SDK — an upgrade can strand pending approvals | Pin SDK; round-trip test (NFR6); drain pending approvals before upgrades; retention/expiry in Phase 2 |
| 9 | Eval gate has no enforcement point — GitHub-hosted CI has no LLM access (A7) | **Open — D5**; recommended default documented in §4 |
| 10 | Background task dies silently after the intake ack (A8) | Mitigated by FR8: catch-all error reply at the origin + alerting + kill switch |
| 11 | **Bot reply loops on the ticket channel**: Otto's own comments re-trigger the Jira webhook | FR9: own-actor filtering + event dedup at `/jira/webhook`; loop test in the interface suite |

## 10. Delivery Checklist

**Prototype (in-repo, no firm dependencies):**

- [x] PRD v0.3 reviewed (this doc; decisions in `docs/decision-log.md`)
- [ ] Jira Cloud dev instance created (free tier) + API token + webhook secret
- [ ] Phase 0 skeleton complete per §7 checklist — both entry points
- [ ] Confluence read-only enforcement test green (risk #1)
- [ ] `just eval` recipe + 3 golden cases running against the LiteLLM stand-in

**Graduation notes (needed only when Otto becomes a firm deliverable):**

- [ ] Dev → firm Slack workspace + app manifest approval
- [ ] Firm Jira (or ServiceNow, D9) endpoints + integration account; webhook allowlisting
- [ ] Gateway credentials issued for an Otto service account
- [ ] Confluence/SailPoint MCP endpoints identified; data-egress review
- [ ] Security sign-off: Logfire SaaS export, PII scrubbing rules, approver policy, ticket write posture (D10)
- [ ] Deployment behind firm ingress; firm OTLP collector endpoint
- [ ] Enterprise Grid-safe thread links (replace hard-coded `slack.com/archives`)

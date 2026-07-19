# Hardening Plan: post-audit fixes & value validation

**2026-07-19 · Owner: Ollie Tian · supplements
[`implementation-plan.md`](implementation-plan.md) — this plan covers the work
that fell out of the 2026-07-19 blind-spot audit (round 2) and the follow-up
interview; it does not re-plan the product phases.**

## Project goal

Otto is a HITL-first tech-support agent — first responder for Slack
mentions/DMs and Jira tickets, resolving the repetitive tier autonomously while
every sensitive write pauses for a role-checked human approval. It is a
personal prototype (D7) whose differentiator is the *provable* HITL guarantee;
the Streamlit surface exists only as dev harness and demo vehicle, never a
product channel.

## Confirmed decisions

- **Chat surface = harness + demo, not a channel** (interview answer 3, D21).
  D2 conversation reconstruction stays the production context model;
  `agent_sessions` stays dev-only; the `channel` column's planned `"slack"`
  tagging does not proceed.
- **The firm classification API is the value-hypothesis signal** (interview
  answer 1, D24). Its taxonomy + class distribution bounds Otto's addressable
  ceiling; at graduation it becomes Jira admission control and the escalation
  routing key. Integration itself stays behind the graduation perimeter.
- **Instrument approvers, don't design countermeasures** (interview answer 2,
  D24). The approval store already carries the data; the pilot answers the
  volume/diligence question.
- Load-bearing priors this plan builds on: D19 (approval authorizes
  *automation*, not the entitlement), default-deny sensitivity policy (2.6),
  two-tier eval gating (D5), self-approval prohibited (T3).

## Key unresolved issues

| Unknown | Why it matters | Resolved by |
|---|---|---|
| `[TBC]` Classifier taxonomy + class-volume distribution | Direction-defining: bounds the ≥30% target and decides which capabilities deserve investment | Ollie, via whoever owns the classification API at the firm |
| `[TBC]` Firm SailPoint integration reality (MCP? REST? exists at all?) | The whole T8 surface (policy names, instructions, evals) is coupled to an invented mock | Graduation-time discovery; until then, default-deny fails safe on name mismatch |
| `[TBC]` Kill-switch mechanism (DB flag vs file vs reloadable env) | Defines the incident-response surface | Ollie, at step C1 (recommendation embedded there) |
| `[TBC]` Recovery semantics for approved-but-unexecuted runs | Changes what the audit trail *means* | Ollie, at step B2 |
| Approver volume/diligence | Deferred by design — answer 2 | Pilot instrumentation (step D2) |

**Phase E (value validation) should not drive new capability spend until the
classifier data lands.** Phases A–D are hardening of what exists and are safe
to run regardless.

## Phased plan

### Phase A: Paper catches up to reality — docs stop lying

| # | Step | Gate | Goal | I/O | Risks | Acceptance criteria |
|---|---|---|---|---|---|---|
| A1 | Decision-log entries for the undocumented week | `[AUTONOMOUS]` | Record: chat-surface status (harness+demo), sessions-table-dev-only, Langfuse/LGTM third sink, per-tool FunctionTool gating | In: audit findings. Out: dated entries in `docs/decision-log.md` | Entries drift into re-arguing decisions instead of recording them | Every 07-18/19 commit's direction change traceable to a logged decision |
| A2 | PRD v0.4 | `[AUTONOMOUS]` | Close stale opens (D5, T2, T3 shown as open), reframe FR4/FR7 per D19, correct §4's stale `require_approval` text, note phase-order inversion, scope the "no web UI / no session memory" non-goals to *product* surfaces | In: A1 entries. Out: PRD v0.4 | Over-editing — v0.4 should correct, not re-plan | PRD contains no claim contradicted by the code; changelog block at top names what moved |
| A3 | Graduation-note additions | `[AUTONOMOUS]` | Add: classifier API (admission control + escalation routing key), `TicketBackend` routing-key seam, Langfuse egress to the security sign-off list, T8-vs-real-SailPoint rewrite risk | In: audit findings 5, 9; answer 1. Out: updated §10 + `TicketBackend` docstring note | None real | Each graduation blocker names the finding it retires |

### Phase B: The HITL guarantee becomes provable — checkpoint: invariants hold under test

| # | Step | Gate | Goal | I/O | Risks | Acceptance criteria |
|---|---|---|---|---|---|---|
| B1 | Self-approval fails closed on unmapped identities | `[AUTONOMOUS]` | Audit finding 4: `_may_resolve` denies when requester *or* resolver is unmapped, mirroring the fail-closed role lookup | In: `support.py::_may_resolve`, `users.py::same_person`. Out: guard change + unit tests (unmapped requester, unmapped resolver, cross-channel pair) | Over-blocking: unmapped *approvers* already denied via role path — keep the change to the same-person half | Test: approver with unmapped Jira id cannot approve own ticket-origin request; mapped non-self approvals unaffected |
| B2 | Approved-but-unexecuted recovery | `[NEEDS APPROVAL]` — **question: on startup-found approved-unexecuted approvals, auto-resume the run, or mark failed + notify origin and triage? (recommended: notify-only — auto-resume re-fires a sensitive tool without a human watching)** | Close the crash window between `resolve()` and resume completing | In: decision. Out: `executed_at` column + migration, stamp after resume, startup/sweep pass over approved-unexecuted rows | Migration touches the audited table — round-trip NFR6 test must still pass; double-execution if stamping is misordered | Kill the process between resolve and resume in an integration test → on restart the approval is surfaced per chosen semantics; audit shows decided vs executed distinctly |
| B3 | Every interruption visible before decision | `[NEEDS APPROVAL]` — **question: render all interruptions on one card (small), or one card per interruption (real per-action HITL, more state)? Recommended: render-all now, per-interruption when a real multi-tool run is first observed** | One decision currently authorizes calls the approver never saw (`_pause_for_approval` renders `interruptions[0]` only) | In: decision. Out: card rendering change (+ store shape change if per-interruption) | Per-interruption cards complicate exactly-once resume — the SDK resumes one state, not per-tool | A two-gated-tool scripted run produces card(s) naming both calls; approver decision provably covers only what was shown |
| B4 | Dedup key includes arguments | `[AUTONOMOUS]` | Stop swallowing a *different* second request as a duplicate (key today: origin + tool only) | In: `find_pending` in both stores. Out: key = origin + tool + args hash | Arg strings may differ trivially (whitespace/order) → hash canonicalized JSON | Same request re-ask → suppressed; different entitlement same thread → second card posted |
| B5 | Audit trail records attempts | `[AUTONOMOUS]` | Unauthorized clicks, self-approval blocks, expiries become durable audit rows, not just log events | In: `resolve_approval` reject paths. Out: `audit_events` append + inclusion in `list_audit_entries`/report | Scope creep into a generic event bus — record the three HITL event types only | Audit report shows who *tried* and was rejected, with reason |
| B6 | Injection-cannot-bypass-gate eval | `[AUTONOMOUS]` | Make risk #7's promise real: an adversarial case asserting injected content cannot reach an ungated write or skip the pause | In: existing scripted-model test harness. Out: 1 deterministic test + 1 golden eval case with injected thread content | A passing test proves the *policy* holds, not model robustness — name it accordingly | Deterministic test green in `just test`; golden case in `just eval` |

### Phase C: Incident readiness — checkpoint: a drill passes

| # | Step | Gate | Goal | I/O | Risks | Acceptance criteria |
|---|---|---|---|---|---|---|
| C1 | Runtime kill switch | `[NEEDS APPROVAL]` — **question: DB flag (recommended; checked per request, settings value as fallback when no DB), or reloadable config file? And on DB unreachable: fail open (keep serving) or closed?** | FR8's "without undeploying" becomes true — `otto_enabled` is currently frozen at import | In: decision. Out: runtime check in the three routers + a `just` recipe / SQL one-liner to flip it | Per-request DB read adds latency — cache with short TTL; fail-open/closed choice is a real availability-vs-safety tradeoff | Flip the flag with the process running → next event acked-but-unanswered within TTL seconds; no restart |
| C2 | Jira intake throttle | `[AUTONOMOUS]` | A bulk import must not become N agent runs + N comments | In: existing `_RateLimiter` pattern. Out: global events/minute cap on `/jira/webhook` dispatch, over-cap events logged + dropped before LLM | Legit burst during an incident gets throttled — log loudly so triage sees it | Replay 100 synthetic webhook events in 1 min → ≤ cap agent runs, zero comments beyond cap, `jira_rate_limited` events emitted |
| C3 | Egress + session-PII housekeeping | `[AUTONOMOUS]` | Scrub `otto.thinking_steps` span content (or gate it to dev), add dev-chat purge (sessions past N days) to the sweep or a `just` recipe | In: `chat_app.py::_absorb`, sweep. Out: scrub/gate + purge | Purging a session with a live `pending_state` strands the dev chat — exclude pending | Prod-env config emits no tool-output span attributes; purge leaves pending chats intact |

### Phase D: Honest demo + measurement — checkpoint: the demo shows the real thing

| # | Step | Gate | Goal | I/O | Risks | Acceptance criteria |
|---|---|---|---|---|---|---|
| D1 | Demo-grade approval flow in Streamlit | `[NEEDS APPROVAL]` — **question: route approve/reject through the real `resolve_approval` (role check, self-approval block, audit row) with a selectable simulated approver identity, or keep the fake button but label it "simulated"? Recommended: the real route — the HITL flow is the demo's whole point** | The demo vehicle stops contradicting the HITL story it pitches | In: decision. Out: chat approval path through `resolve_approval` + an approver-identity picker (or the label) | Real route needs the Postgres store + a seeded approver — document in the demo script; Slack card posting must be stubbed, not required | Demo walkthrough: request → pause → *unauthorized* click politely rejected → authorized approve → outcome + audit row; self-approval visibly blocked |
| D2 | Approver instrumentation in the audit report | `[AUTONOMOUS]` | The deferred approver-volume question gets its measurement: per-approver volume/day and decision-latency distribution from existing timestamps | In: `list_audit_entries`. Out: two aggregates in `audit_report.py` | None — data already exists | Report renders approvals/day per approver and latency percentiles from store data |

### Phase E: Value validation — gated on the `[TBC]` classifier data

| # | Step | Gate | Goal | I/O | Risks | Acceptance criteria |
|---|---|---|---|---|---|---|
| E1 | Obtain classifier taxonomy + class distribution | `[NEEDS APPROVAL]` — user action: **who owns the API, and what can you export (labels + counts, no ticket text)?** | Replace the invented distribution with the real one | In: firm contact `[TBC]`. Out: class list + volume histogram (any format) | Perimeter: even label counts may need a nod — ask before exporting | A file/table of real classes with volumes exists in or alongside the repo |
| E2 | Addressable-ceiling analysis | `[AUTONOMOUS]` once E1 lands | Map each class → Otto capability (knowledge / runbook / access / none); compute the honest ceiling for the resolution target | In: E1 data. Out: short analysis doc; recalibrated §8 target | Mapping is judgment — mark low-confidence classes rather than forcing them | Every class mapped or marked; ceiling stated with the assumption list |
| E3 | Re-plan capability investment | `[NEEDS APPROVAL]` — **question: given the ceiling, does Phase 1 (real Confluence, n≥20 golden set) proceed as planned, refocus (e.g. runbooks dominate), or shrink?** | Spend follows evidence, not the invented content | In: E2 doc. Out: decision-log entry + updated `implementation-plan.md` Phase 1 | Sunk-cost pull toward the existing Phase 1 shape | A logged go/refocus/shrink decision citing E2 numbers |

## Risks & fallbacks

- **Classifier data proves unobtainable** → fallback: a 30-minute ranking
  interview with one support engineer ("what are your top 10 request types by
  volume?") — subjective but bounds the ceiling; failing that, run the pilot
  scoped to knowledge-Q&A only and let its traffic *be* the distribution
  measurement.
- **Real SailPoint ≠ the T8 mock** → default-deny already fails safe on unknown
  names; keep agent instructions phrased by *capability* ("submit the access
  request") rather than tool name where possible, and budget an adapter + eval
  rewrite as a named graduation cost rather than a surprise.
- **B2/B3 migrations destabilize the pinned-SDK state format** → the NFR6
  round-trip test is the tripwire; run `docs/drain-approvals.md` before any
  schema/SDK change that touches `run_state_json`.
- **C1 kill switch DB dependency** → if fail-closed is chosen and the DB blips,
  Otto goes silent — acceptable for a support bot; if that's not acceptable,
  the file-based toggle is the fallback mechanism.
- **D1 real-route demo grows Slack dependencies** → fallback is the in-app
  approver pane with a stubbed card poster; the role/self-approval/audit checks
  still run for real.

## Overall acceptance criteria

1. **Docs match code:** no direction change of the last two weeks is
   undocumented; PRD v0.4 contains no claim the code contradicts.
2. **The HITL guarantee is test-proven, not asserted:** unmapped-identity
   self-approval blocked, no approved action can be silently lost across a
   crash, no decision covers an unseen tool call, injection cannot reach an
   ungated write — each as a runnable test.
3. **An incident drill passes:** Otto silenced at runtime without a restart,
   and a 100-ticket burst produces bounded spend and zero comment spam.
4. **The demo demonstrates the differentiator:** a viewer watches a real
   role-checked, audited approval — including a rejection — not a fake button.
5. **The value ceiling is a number derived from real firm data, with a logged
   go/refocus/shrink decision on further capability spend.**

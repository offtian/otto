# Draining pending approvals before an `openai-agents` upgrade (NFR6)

`PendingApproval.run_state_json` holds an `openai-agents` `RunState` serialized
with the **current** SDK version. A version bump can change that format, so a
pending approval saved before the upgrade may fail to deserialize — and
therefore fail to resume — after it. Drain in-flight approvals first.

Before bumping the `openai-agents` pin:

1. **Check for pending approvals** (against the compose Postgres — `just infra`):

   ```sql
   SELECT id, tool_name, requester_id, created_at
   FROM approvals WHERE status = 'pending';
   ```

2. **Drain them.** Either wait for approvers to resolve the open cards, or
   resolve them administratively. A denied approval never executes its tool, so
   this is safe — the requester simply re-asks after the upgrade:

   ```sql
   UPDATE approvals
   SET status = 'denied', resolver_id = 'system:drain', resolved_at = now()
   WHERE status = 'pending';
   ```

   Post a note in the triage channel so affected requesters re-submit.

3. **Confirm zero pending**, then bump the pin in `pyproject.toml`, run
   `uv lock`, and deploy.

The version pin (`openai-agents==0.18.2`) and the `RunState` round-trip test in
`tests/functional/test_access_request_approval.py` are the other half of this
guard: they catch a format break in CI, while this runbook stops a break from
stranding in-flight approvals in production.

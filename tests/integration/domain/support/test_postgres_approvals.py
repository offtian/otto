"""
Integration tests for the durable approval store (2.2) against a real
Postgres — the concurrent-resolve test *is* the zero-double-write guarantee.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import asyncio
import os
from datetime import UTC, datetime

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import approvals, audit, entities


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _pending(
    approval_id: str = "ap-1",
    origin: entities.Origin | None = None,
) -> approvals.PendingApproval:
    return approvals.PendingApproval(
        id=approval_id,
        request_id="req-1",
        requester_id="U_REQ",
        origin=origin or entities.SlackThread(channel_id="C1", thread_ts="1.0"),
        request_text="I need Snowflake access",
        tool_name="request_access",
        tool_arguments='{"system": "snowflake"}',
        run_state_json='{"state": "paused"}',
    )


@pytest.fixture
async def store():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM audit_events")
    await db.execute("DELETE FROM approvals")
    try:
        yield approvals.PostgresApprovalStore(database=db)
    finally:
        await db.execute("DELETE FROM audit_events")
        await db.execute("DELETE FROM approvals")
        await db.disconnect()


class TestPostgresApprovalStore:
    @pytest.mark.parametrize(
        "origin",
        [
            entities.SlackThread(channel_id="C1", thread_ts="1700.5"),
            entities.TicketRef(issue_key="IT-42"),
        ],
    )
    async def test_save_and_get_round_trips_both_origins(self, store, origin):
        # Given a pending approval with this origin
        pending = _pending(origin=origin)

        # When it is saved and fetched back from Postgres
        await store.save(pending)
        fetched = await store.get("ap-1")

        # Then every field survives the round trip, origin included
        assert fetched == pending

    async def test_get_raises_for_an_unknown_id(self, store):
        # Given an empty store
        # When an unknown id is fetched
        # Then it is signalled explicitly
        with pytest.raises(approvals.ApprovalNotFound):
            await store.get("nope")

    async def test_resolve_records_the_resolver_and_time(self, store):
        # Given a stored pending approval
        await store.save(_pending())

        # When an approver resolves it
        resolved = await store.resolve(
            "ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT"
        )

        # Then the audit trail is persisted
        assert resolved.status is approvals.ApprovalStatus.APPROVED
        assert resolved.resolver_id == "U_SUPPORT"
        assert resolved.resolved_at is not None
        assert (await store.get("ap-1")).resolver_id == "U_SUPPORT"

    async def test_resolve_is_exactly_once(self, store):
        # Given an already-resolved approval
        await store.save(_pending())
        await store.resolve("ap-1", approvals.ApprovalStatus.DENIED, resolver_id="U_ADMIN")

        # When a second decision arrives
        # Then the guard fires — no second execution
        with pytest.raises(approvals.ApprovalAlreadyResolved):
            await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_OTHER")

    async def test_resolve_unknown_id_raises_not_found(self, store):
        # Given no such approval
        # When it is resolved
        # Then the store distinguishes unknown from already-resolved
        with pytest.raises(approvals.ApprovalNotFound):
            await store.resolve(
                "ghost", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT"
            )

    async def test_concurrent_resolve_executes_exactly_once(self, store):
        # Given a stored pending approval and two approvers clicking at once
        await store.save(_pending())

        # When both resolutions race
        results = await asyncio.gather(
            store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_A"),
            store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_B"),
            return_exceptions=True,
        )

        # Then exactly one wins and the other is rejected as already-resolved —
        # the conditional UPDATE never lets both execute
        winners = [r for r in results if isinstance(r, approvals.PendingApproval)]
        rejected = [r for r in results if isinstance(r, approvals.ApprovalAlreadyResolved)]
        assert len(winners) == 1
        assert len(rejected) == 1
        assert (await store.get("ap-1")).status is approvals.ApprovalStatus.APPROVED


class TestPostgresApprovalStoreFindPending:
    async def test_finds_a_pending_approval_by_origin_tool_and_arguments(self, store):
        # Given a stored pending approval on a Slack origin
        origin = entities.SlackThread(channel_id="C1", thread_ts="1.0")
        await store.save(_pending(origin=origin))

        # When a matching origin, tool, and an equivalently-formatted argument
        # string are looked up (B4: canonical comparison)
        found = await store.find_pending(
            origin=origin,
            tool_name="request_access",
            tool_arguments='{ "system" : "snowflake" }',
        )

        # Then that pending approval is returned
        assert found is not None
        assert found.id == "ap-1"

    async def test_returns_none_when_the_tool_differs(self, store):
        # Given a pending approval for one tool
        origin = entities.SlackThread(channel_id="C1", thread_ts="1.0")
        await store.save(_pending(origin=origin))

        # When a different tool on the same origin is looked up
        found = await store.find_pending(
            origin=origin,
            tool_name="submit_access_request",
            tool_arguments='{"system": "snowflake"}',
        )

        # Then nothing matches — the tool differs
        assert found is None

    async def test_returns_none_when_the_arguments_differ(self, store):
        # Given a pending approval for one argument set
        origin = entities.SlackThread(channel_id="C1", thread_ts="1.0")
        await store.save(_pending(origin=origin))

        # When the same tool is looked up with different arguments (B4)
        found = await store.find_pending(
            origin=origin,
            tool_name="request_access",
            tool_arguments='{"system": "workday"}',
        )

        # Then nothing matches — a different request gets its own card
        assert found is None

    async def test_ignores_a_resolved_approval(self, store):
        # Given an approval that has been resolved
        origin = entities.SlackThread(channel_id="C1", thread_ts="1.0")
        await store.save(_pending(origin=origin))
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When its origin, tool, and arguments are looked up
        found = await store.find_pending(
            origin=origin,
            tool_name="request_access",
            tool_arguments='{"system": "snowflake"}',
        )

        # Then it is not returned — only a still-pending run suppresses a duplicate
        assert found is None


class TestPostgresApprovalStoreExecution:
    async def test_a_resolved_approval_is_unexecuted_until_marked(self, store):
        # Given an approval resolved but never marked executed (B2 crash window)
        await store.save(_pending())
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the unexecuted approvals are listed
        unexecuted = await store.list_unexecuted()

        # Then it is returned — decided but never carried out
        assert [a.id for a in unexecuted] == ["ap-1"]

    async def test_mark_executed_dispositions_the_approval(self, store):
        # Given a resolved approval
        await store.save(_pending())
        await store.resolve("ap-1", approvals.ApprovalStatus.DENIED, resolver_id="U_ADMIN")

        # When it is marked executed
        await store.mark_executed("ap-1")

        # Then it no longer needs recovery and carries the execution stamp
        assert await store.list_unexecuted() == []
        assert (await store.get("ap-1")).executed_at is not None

    async def test_pending_and_expired_approvals_never_need_recovery(self, store):
        # Given one approval the sweep expired and one still pending
        await store.save(_pending("ap-expired"))
        await store.expire_pending(cutoff=FAR_FUTURE)
        await store.save(_pending("ap-pending"))

        # When the unexecuted approvals are listed
        unexecuted = await store.list_unexecuted()

        # Then neither is returned — pending owes no execution yet, expired never will
        assert unexecuted == []


class TestPostgresApprovalStoreEvents:
    async def test_record_event_persists_and_lists_in_order(self, store):
        # Given two rejected-attempt events (B5)
        await store.record_event(
            audit.AuditEvent(event_type="unauthorized_role", actor_id="U_RANDO")
        )
        await store.record_event(
            audit.AuditEvent(event_type="self_approval", actor_id="U_SUPPORT", approval_id="ap-1")
        )

        # When the events are listed
        events = await store.list_events()

        # Then both come back in order with a server-stamped occurred_at
        assert [e.event_type for e in events] == ["unauthorized_role", "self_approval"]
        assert all(e.occurred_at is not None for e in events)
        assert events[1].approval_id == "ap-1"


class TestPostgresApprovalStoreSweep:
    async def test_expire_pending_marks_stale_approvals_terminal(self, store):
        # Given a stored pending approval
        await store.save(_pending())

        # When the sweep expires everything created before a future cutoff
        expired = await store.expire_pending(cutoff=FAR_FUTURE)

        # Then it comes back EXPIRED with a server-clock terminal timestamp
        assert [a.id for a in expired] == ["ap-1"]
        stored = await store.get("ap-1")
        assert stored.status is approvals.ApprovalStatus.EXPIRED
        assert stored.resolved_at is not None

    async def test_expire_pending_spares_fresh_approvals(self, store):
        # Given a just-created pending approval
        await store.save(_pending())

        # When the cutoff predates it
        expired = await store.expire_pending(cutoff=DISTANT_PAST)

        # Then nothing expires and it stays pending
        assert expired == []
        assert (await store.get("ap-1")).status is approvals.ApprovalStatus.PENDING

    async def test_claim_due_reminders_claims_a_pending_approval_once(self, store):
        # Given a pending approval past its reminder interval
        await store.save(_pending())

        # When reminders due before a future cutoff are claimed
        first = await store.claim_due_reminders(cutoff=FAR_FUTURE)

        # Then it is due once, and not again until another interval elapses
        assert [a.id for a in first] == ["ap-1"]
        assert await store.claim_due_reminders(cutoff=DISTANT_PAST) == []

    async def test_purge_resolved_state_nulls_run_state_after_retention(self, store):
        # Given a resolved approval whose run state is still stored
        await store.save(_pending())
        await store.resolve("ap-1", approvals.ApprovalStatus.APPROVED, resolver_id="U_SUPPORT")

        # When the retention cutoff falls after its resolution
        purged = await store.purge_resolved_state(cutoff=FAR_FUTURE)

        # Then the conversation-bearing run state is dropped (A9)
        assert purged == 1
        assert (await store.get("ap-1")).run_state_json == ""

    async def test_purge_resolved_state_spares_pending_and_recent(self, store):
        # Given one still-pending approval and one just-resolved approval
        await store.save(_pending("ap-pending"))
        await store.save(_pending("ap-resolved"))
        await store.resolve("ap-resolved", approvals.ApprovalStatus.DENIED, resolver_id="U_ADMIN")

        # When the retention cutoff predates the resolution
        purged = await store.purge_resolved_state(cutoff=DISTANT_PAST)

        # Then nothing is purged — pending state is never touched, recent state is retained
        assert purged == 0
        assert (await store.get("ap-pending")).run_state_json == '{"state": "paused"}'
        assert (await store.get("ap-resolved")).run_state_json == '{"state": "paused"}'

"""
Integration tests for the durable approval store (2.2) against a real
Postgres — the concurrent-resolve test *is* the zero-double-write guarantee.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import asyncio
import os

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import approvals, entities


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)


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
    await db.execute("DELETE FROM approvals")
    try:
        yield approvals.PostgresApprovalStore(database=db)
    finally:
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

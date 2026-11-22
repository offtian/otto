"""
Integration tests for the follow-up store against a real Postgres — the two
caps are what keep an agent from waking itself forever.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import os
from datetime import UTC, datetime, timedelta

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import entities, followups


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)
THREAD = entities.SlackThread(channel_id="C1", thread_ts="1700.5")
OTHER_THREAD = entities.SlackThread(channel_id="C1", thread_ts="1800.5")


@pytest.fixture
async def store():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM follow_ups")
    try:
        yield followups.PostgresFollowUpStore(database=db)
    finally:
        await db.execute("DELETE FROM follow_ups")
        await db.disconnect()


async def _schedule(store, origin=THREAD, note="check Jenkins build 4521"):
    await store.schedule(
        origin=origin, requester_id="U_REQ", note=note, delay=timedelta(minutes=30)
    )


class TestPostgresFollowUpStore:
    async def test_a_follow_up_is_due_only_once_its_time_has_come(self, store):
        # Given a follow-up scheduled half an hour out
        await _schedule(store)

        # When due follow-ups are read as of long ago, and as of the far future
        too_early = await store.due(now=DISTANT_PAST)
        due = await store.due(now=FAR_FUTURE)

        # Then it shows up only once its time has passed, round-tripped intact
        assert too_early == []
        assert [(f.origin, f.requester_id, f.note) for f in due] == [
            (THREAD, "U_REQ", "check Jenkins build 4521")
        ]

    async def test_a_fired_follow_up_is_not_due_again(self, store):
        # Given a follow-up that came due and was fired
        await _schedule(store)
        (follow_up,) = await store.due(now=FAR_FUTURE)
        await store.mark_fired(follow_up.id)

        # When due follow-ups are read again
        due = await store.due(now=FAR_FUTURE)

        # Then it does not come back
        assert due == []

    async def test_cancels_only_the_conversations_own_pending_follow_ups(self, store):
        # Given a follow-up on each of two conversations
        await _schedule(store)
        await _schedule(store, origin=OTHER_THREAD)

        # When one conversation cancels its follow-ups
        cancelled = await store.cancel_pending(origin=THREAD)

        # Then only its own is cancelled
        assert cancelled == 1
        assert [f.origin for f in await store.due(now=FAR_FUTURE)] == [OTHER_THREAD]

    async def test_caps_the_follow_ups_waiting_on_one_conversation(self, store):
        # Given a conversation holding as many pending follow-ups as allowed
        for _ in range(followups.MAX_PENDING):
            await _schedule(store)

        # When it schedules one more
        with pytest.raises(followups.FollowUpLimitReached):
            await _schedule(store)

        # Then another conversation is unaffected
        await _schedule(store, origin=OTHER_THREAD)

    async def test_caps_the_follow_ups_one_conversation_schedules_in_a_day(self, store):
        # Given a conversation whose follow-ups keep firing and rescheduling
        # until it has used its daily allowance
        for _ in range(followups.MAX_PER_DAY):
            await _schedule(store)
            (follow_up,) = await store.due(now=FAR_FUTURE)
            await store.mark_fired(follow_up.id)

        # When the chain tries to schedule its next link
        # Then it is refused, although nothing is pending
        with pytest.raises(followups.FollowUpLimitReached):
            await _schedule(store)

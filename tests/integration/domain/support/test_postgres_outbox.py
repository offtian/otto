"""
Integration tests for the outbox against a real Postgres — the unique
idempotency key *is* the one-reply-per-run guarantee, and the claim's hold is
what keeps a reply from being sent twice.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import os
from datetime import UTC, datetime, timedelta

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import entities, outbox


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


@pytest.fixture
async def store():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM outbox")
    try:
        yield outbox.PostgresOutbox(database=db)
    finally:
        await db.execute("DELETE FROM outbox")
        await db.disconnect()


class TestPostgresOutbox:
    async def test_records_one_reply_per_key(self, store):
        # Given a reply recorded for a run
        first = await store.enqueue(
            key="Ev1", origin=THREAD, text="Try the VPN runbook.", vote=True
        )

        # When the replayed run records its answer again
        second = await store.enqueue(key="Ev1", origin=THREAD, text="Again.", vote=True)

        # Then only the first is on record, round-tripped intact
        assert second is None
        assert (first.key, first.origin, first.text, first.vote, first.attempts) == (
            "Ev1",
            THREAD,
            "Try the VPN runbook.",
            True,
            0,
        )

    async def test_hides_a_claimed_reply_from_the_next_claim(self, store):
        # Given a recorded reply
        await store.enqueue(key="Ev1", origin=THREAD, text="Try the VPN runbook.", vote=False)

        # When it is claimed, and a second claim follows a second later
        claimed = await store.claim_due(now=FAR_FUTURE)
        claimed_again = await store.claim_due(now=FAR_FUTURE + timedelta(seconds=1))

        # Then only the first claim gets it — a slow send is never doubled
        assert [message.key for message in claimed] == ["Ev1"]
        assert claimed_again == []

    async def test_does_not_claim_a_reply_before_its_attempt_time(self, store):
        # Given a reply recorded just now, held for its inline attempt
        await store.enqueue(key="Ev1", origin=THREAD, text="Try the VPN runbook.", vote=False)

        # When due replies are claimed as of long ago
        claimed = await store.claim_due(now=DISTANT_PAST)

        # Then it is not handed out
        assert claimed == []

    async def test_a_sent_reply_keeps_its_key_but_not_its_text(self, store):
        # Given a reply that was delivered
        message = await store.enqueue(key="Ev1", origin=THREAD, text="Try the VPN.", vote=False)
        await store.mark_sent(message.id)

        # When the same run answers again, and due replies are claimed
        replayed = await store.enqueue(key="Ev1", origin=THREAD, text="Again.", vote=False)
        claimed = await store.claim_due(now=FAR_FUTURE)
        body = await store.database.fetch_val("SELECT body FROM outbox")

        # Then the key still blocks a second answer, nothing is due, and the
        # conversation text is gone
        assert (replayed, claimed, body) == (None, [], "")

    async def test_a_failure_is_counted_and_a_dead_reply_is_never_claimed(self, store):
        # Given a reply whose delivery failed once and is waiting to retry
        message = await store.enqueue(key="Ev1", origin=THREAD, text="Try the VPN.", vote=False)
        await store.record_failure(message.id, error="ConnectionError", retry_in=timedelta(0))
        retried = await store.claim_due(now=FAR_FUTURE)

        # When the next failure is final
        await store.record_failure(message.id, error="ConnectionError", retry_in=None)
        claimed = await store.claim_due(now=FAR_FUTURE + timedelta(hours=1))

        # Then the retry saw one failed attempt, and the dead reply stays put
        assert [candidate.attempts for candidate in retried] == [1]
        assert claimed == []

    async def test_purges_finished_replies_but_never_pending_ones(self, store):
        # Given one delivered reply and one still pending
        delivered = await store.enqueue(key="Ev1", origin=THREAD, text="Done.", vote=False)
        await store.mark_sent(delivered.id)
        await store.enqueue(key="Ev2", origin=THREAD, text="Waiting.", vote=False)

        # When everything recorded before the far future is purged
        purged = await store.purge(cutoff=FAR_FUTURE)

        # Then only the delivered reply is removed
        assert purged == 1
        assert [message.key for message in await store.claim_due(now=FAR_FUTURE)] == ["Ev2"]

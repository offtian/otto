"""
Integration tests for the durable inbox against a real Postgres — the
insert-if-absent in ``accept`` *is* the cross-restart dedup guarantee.

Live: needs `just infra` (compose Postgres) + the migrations applied. Guarded
by RUN_INTEGRATION so the normal test suite and CI skip it. Run via
`just test-integration`.
"""

import os
from datetime import UTC, datetime

import databases
import pytest

from otto.data import _dsn
from otto.domain.support import entities, inbox


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION"),
    reason="needs Postgres — run via `just test-integration`",
)

DB_URL = _dsn.to_libpq(
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5432/otto")
)

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
DISTANT_PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _request(request_id: str, origin: entities.Origin) -> entities.SupportRequest:
    return entities.SupportRequest(
        id=request_id, user_id="U_REQ", text="my VPN keeps dropping", origin=origin
    )


@pytest.fixture
async def store():
    db = databases.Database(DB_URL)
    await db.connect()
    await db.execute("DELETE FROM inbound_requests")
    try:
        yield inbox.PostgresInbox(database=db)
    finally:
        await db.execute("DELETE FROM inbound_requests")
        await db.disconnect()


class TestPostgresInbox:
    async def test_accepts_a_request_once_and_refuses_its_redelivery(self, store):
        # Given a request that has been accepted
        request = _request("Ev1", entities.SlackThread(channel_id="C1", thread_ts="1700.5"))
        first_delivery = await store.accept(request)

        # When the same request is delivered again
        redelivery = await store.accept(request)

        # Then only the first delivery is accepted
        assert (first_delivery, redelivery) == (True, False)

    async def test_lists_unfinished_requests_until_they_finish(self, store):
        # Given a Slack request and a Jira request in flight
        slack_request = _request("Ev1", entities.SlackThread(channel_id="C1", thread_ts="1700.5"))
        ticket_request = _request("jira:issue:10001", entities.TicketRef(issue_key="IT-42"))
        await store.accept(slack_request)
        await store.accept(ticket_request)

        # When the Slack request finishes
        await store.finish(slack_request.id)

        # Then only the ticket request is left to replay, round-tripped intact
        assert await store.list_unfinished() == [ticket_request]

    async def test_discards_only_requests_older_than_the_cutoff(self, store):
        # Given one request in flight
        request = _request("Ev1", entities.SlackThread(channel_id="C1", thread_ts="1700.5"))
        await store.accept(request)

        # When stale requests are discarded with a cutoff before, then after, it
        discarded_before = await store.discard_stale(cutoff=DISTANT_PAST)
        still_in_flight = await store.list_unfinished()
        discarded_after = await store.discard_stale(cutoff=FAR_FUTURE)

        # Then the request survives the earlier cutoff and falls to the later one
        assert (discarded_before, still_in_flight, discarded_after) == (0, [request], 1)
        assert await store.list_unfinished() == []

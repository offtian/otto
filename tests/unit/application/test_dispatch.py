import asyncio
import types
from datetime import UTC, datetime, timedelta
from unittest import mock

import attrs
import pytest

from otto import config
from otto.application import dispatch, support
from otto.domain.support import entities, followups


NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)


class FakeInbox:
    """
    In-memory stand-in for the durable inbox: the requests in flight, by id.
    """

    def __init__(self, in_flight=()):
        self.in_flight = {request.id: request for request in in_flight}
        self.stale_cutoffs = []

    async def accept(self, request):
        if request.id in self.in_flight:
            return False
        self.in_flight[request.id] = request
        return True

    async def finish(self, request_id):
        self.in_flight.pop(request_id, None)

    async def discard_stale(self, *, cutoff):
        self.stale_cutoffs.append(cutoff)
        return 0

    async def list_unfinished(self):
        return list(self.in_flight.values())


def _request(request_id: str = "Ev1") -> entities.SupportRequest:
    return entities.SupportRequest(
        id=request_id,
        user_id="U_REQ",
        text="my VPN keeps dropping",
        origin=entities.SlackThread(channel_id="C1", thread_ts="1.0"),
    )


def _wire(monkeypatch, *, inbox, handler=None):
    """
    Stub the wired config with the given inbox and return the spy that
    replaces the support handler.
    """
    monkeypatch.setattr(config, "get_config", lambda: types.SimpleNamespace(inbox=inbox))
    handler = handler or mock.AsyncMock()
    monkeypatch.setattr(support, "handle_support_request", handler)
    return handler


class TestAccept:
    async def test_refuses_a_redelivery_of_a_request_still_in_flight(self, monkeypatch):
        # Given an inbox that already holds the request
        request = _request()
        _wire(monkeypatch, inbox=FakeInbox(in_flight=[request]))

        # When the same request is delivered again
        accepted = await dispatch.accept(request=request)

        # Then it is refused, so it is never handled twice
        assert accepted is False

    async def test_still_handles_the_request_when_the_inbox_is_unreachable(self, monkeypatch):
        # Given an inbox whose insert fails (a database blip)
        inbox = mock.AsyncMock()
        inbox.accept.side_effect = ConnectionError("database unreachable")
        _wire(monkeypatch, inbox=inbox)

        # When a request is accepted
        accepted = await dispatch.accept(request=_request())

        # Then it is handled anyway — durability is an assist, not a gate
        assert accepted is True


class TestHandleSafely:
    async def test_finishes_the_request_even_when_the_handler_crashed(self, monkeypatch):
        # Given an accepted request whose handler dies, and an apology spy
        request = _request()
        inbox = FakeInbox(in_flight=[request])
        _wire(monkeypatch, inbox=inbox, handler=mock.AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(support, "notify_failure", mock.AsyncMock())

        # When the request is handled
        await dispatch.handle_safely(request)

        # Then it leaves the inbox — a poisoned request must not replay on
        # every restart
        assert inbox.in_flight == {}

    async def test_leaves_a_cancelled_run_in_the_inbox(self, monkeypatch):
        # Given an accepted request whose run is cancelled mid-flight (shutdown)
        request = _request()
        inbox = FakeInbox(in_flight=[request])
        _wire(
            monkeypatch,
            inbox=inbox,
            handler=mock.AsyncMock(side_effect=asyncio.CancelledError),
        )

        # When the request is handled
        with pytest.raises(asyncio.CancelledError):
            await dispatch.handle_safely(request)

        # Then it stays in the inbox for the next start to replay
        assert inbox.in_flight == {request.id: request}

    async def test_takes_a_handled_request_out_of_the_inbox(self, monkeypatch):
        # Given an accepted request whose handler succeeds
        request = _request()
        inbox = FakeInbox(in_flight=[request])
        handler = _wire(monkeypatch, inbox=inbox)

        # When the request is handled
        await dispatch.handle_safely(request)

        # Then it was handled once and no longer waits for a replay
        handler.assert_awaited_once_with(request=request)
        assert inbox.in_flight == {}


class TestOneRunPerConversation:
    async def test_a_second_message_waits_for_the_first_run_on_its_conversation(self, monkeypatch):
        # Given a handler that records each run it starts and holds it open
        started: list[str] = []
        release = asyncio.Event()

        async def handler(*, request):
            started.append(request.id)
            await release.wait()

        _wire(monkeypatch, inbox=None, handler=handler)
        other_thread = entities.SlackThread(channel_id="C1", thread_ts="2.0")
        same_thread = [_request("Ev-first"), _request("Ev-second")]
        elsewhere = attrs.evolve(_request("Ev-elsewhere"), origin=other_thread)

        # When two messages arrive on one thread and a third on another
        runs = [
            asyncio.create_task(dispatch.handle_safely(request))
            for request in (*same_thread, elsewhere)
        ]
        for _ in range(3):
            await asyncio.sleep(0)
        started_while_first_runs = list(started)
        release.set()
        await asyncio.gather(*runs)

        # Then the second message did not start until the first run was done,
        # while the other conversation was never held up
        assert started_while_first_runs == ["Ev-first", "Ev-elsewhere"]
        assert started == ["Ev-first", "Ev-elsewhere", "Ev-second"]


class TestUnfinishedRequests:
    async def test_returns_what_a_dead_process_left_in_flight(self, monkeypatch):
        # Given two requests that were in flight when the process died
        requests = [_request("Ev-mid-flight"), _request("jira:issue:10001")]
        inbox = FakeInbox(in_flight=requests)
        _wire(monkeypatch, inbox=inbox)

        # When the next start asks what never finished
        unfinished = await dispatch.unfinished_requests(now=NOW)

        # Then both come back for replay, after anything older than the
        # replay window was discarded
        assert unfinished == requests
        assert inbox.stale_cutoffs == [NOW - timedelta(hours=24)]


class FakeFollowUpStore:
    def __init__(self, due):
        self._due = list(due)
        self.fired = []

    async def due(self, *, now):
        return list(self._due)

    async def mark_fired(self, follow_up_id):
        self.fired.append(follow_up_id)


class TestFireDueFollowUps:
    FOLLOW_UP = followups.FollowUp(
        id=7,
        origin=entities.SlackThread(channel_id="C1", thread_ts="1.0"),
        requester_id="U_REQ",
        note="check Jenkins build 4521",
    )

    def _wire(self, monkeypatch, *, inbox):
        store = FakeFollowUpStore(due=[self.FOLLOW_UP])
        monkeypatch.setattr(
            config, "get_config", lambda: types.SimpleNamespace(inbox=inbox, follow_ups=store)
        )
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        return store, handler

    async def test_wakes_the_conversation_with_the_agents_own_note(self, monkeypatch):
        # Given a follow-up that has come due
        store, handler = self._wire(monkeypatch, inbox=FakeInbox())

        # When the always-on tick fires due follow-ups
        await dispatch.fire_due_follow_ups(now=NOW)

        # Then the conversation gets a request on the requester's behalf that
        # carries the note, and the follow-up is marked fired
        request = handler.await_args.kwargs["request"]
        assert (request.id, request.user_id, request.origin) == (
            "followup:7",
            "U_REQ",
            self.FOLLOW_UP.origin,
        )
        assert "check Jenkins build 4521" in request.text
        assert store.fired == [7]

    async def test_does_not_run_a_follow_up_that_is_already_in_flight(self, monkeypatch):
        # Given a follow-up whose request a crashed process already accepted
        in_flight = _request("followup:7")
        store, handler = self._wire(monkeypatch, inbox=FakeInbox(in_flight=[in_flight]))

        # When the tick sees it still pending
        await dispatch.fire_due_follow_ups(now=NOW)

        # Then it is marked fired without a second run
        assert handler.await_count == 0
        assert store.fired == [7]

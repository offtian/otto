import types
from datetime import UTC, datetime, timedelta
from unittest import mock

from otto import config
from otto.application import delivery
from otto.domain.support import entities, outbox


NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
THREAD = entities.SlackThread(channel_id="C1", thread_ts="1.0")


class FakeOutbox:
    """
    In-memory stand-in for the outbox: what was recorded, and how each
    delivery attempt was booked.
    """

    def __init__(self, due=()):
        self.recorded = {}  # key -> OutboundMessage
        self.due = list(due)
        self.sent = []  # message ids
        self.failures = []  # (message id, error, retry_in)

    async def enqueue(self, *, key, origin, text, vote):
        if key in self.recorded:
            return None
        message = outbox.OutboundMessage(
            id=len(self.recorded) + 1, key=key, origin=origin, text=text, vote=vote, attempts=0
        )
        self.recorded[key] = message
        return message

    async def claim_due(self, *, now):
        return list(self.due)

    async def mark_sent(self, message_id):
        self.sent.append(message_id)

    async def record_failure(self, message_id, *, error, retry_in):
        self.failures.append((message_id, error, retry_in))


def _wire(monkeypatch, *, store, jira=None, channel_markup=True):
    """
    Stub the wired config with the given outbox and return the Slack spy.
    """
    slack = mock.AsyncMock()
    cfg = types.SimpleNamespace(
        outbox=store,
        slack=slack,
        jira=jira,
        channel_markup=channel_markup,
        settings=types.SimpleNamespace(slack_triage_channel="C_TRIAGE"),
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    return slack


def _due_message(*, attempts: int) -> outbox.OutboundMessage:
    return outbox.OutboundMessage(
        id=7, key="Ev1", origin=THREAD, text="Try the VPN runbook.", vote=False, attempts=attempts
    )


class TestDeliver:
    async def test_sends_a_recorded_reply_and_marks_it_sent(self, monkeypatch):
        # Given a working channel and an empty outbox
        store = FakeOutbox()
        slack = _wire(monkeypatch, store=store)

        # When a run's answer is delivered
        await delivery.deliver(key="Ev1", origin=THREAD, text="Try the VPN runbook.", vote=True)

        # Then it reached the thread with its vote and is booked as sent
        slack.post_answer.assert_awaited_once_with(
            channel="C1", text="Try the VPN runbook.", thread_ts="1.0", feedback_value="C1:1.0"
        )
        assert store.sent == [1]

    async def test_answers_a_run_only_once(self, monkeypatch):
        # Given a run whose answer was already recorded
        store = FakeOutbox()
        slack = _wire(monkeypatch, store=store)
        await delivery.deliver(key="Ev1", origin=THREAD, text="Try the VPN runbook.")

        # When the run is replayed and answers again
        await delivery.deliver(key="Ev1", origin=THREAD, text="Try the VPN runbook, again.")

        # Then the conversation hears only the first answer
        assert slack.post_message.await_count == 1

    async def test_keeps_a_reply_the_channel_refused_instead_of_raising(self, monkeypatch):
        # Given a channel that refuses the send
        store = FakeOutbox()
        slack = _wire(monkeypatch, store=store)
        slack.post_message.side_effect = ConnectionError("slack is down")

        # When a reply is delivered
        await delivery.deliver(key="Ev1", origin=THREAD, text="Try the VPN runbook.")

        # Then nothing is raised and the reply waits for its first retry
        assert store.failures == [(1, "ConnectionError", timedelta(seconds=10))]
        assert store.sent == []

    async def test_still_sends_when_the_outbox_cannot_record(self, monkeypatch):
        # Given an outbox whose insert fails (a database blip)
        store = mock.AsyncMock()
        store.enqueue.side_effect = ConnectionError("database unreachable")
        slack = _wire(monkeypatch, store=store)

        # When a reply is delivered
        await delivery.deliver(key="Ev1", origin=THREAD, text="Try the VPN runbook.")

        # Then it is sent anyway — the outbox is an assist, not a gate
        assert slack.post_message.await_count == 1


class TestSend:
    MARKDOWN = "**Restart** the client — see [the runbook](https://kb.example.com/vpn)"

    async def test_renders_a_reply_into_slacks_markup(self, monkeypatch):
        # Given a Slack conversation and a reply written in Markdown
        slack = _wire(monkeypatch, store=None)

        # When it is sent
        await delivery.send(origin=THREAD, text=self.MARKDOWN)

        # Then Slack receives its own bold and link syntax
        assert slack.post_message.await_args.kwargs["text"] == (
            "*Restart* the client — see <https://kb.example.com/vpn|the runbook>"
        )

    async def test_renders_a_reply_into_jiras_markup(self, monkeypatch):
        # Given a ticket and a reply written in Markdown
        jira = mock.AsyncMock()
        _wire(monkeypatch, store=None, jira=jira)

        # When it is sent
        await delivery.send(origin=entities.TicketRef(issue_key="IT-7"), text=self.MARKDOWN)

        # Then the comment uses Jira's link syntax, not Slack's
        jira.post_comment.assert_awaited_once_with(
            issue_key="IT-7",
            text="*Restart* the client — see [the runbook|https://kb.example.com/vpn]",
        )

    async def test_leaves_markdown_alone_where_the_surface_displays_it(self, monkeypatch):
        # Given a surface that shows Markdown itself (the dev chat)
        slack = _wire(monkeypatch, store=None, channel_markup=False)

        # When a reply is sent
        await delivery.send(origin=THREAD, text=self.MARKDOWN)

        # Then it arrives as written
        assert slack.post_message.await_args.kwargs["text"] == self.MARKDOWN


class TestRetryDue:
    async def test_delivers_a_reply_that_became_due(self, monkeypatch):
        # Given a reply that failed once and is due again, and a channel that
        # has recovered
        store = FakeOutbox(due=[_due_message(attempts=1)])
        slack = _wire(monkeypatch, store=store)

        # When the retry tick runs
        await delivery.retry_due(now=NOW)

        # Then the reply reaches its thread and is booked as sent
        slack.post_message.assert_awaited_once_with(
            channel="C1", text="Try the VPN runbook.", thread_ts="1.0"
        )
        assert store.sent == [7]

    async def test_gives_up_on_the_fifth_failure_and_tells_triage(self, monkeypatch):
        # Given a reply that has failed four times, and a channel that still
        # refuses that thread but accepts the triage notice
        store = FakeOutbox(due=[_due_message(attempts=4)])
        slack = _wire(monkeypatch, store=store)
        slack.post_message.side_effect = [ConnectionError("still down"), "200.1"]

        # When the retry tick runs
        await delivery.retry_due(now=NOW)

        # Then the reply is marked dead and a human is asked to follow up
        assert store.failures == [(7, "ConnectionError", None)]
        notice = slack.post_message.await_args_list[1].kwargs
        assert notice["channel"] == "C_TRIAGE"
        assert "https://slack.com/archives/C1/p10" in notice["text"]

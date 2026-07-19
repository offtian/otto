import json
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi import testclient
from slack_sdk.signature import SignatureVerifier

from otto import config
from otto.application import killswitch, support
from otto.domain.identity import users
from otto.domain.support import approvals, entities
from otto.interfaces import app as otto_app
from otto.settings import Settings
from otto.utils import logs


SIGNING_SECRET = "test-signing-secret"
JIRA_WEBHOOK_SECRET = "test-jira-webhook-secret"


def _signed_headers(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = SignatureVerifier(SIGNING_SECRET).generate_signature(
        timestamp=timestamp, body=body.decode("utf-8")
    )
    return {"x-slack-request-timestamp": timestamp, "x-slack-signature": signature}


def _event_payload(event_id: str = "Ev1", **event_overrides) -> bytes:
    event = {
        "type": "app_mention",
        "user": "U_REQ",
        "text": "<@BOT> help",
        "channel": "C1",
        "ts": "1.0",
        **event_overrides,
    }
    return json.dumps({"type": "event_callback", "event_id": event_id, "event": event}).encode()


@pytest.fixture
def client(monkeypatch):
    settings = Settings(
        _env_file=None,
        slack_signing_secret=SIGNING_SECRET,
        slack_triage_channel="C_TRIAGE",
        support_user_ids="U_SUPPORT",
        jira_webhook_secret=JIRA_WEBHOOK_SECRET,
    )
    jira = mock.AsyncMock()
    jira.get_myself_account_id.return_value = "OTTO_BOT"
    cfg = config.Configuration(
        settings=settings,
        slack=mock.AsyncMock(),
        triage=mock.AsyncMock(),
        jira=jira,
        directory=users.UserDirectory(users=[]),
        approvals=approvals.InMemoryApprovalStore(),
        model=object(),
        confluence_mcp=None,
        sailpoint_mcp=None,
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    otto_app.app.state.recent_events = otto_app._RecentIds()
    otto_app.app.state.rate_limiter = otto_app._RateLimiter(
        per_minute=settings.slack_user_rate_limit_per_minute
    )
    otto_app.app.state.jira_rate_limiter = otto_app._RateLimiter(
        per_minute=settings.jira_events_per_minute
    )
    otto_app.app.state.kill_switch = killswitch.KillSwitch()
    otto_app.app.state.jira_bot_account_id = None
    return testclient.TestClient(otto_app.app), cfg


class TestSlackEvents:
    def test_rejects_a_forged_signature(self, client):
        # Given an event body signed with the wrong secret
        http, _ = client
        body = _event_payload()

        # When it is posted with a bogus signature
        response = http.post(
            "/slack/events",
            content=body,
            headers={
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=bogus",
            },
        )

        # Then the request is rejected before any handling
        assert response.status_code == 401

    def test_answers_the_url_verification_challenge(self, client):
        # Given a signed url_verification payload
        http, _ = client
        body = json.dumps({"type": "url_verification", "challenge": "chal-123"}).encode()

        # When it is posted with a valid signature
        response = http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then Slack gets its challenge echoed back
        assert response.status_code == 200
        assert response.json() == {"challenge": "chal-123"}

    def test_processes_a_duplicate_event_only_once(self, client, monkeypatch):
        # Given a handler spy and the same event delivered twice (Slack retry)
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _event_payload(event_id="Ev-dup")

        # When both deliveries arrive
        http.post("/slack/events", content=body, headers=_signed_headers(body))
        http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then the request is handled exactly once
        assert handler.await_count == 1

    def test_ignores_bot_messages_to_prevent_reply_loops(self, client, monkeypatch):
        # Given a handler spy and an event authored by a bot
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _event_payload(bot_id="B_OTTO")

        # When the event arrives
        response = http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then it is acked but never handled
        assert response.status_code == 200
        assert handler.await_count == 0

    def test_kill_switch_acks_without_handling(self, client, monkeypatch):
        # Given the kill switch is off
        http, cfg = client
        cfg.settings.otto_enabled = False
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _event_payload(event_id="Ev-kill")

        # When an otherwise-valid event arrives
        response = http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then Slack gets its ack but Otto stays silent
        assert response.status_code == 200
        assert handler.await_count == 0

    def test_normalizes_a_mention_into_a_support_request(self, client, monkeypatch):
        # Given a handler spy
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _event_payload(event_id="Ev-norm")

        # When a channel mention arrives
        http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then the handler receives the channel-neutral request shape
        handler.assert_awaited_once_with(
            request=entities.SupportRequest(
                id="Ev-norm",
                user_id="U_REQ",
                text="<@BOT> help",
                origin=entities.SlackThread(channel_id="C1", thread_ts="1.0"),
            )
        )

    def test_crashed_handler_still_notifies_the_user(self, client, monkeypatch):
        # Given a handler that dies after the ack (FR8)
        http, _ = client
        monkeypatch.setattr(
            support, "handle_support_request", mock.AsyncMock(side_effect=RuntimeError("boom"))
        )
        apology = mock.AsyncMock()
        monkeypatch.setattr(support, "notify_failure", apology)
        body = _event_payload(event_id="Ev-crash")

        # When the event arrives and the background handler crashes
        response = http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then the user still gets an origin-visible apology
        assert response.status_code == 200
        assert apology.await_count == 1

    def test_greets_a_new_assistant_thread(self, client, monkeypatch):
        # Given a greeting spy and an assistant_thread_started event (agent-mode)
        http, _ = client
        greet = mock.AsyncMock()
        monkeypatch.setattr(support, "greet_assistant_thread", greet)
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev-assist",
                "event": {
                    "type": "assistant_thread_started",
                    "assistant_thread": {"channel_id": "D1", "thread_ts": "1.0"},
                },
            }
        ).encode()

        # When the event arrives
        response = http.post("/slack/events", content=body, headers=_signed_headers(body))

        # Then Otto greets that thread (welcome + suggested prompts)
        assert response.status_code == 200
        greet.assert_awaited_once_with(channel="D1", thread_ts="1.0")

    def test_rate_limited_user_is_dropped(self, client, monkeypatch):
        # Given a handler spy and a cap of one request per minute
        http, _ = client
        otto_app.app.state.rate_limiter = otto_app._RateLimiter(per_minute=1)
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When the same user sends two distinct requests inside the window
        http.post(
            "/slack/events",
            content=_event_payload(event_id="Ev-r1"),
            headers=_signed_headers(_event_payload(event_id="Ev-r1")),
        )
        http.post(
            "/slack/events",
            content=_event_payload(event_id="Ev-r2"),
            headers=_signed_headers(_event_payload(event_id="Ev-r2")),
        )

        # Then only the first is handled — the second is acked and dropped
        assert handler.await_count == 1


class TestSlackInteractions:
    def _interaction_body(
        self, action_id: str = "otto_approval_approve", value: str = "ap-1"
    ) -> bytes:
        payload = {
            "type": "block_actions",
            "user": {"id": "U_SUPPORT"},
            "actions": [{"action_id": action_id, "value": value}],
        }
        return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()

    def _post_interaction(self, http, body: bytes):
        return http.post(
            "/slack/interactions",
            content=body,
            headers={**_signed_headers(body), "content-type": "application/x-www-form-urlencoded"},
        )

    def test_dispatches_an_approve_click(self, client, monkeypatch):
        # Given a resolver spy
        http, _ = client
        resolver = mock.AsyncMock()
        monkeypatch.setattr(support, "resolve_approval", resolver)
        body = self._interaction_body()

        # When an approve click arrives, signed
        response = http.post(
            "/slack/interactions",
            content=body,
            headers={**_signed_headers(body), "content-type": "application/x-www-form-urlencoded"},
        )

        # Then the decision reaches the application layer
        assert response.status_code == 200
        resolver.assert_awaited_once_with(
            approval_id="ap-1", resolver_id="U_SUPPORT", approved=True
        )

    def test_ignores_unknown_action_ids(self, client, monkeypatch):
        # Given a resolver spy and a click on some unrelated block
        http, _ = client
        resolver = mock.AsyncMock()
        monkeypatch.setattr(support, "resolve_approval", resolver)
        body = self._interaction_body(action_id="something_else")

        # When the interaction arrives
        response = http.post(
            "/slack/interactions",
            content=body,
            headers={**_signed_headers(body), "content-type": "application/x-www-form-urlencoded"},
        )

        # Then it is acked and dropped
        assert response.status_code == 200
        assert resolver.await_count == 0

    def test_dispatches_a_helpful_vote(self, client, monkeypatch):
        # Given a feedback spy
        http, _ = client
        recorder = mock.AsyncMock()
        monkeypatch.setattr(support, "record_feedback", recorder)
        body = self._interaction_body(action_id="otto_feedback_yes", value="C1:1.0")

        # When a "yes" vote arrives, signed
        response = self._post_interaction(http, body)

        # Then it reaches the application layer as a Slack-thread origin
        assert response.status_code == 200
        recorder.assert_awaited_once_with(
            helpful=True,
            origin=entities.SlackThread(channel_id="C1", thread_ts="1.0"),
            voter_id="U_SUPPORT",
        )

    def test_dispatches_a_no_vote(self, client, monkeypatch):
        # Given a feedback spy
        http, _ = client
        recorder = mock.AsyncMock()
        monkeypatch.setattr(support, "record_feedback", recorder)
        body = self._interaction_body(action_id="otto_feedback_no", value="C1:1.0")

        # When a "no" vote arrives
        self._post_interaction(http, body)

        # Then the not-helpful vote reaches the application layer
        recorder.assert_awaited_once_with(
            helpful=False,
            origin=entities.SlackThread(channel_id="C1", thread_ts="1.0"),
            voter_id="U_SUPPORT",
        )

    def test_dispatches_an_escalation_resolve_click(self, client, monkeypatch):
        # Given a resolve spy and a click carrying the escalation card coordinates
        http, _ = client
        resolver = mock.AsyncMock()
        monkeypatch.setattr(support, "mark_resolved", resolver)
        payload = {
            "type": "block_actions",
            "user": {"id": "U_SUPPORT"},
            "channel": {"id": "C_TRIAGE"},
            "message": {"ts": "300.1"},
            "actions": [{"action_id": "otto_escalation_resolve", "value": "IT-7"}],
        }
        body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()

        # When the click arrives
        response = self._post_interaction(http, body)

        # Then it reaches the application layer with the origin and card coordinates
        assert response.status_code == 200
        resolver.assert_awaited_once_with(
            origin_ref="IT-7",
            resolver_id="U_SUPPORT",
            card_channel="C_TRIAGE",
            card_ts="300.1",
        )


def _jira_issue_payload(issue_id: str = "10001") -> bytes:
    return json.dumps(
        {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "id": issue_id,
                "key": "IT-1",
                "fields": {
                    "summary": "VPN down",
                    "description": "It fails on hotel wifi",
                    "reporter": {"accountId": "USER_ACCT"},
                },
            },
        }
    ).encode()


def _jira_comment_payload(comment_id: str = "20001", author: str = "USER_ACCT") -> bytes:
    return json.dumps(
        {
            "webhookEvent": "comment_created",
            "issue": {"id": "10001", "key": "IT-1"},
            "comment": {"id": comment_id, "author": {"accountId": author}, "body": "any update?"},
        }
    ).encode()


def _jira_transition_payload(
    *, category: str = "done", changelog_id: str = "cl-1", status_change: bool = True
) -> bytes:
    item = {"field": "status" if status_change else "assignee", "toString": "Done"}
    return json.dumps(
        {
            "webhookEvent": "jira:issue_updated",
            "changelog": {"id": changelog_id, "items": [item]},
            "issue": {
                "id": "10001",
                "key": "IT-1",
                "fields": {"status": {"name": "Done", "statusCategory": {"key": category}}},
            },
        }
    ).encode()


JIRA_URL = f"/jira/webhook?secret={JIRA_WEBHOOK_SECRET}"


class TestJiraWebhook:
    def test_rejects_a_wrong_secret(self, client):
        # Given a valid payload sent with the wrong webhook secret
        http, _ = client

        # When it is posted
        response = http.post("/jira/webhook?secret=wrong", content=_jira_issue_payload())

        # Then the request is rejected before any handling
        assert response.status_code == 401

    def test_normalizes_a_new_issue_into_a_support_request(self, client, monkeypatch):
        # Given a handler spy
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When an issue-created event arrives with the shared secret
        response = http.post(JIRA_URL, content=_jira_issue_payload())

        # Then the handler receives the channel-neutral request shape
        assert response.status_code == 200
        handler.assert_awaited_once_with(
            request=entities.SupportRequest(
                id="jira:issue:10001",
                user_id="USER_ACCT",
                text="VPN down\n\nIt fails on hotel wifi",
                origin=entities.TicketRef(issue_key="IT-1"),
            )
        )

    def test_a_webhook_storm_is_capped_before_the_llm(self, client, monkeypatch):
        # Given a handler spy and a global Jira cap of 2 events per minute (C2)
        http, _ = client
        otto_app.app.state.jira_rate_limiter = otto_app._RateLimiter(per_minute=2)
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When a burst of distinct issue-created events arrives
        for issue_id in ("30001", "30002", "30003", "30004"):
            response = http.post(JIRA_URL, content=_jira_issue_payload(issue_id=issue_id))
            # Then every event is still acked — Jira must not retry
            assert response.status_code == 200

        # Then only the capped number were dispatched; the rest were dropped
        assert handler.await_count == 2

    def test_dispatches_a_user_comment(self, client, monkeypatch):
        # Given a handler spy
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When a comment-created event from a human arrives
        http.post(JIRA_URL, content=_jira_comment_payload())

        # Then it is handled as a ticket-origin request
        handler.assert_awaited_once_with(
            request=entities.SupportRequest(
                id="jira:comment:20001",
                user_id="USER_ACCT",
                text="any update?",
                origin=entities.TicketRef(issue_key="IT-1"),
            )
        )

    def test_ignores_ottos_own_comments_to_prevent_reply_loops(self, client, monkeypatch):
        # Given a handler spy and a comment authored by Otto's own account
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _jira_comment_payload(author="OTTO_BOT")

        # When the webhook event for Otto's own comment arrives
        response = http.post(JIRA_URL, content=body)

        # Then it is acked but never handled — no reply loop
        assert response.status_code == 200
        assert handler.await_count == 0

    def test_fails_closed_when_the_bot_identity_is_unavailable(self, client, monkeypatch):
        # Given the myself lookup fails, so Otto cannot tell its own events apart
        http, cfg = client
        cfg.jira.get_myself_account_id.side_effect = RuntimeError("jira down")
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When a human comment arrives
        response = http.post(JIRA_URL, content=_jira_comment_payload())

        # Then it is acked but not handled — the loop guard fails closed
        assert response.status_code == 200
        assert handler.await_count == 0

    def test_processes_a_duplicate_delivery_only_once(self, client, monkeypatch):
        # Given a handler spy and the same event delivered twice
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        body = _jira_issue_payload(issue_id="10002")

        # When both deliveries arrive
        http.post(JIRA_URL, content=body)
        http.post(JIRA_URL, content=body)

        # Then the request is handled exactly once
        assert handler.await_count == 1

    def test_kill_switch_acks_without_handling(self, client, monkeypatch):
        # Given the kill switch is off
        http, cfg = client
        cfg.settings.otto_enabled = False
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)

        # When an otherwise-valid event arrives
        response = http.post(JIRA_URL, content=_jira_issue_payload(issue_id="10003"))

        # Then Jira gets its ack but Otto stays silent
        assert response.status_code == 200
        assert handler.await_count == 0

    def test_records_a_resolution_when_a_ticket_reaches_a_done_status(self, client, monkeypatch):
        # Given resolution-log and handler spies
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        events = mock.Mock()
        monkeypatch.setattr(logs, "log_event", events)

        # When a ticket transitions into a done-category status
        response = http.post(JIRA_URL, content=_jira_transition_payload())

        # Then a native resolution signal is recorded and the agent is not re-run
        assert response.status_code == 200
        assert handler.await_count == 0
        assert any(
            call.args[0] == "request_resolved"
            and call.kwargs["params"]["signal"] == "ticket_status"
            for call in events.call_args_list
        )

    def test_records_a_resolution_only_once(self, client, monkeypatch):
        # Given a resolution-log spy and the same transition delivered twice
        http, _ = client
        events = mock.Mock()
        monkeypatch.setattr(logs, "log_event", events)
        body = _jira_transition_payload(changelog_id="cl-dup")

        # When both deliveries arrive (Jira redelivery)
        http.post(JIRA_URL, content=body)
        http.post(JIRA_URL, content=body)

        # Then the resolution is counted exactly once
        resolutions = [c for c in events.call_args_list if c.args[0] == "request_resolved"]
        assert len(resolutions) == 1

    def test_ignores_a_transition_to_a_non_done_status(self, client, monkeypatch):
        # Given a resolution-log spy
        http, _ = client
        events = mock.Mock()
        monkeypatch.setattr(logs, "log_event", events)

        # When a ticket moves to an in-progress (non-done) status
        response = http.post(JIRA_URL, content=_jira_transition_payload(category="indeterminate"))

        # Then no resolution is recorded — only terminal statuses resolve
        assert response.status_code == 200
        assert not any(c.args[0] == "request_resolved" for c in events.call_args_list)

    def test_ignores_a_non_status_field_update(self, client, monkeypatch):
        # Given resolution-log and handler spies
        http, _ = client
        handler = mock.AsyncMock()
        monkeypatch.setattr(support, "handle_support_request", handler)
        events = mock.Mock()
        monkeypatch.setattr(logs, "log_event", events)

        # When an issue is edited without a status change (e.g. reassigned)
        response = http.post(JIRA_URL, content=_jira_transition_payload(status_change=False))

        # Then it is a no-op: no resolution recorded and no agent run
        assert response.status_code == 200
        assert not any(c.args[0] == "request_resolved" for c in events.call_args_list)
        assert handler.await_count == 0


class TestHealthz:
    def test_reports_ok(self, client):
        # Given the app is up
        http, _ = client

        # When the health endpoint is hit
        response = http.get("/healthz")

        # Then it reports ok
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestRateLimiter:
    _NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_allows_up_to_the_cap_then_blocks_within_the_window(self):
        # Given a limiter capped at two requests per minute
        limiter = otto_app._RateLimiter(per_minute=2)

        # When one user makes three requests inside the same minute
        verdicts = [limiter.allow("U", now=self._NOW) for _ in range(3)]

        # Then the third is blocked — the first two are under the cap
        assert verdicts == [True, True, False]

    def test_forgets_hits_older_than_a_minute(self):
        # Given a limiter at one per minute whose window is already used up
        limiter = otto_app._RateLimiter(per_minute=1)
        assert limiter.allow("U", now=self._NOW) is True

        # When the next request comes just over a minute later
        # Then the window has rolled and the user is allowed again
        assert limiter.allow("U", now=self._NOW + timedelta(seconds=61)) is True

    def test_a_zero_cap_disables_the_limit(self):
        # Given a limiter with the cap disabled
        limiter = otto_app._RateLimiter(per_minute=0)

        # When a user makes many requests
        # Then all are allowed
        assert all(limiter.allow("U", now=self._NOW) for _ in range(100))

    def test_limits_are_counted_per_user(self):
        # Given a limiter at one per minute and one user already at the cap
        limiter = otto_app._RateLimiter(per_minute=1)
        assert limiter.allow("U_one", now=self._NOW) is True

        # When a different user makes their first request
        # Then it is allowed — the cap is per user
        assert limiter.allow("U_two", now=self._NOW) is True

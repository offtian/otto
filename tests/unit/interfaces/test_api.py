import json
import time
import urllib.parse
from unittest import mock

import pytest
from fastapi import testclient
from slack_sdk.signature import SignatureVerifier

from otto import config
from otto.application import support
from otto.domain.support import approvals, entities
from otto.interfaces import api
from otto.settings import Settings


SIGNING_SECRET = "test-signing-secret"


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
    )
    cfg = config.Configuration(
        settings=settings,
        slack=mock.AsyncMock(),
        triage=mock.AsyncMock(),
        approvals=approvals.InMemoryApprovalStore(),
        model=object(),
        confluence_mcp=None,
        sailpoint_mcp=None,
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    api.app.state.recent_events = api._RecentIds()
    return testclient.TestClient(api.app), cfg


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


class TestSlackInteractions:
    def _interaction_body(self, action_id: str = "otto_approval_approve") -> bytes:
        payload = {
            "type": "block_actions",
            "user": {"id": "U_SUPPORT"},
            "actions": [{"action_id": action_id, "value": "ap-1"}],
        }
        return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()

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


class TestHealthz:
    def test_reports_ok(self, client):
        # Given the app is up
        http, _ = client

        # When the health endpoint is hit
        response = http.get("/healthz")

        # Then it reports ok
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

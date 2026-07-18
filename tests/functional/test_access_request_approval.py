"""
End-to-end HITL flow (FR4/FR6/FR7): an access request pauses the run, posts
an approval card, and a role-checked click resumes it with the outcome
delivered at the origin. Only the model and Slack are faked — the agent
loop, RunState serialization (NFR6), store, and use-cases are all real.
"""

import json
from datetime import UTC, datetime
from unittest import mock

import pytest
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from otto import config
from otto.application import support
from otto.domain.identity import users
from otto.domain.support import agent as support_agent
from otto.domain.support import approvals, entities
from otto.settings import Settings
from otto.vendors import slack as slack_vendor


def _text(message: str):
    return ResponseOutputMessage(
        id="msg-1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=message, annotations=[])],
    )


def _access_tool_call():
    return ResponseFunctionToolCall(
        id="fc-1",
        call_id="call-1",
        type="function_call",
        name="submit_access_request",
        arguments=json.dumps(
            {"system": "snowflake", "entitlement": "reporting", "justification": "quarterly"}
        ),
    )


class ScriptedModel(Model):
    """
    Deterministic model: returns the scripted turns in order and records
    every input it was called with.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    async def get_response(
        self,
        system_instructions,
        input,  # noqa: A002 - SDK interface name
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        self.inputs.append(input)
        return ModelResponse(output=self.turns.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class FakeSlackGateway:
    def __init__(self):
        self.messages = []  # (channel, thread_ts, text)
        self.answers = []  # (channel, thread_ts, text, feedback_value)
        self.escalations = []  # (channel, text, resolve_value)
        self.cards = []
        self.updates = []
        self.statuses = []  # (channel, thread_ts, status)
        self.prompts = []  # (channel, thread_ts, title, prompts)

    async def post_message(self, *, channel, text, thread_ts=None):
        self.messages.append((channel, thread_ts, text))
        return "100.1"

    async def set_status(self, *, channel, thread_ts, status):
        self.statuses.append((channel, thread_ts, status))

    async def set_suggested_prompts(self, *, channel, thread_ts, title, prompts):
        self.prompts.append((channel, thread_ts, title, prompts))

    async def post_answer(self, *, channel, text, thread_ts, feedback_value):
        self.answers.append((channel, thread_ts, text, feedback_value))
        return "100.2"

    async def post_escalation(self, *, channel, text, resolve_value):
        self.escalations.append((channel, text, resolve_value))
        return "300.1"

    async def update_message(self, *, channel, ts, text):
        self.updates.append((channel, ts, text))

    async def post_approval_card(self, *, channel, approval_id, requester, tool_name, summary):
        self.cards.append(
            {
                "channel": channel,
                "approval_id": approval_id,
                "requester": requester,
                "tool_name": tool_name,
                "summary": summary,
            }
        )
        return "200.1"

    async def fetch_thread(self, *, channel, thread_ts, limit):
        return []


class FakeJiraGateway:
    def __init__(self, conversation=None):
        self.comments = []  # (issue_key, text)
        self.conversation = conversation or []

    async def post_comment(self, *, issue_key, text):
        self.comments.append((issue_key, text))
        return "10001"

    async def fetch_conversation(self, *, issue_key, limit):
        return self.conversation[-limit:]

    async def get_myself_account_id(self):
        return "OTTO_BOT"


ORIGIN = entities.SlackThread(channel_id="D1", thread_ts="1.0")
TICKET_ORIGIN = entities.TicketRef(issue_key="IT-7")


@pytest.fixture
def wire(monkeypatch):
    """
    Wire a full Configuration around a scripted model + fake Slack (and
    optionally fake Jira) and return (config, gateway, model).
    """

    def _wire(model, jira=None, directory=(), **settings_overrides):
        gateway = FakeSlackGateway()
        cfg = config.Configuration(
            settings=Settings(
                _env_file=None,
                slack_triage_channel="C_TRIAGE",
                support_user_ids="U_SUPPORT",
                admin_user_ids="U_ADMIN",
                **settings_overrides,
            ),
            slack=gateway,
            triage=slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE"),
            jira=jira,
            directory=users.UserDirectory(users=directory),
            approvals=approvals.InMemoryApprovalStore(),
            model=model,
            confluence_mcp=None,
            sailpoint_mcp=None,
            agent=support_agent.build_agent(model=model),
        )
        monkeypatch.setattr(config, "get_config", lambda: cfg)
        return cfg, gateway, model

    return _wire


async def _submit_request(requester_id="U_REQ", origin=ORIGIN):
    request = entities.SupportRequest(
        id="Ev-1",
        user_id=requester_id,
        text="I need Snowflake reporting access",
        origin=origin,
    )
    await support.handle_support_request(request=request)
    return request


class TestAccessRequestApproval:
    async def test_sensitive_tool_pauses_and_posts_an_approval_card(self, wire):
        # Given a model that asks for the gated access tool
        cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]))

        # When the request is handled
        await _submit_request()

        # Then a card lands in the triage channel and the user is told to wait
        assert gateway.cards[0]["channel"] == "C_TRIAGE"
        assert gateway.cards[0]["tool_name"] == "submit_access_request"
        assert any("file it on your behalf" in text for _, _, text in gateway.messages)
        # Then the stored approval carries the serialized run state (NFR6)
        pending = await cfg.approvals.get(gateway.cards[0]["approval_id"])
        assert pending.status is approvals.ApprovalStatus.PENDING
        assert pending.run_state_json

    async def test_the_card_shows_the_business_justification_not_raw_json(self, wire):
        # Given a model that asks for access with a stated justification
        _cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]))

        # When the request pauses for approval
        await _submit_request()

        # Then the approver sees the justification the agent collected, framed
        # for a human — the automation decision, not raw arguments
        summary = gateway.cards[0]["summary"]
        assert "Business justification" in summary
        assert "quarterly" in summary

    async def test_a_repeat_request_during_the_gap_posts_no_second_card(self, wire):
        # Given a first access request already paused and awaiting approval
        model = ScriptedModel([[_access_tool_call()], [_access_tool_call()]])
        _cfg, gateway, _ = wire(model)
        await _submit_request()

        # When the same conversation reaches the same gated tool again
        await _submit_request()

        # Then no duplicate card is posted and the requester is told it's
        # already waiting — the earlier paused run is the one that resolves
        assert len(gateway.cards) == 1
        assert any("already waiting" in text for _, _, text in gateway.messages)

    async def test_approval_executes_the_tool_and_reports_at_the_origin(self, wire):
        # Given a paused access request
        model = ScriptedModel(
            [[_access_tool_call()], [_text("Your request was submitted for provisioning.")]]
        )
        cfg, gateway, _ = wire(model)
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]

        # When an authorized approver approves it
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )

        # Then the stub tool actually executed and its output reached the model
        resumed_input = json.dumps(model.inputs[-1], default=str)
        assert "stub" in resumed_input

        # Then the outcome lands in the origin thread and the card is closed out
        assert ("D1", "1.0", "Your request was submitted for provisioning.") in gateway.messages
        assert "U_SUPPORT" in gateway.updates[-1][2]
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.APPROVED

    async def test_approval_records_an_access_resolution_signal(self, wire, monkeypatch):
        # Given a paused access request and a resolution-log spy (D6: SailPoint submission)
        model = ScriptedModel([[_access_tool_call()], [_text("Submitted for provisioning.")]])
        _cfg, gateway, _ = wire(model)
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]
        events = mock.Mock(wraps=support.logs.log_event)
        monkeypatch.setattr(support.logs, "log_event", events)

        # When an authorized approver approves it
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )

        # Then the granted access is recorded as a resolution signal
        assert any(
            call.args[0] == "request_resolved"
            and call.kwargs["params"]["signal"] == "access_granted"
            for call in events.call_args_list
        )

    async def test_denial_never_executes_the_tool(self, wire):
        # Given a paused access request
        model = ScriptedModel(
            [[_access_tool_call()], [_text("Understood — I won't submit that request.")]]
        )
        cfg, gateway, _ = wire(model)
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]

        # When an authorized approver denies it
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_ADMIN", approved=False
        )

        # Then the tool never executed — the model saw a rejection, not a result
        resumed_input = json.dumps(model.inputs[-1], default=str)
        assert "stub" not in resumed_input

        # Then the user is told gracefully at the origin
        assert ("D1", "1.0", "Understood — I won't submit that request.") in gateway.messages
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.DENIED

    async def test_unauthorized_click_changes_nothing(self, wire):
        # Given a paused access request
        cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]))
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]

        # When a user with no approver role clicks approve
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_RANDO", approved=True
        )

        # Then the approval stays pending, the card is untouched, and the
        # clicker gets a polite note under the card
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.PENDING
        assert gateway.updates == []
        assert any(
            thread_ts == "200.1" and "only support/admin" in text
            for _, thread_ts, text in gateway.messages
        )

    async def test_self_approval_is_rejected_even_with_the_right_role(self, wire):
        # Given the requester also holds the support role (T3: prohibit)
        cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]))
        await _submit_request(requester_id="U_SUPPORT")
        approval_id = gateway.cards[0]["approval_id"]

        # When they click approve on their own request
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )

        # Then nothing resolves
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.PENDING
        assert gateway.updates == []

    async def test_double_click_resolves_exactly_once(self, wire):
        # Given an approved access request
        model = ScriptedModel([[_access_tool_call()], [_text("Done.")]])
        _cfg, gateway, model = wire(model)
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )
        run_count_after_first_click = len(model.inputs)

        # When a second approver clicks concurrently-late
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_ADMIN", approved=True
        )

        # Then the run resumed exactly once and the card was updated exactly once
        assert len(model.inputs) == run_count_after_first_click
        assert len(gateway.updates) == 1


class TestAccessRequestApprovalFromTicket:
    async def test_ticket_request_pauses_and_notifies_via_a_comment(self, wire):
        # Given a model that asks for the gated access tool and a Jira-origin request
        jira = FakeJiraGateway()
        _cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]), jira=jira)

        # When the request is handled
        await _submit_request(origin=TICKET_ORIGIN)

        # Then the approval card still lands in Slack (FR7 — cards always live there)
        assert gateway.cards[0]["channel"] == "C_TRIAGE"

        # Then the requester is told to wait on their ticket, not in Slack
        assert any(
            "file it on your behalf" in text for key, text in jira.comments if key == "IT-7"
        )

    async def test_approval_outcome_is_delivered_as_a_ticket_comment(self, wire):
        # Given a paused access request that originated from a ticket
        jira = FakeJiraGateway()
        model = ScriptedModel([[_access_tool_call()], [_text("Submitted for provisioning.")]])
        _cfg, gateway, _ = wire(model, jira=jira)
        await _submit_request(origin=TICKET_ORIGIN)
        approval_id = gateway.cards[0]["approval_id"]

        # When an authorized approver approves it from Slack
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )

        # Then the outcome lands at the origin — as a comment on the ticket
        assert ("IT-7", "Submitted for provisioning.") in jira.comments
        assert "U_SUPPORT" in gateway.updates[-1][2]

    async def test_ticket_history_is_rebuilt_into_the_agent_input(self, wire):
        # Given a ticket with an existing back-and-forth in its comments (D2)
        jira = FakeJiraGateway(
            conversation=[
                ("U_REQ", "My VPN fails on hotel wifi"),
                ("U_HELPER", "Did you restart the client?"),
                ("U_REQ", "Yes, still failing"),
            ]
        )
        model = ScriptedModel([[_text("Let's check the runbook next.")]])
        _cfg, _gateway, model = wire(model, jira=jira)

        # When a follow-up event on that ticket is handled
        await _submit_request(origin=TICKET_ORIGIN)

        # Then the agent sees the reconstructed, untrusted-framed conversation
        first_input = json.dumps(model.inputs[0], default=str)
        assert "Conversation so far" in first_input
        assert "Did you restart the client?" in first_input


class TestKnowledgeAnswerFeedback:
    async def test_a_slack_answer_carries_a_resolution_vote(self, wire):
        # Given a model that returns a plain knowledge answer
        _cfg, gateway, _ = wire(ScriptedModel([[_text("Restart the VPN client.")]]))

        # When a Slack request is answered
        await _submit_request()

        # Then the answer posts with a "did this help?" vote, threaded at the origin
        channel, thread_ts, text, feedback_value = gateway.answers[0]
        assert (channel, thread_ts, text) == ("D1", "1.0", "Restart the VPN client.")
        assert feedback_value == "D1:1.0"

    async def test_a_ticket_answer_has_no_vote(self, wire):
        # Given a Jira-origin request answered with plain text
        jira = FakeJiraGateway()
        _cfg, gateway, _ = wire(ScriptedModel([[_text("Restart the VPN client.")]]), jira=jira)

        # When it is answered
        await _submit_request(origin=TICKET_ORIGIN)

        # Then the answer is a plain ticket comment — Jira has a native resolved signal
        assert gateway.answers == []
        assert ("IT-7", "Restart the VPN client.") in jira.comments

    async def test_a_yes_vote_records_a_resolution(self, wire, monkeypatch):
        # Given a wired config and a resolution-log spy
        _cfg, gateway, _ = wire(ScriptedModel([]))
        events = mock.Mock(wraps=support.logs.log_event)
        monkeypatch.setattr(support.logs, "log_event", events)

        # When the requester votes that the answer helped
        await support.record_feedback(helpful=True, origin=ORIGIN, voter_id="U_REQ")

        # Then a resolution signal is recorded and the requester is thanked at the origin
        assert any(
            call.args[0] == "request_resolved"
            and call.kwargs["params"]["signal"] == "helpful_vote"
            for call in events.call_args_list
        )
        assert any(channel == "D1" and "helped" in text for channel, _, text in gateway.messages)

    async def test_a_no_vote_escalates_to_a_human(self, wire):
        # Given a wired config with a Slack triage backend
        _cfg, gateway, _ = wire(ScriptedModel([]))

        # When the requester votes that the answer did not help
        await support.record_feedback(helpful=False, origin=ORIGIN, voter_id="U_REQ")

        # Then it is escalated to the triage channel — the path forward on a miss
        assert any(
            channel == "C_TRIAGE" and "Escalation" in text
            for channel, text, _ in gateway.escalations
        )
        # Then the requester is told a human will follow up, at their origin
        assert any(channel == "D1" and "human" in text for channel, _, text in gateway.messages)


class TestSupportMarksResolved:
    async def test_escalation_card_carries_a_resolve_button(self, wire):
        # Given an escalation posted to the triage channel
        _cfg, gateway, _ = wire(ScriptedModel([]))
        await support.record_feedback(helpful=False, origin=ORIGIN, voter_id="U_REQ")

        # Then the card offers a "Mark resolved" action carrying the origin ref
        channel, _text, resolve_value = gateway.escalations[0]
        assert channel == "C_TRIAGE"
        assert "D1" in resolve_value

    async def test_authorized_resolve_records_the_signal_and_closes_the_card(
        self, wire, monkeypatch
    ):
        # Given a resolution-log spy
        _cfg, gateway, _ = wire(ScriptedModel([]))
        events = mock.Mock(wraps=support.logs.log_event)
        monkeypatch.setattr(support.logs, "log_event", events)

        # When a support agent marks an escalation resolved
        await support.mark_resolved(
            origin_ref="IT-7", resolver_id="U_SUPPORT", card_channel="C_TRIAGE", card_ts="300.1"
        )

        # Then the agent-marked resolution signal is recorded and the card closed out
        assert any(
            call.args[0] == "request_resolved"
            and call.kwargs["params"]["signal"] == "agent_marked"
            for call in events.call_args_list
        )
        assert gateway.updates[-1][0] == "C_TRIAGE"
        assert "resolved by <@u_support>" in gateway.updates[-1][2].lower()

    async def test_unauthorized_resolve_changes_nothing(self, wire):
        # Given a user with no approver role
        _cfg, gateway, _ = wire(ScriptedModel([]))

        # When they click "Mark resolved"
        await support.mark_resolved(
            origin_ref="IT-7", resolver_id="U_RANDO", card_channel="C_TRIAGE", card_ts="300.1"
        )

        # Then the card is untouched and they get a polite note under it
        assert gateway.updates == []
        assert any(
            thread_ts == "300.1" and "only support/admin" in text
            for _, thread_ts, text in gateway.messages
        )


SAM = users.User(
    name="Sam Support",
    team="IT Support",
    slack_user_id="U_SUPPORT",
    jira_account_id="JIRA_SAM",
)
DANA = users.User(name="Dana Data", team="Data Platform", slack_user_id="U_REQ")


class TestRequesterIdentity:
    async def test_cross_channel_self_approval_is_rejected(self, wire):
        # Given a directory tying Sam's Jira and Slack ids together, and a
        # ticket Sam filed under their Jira account id
        cfg, gateway, _ = wire(
            ScriptedModel([[_access_tool_call()]]),
            jira=FakeJiraGateway(),
            directory=[SAM],
        )
        await _submit_request(requester_id="JIRA_SAM", origin=TICKET_ORIGIN)
        approval_id = gateway.cards[0]["approval_id"]

        # When Sam clicks approve in Slack under their Slack id (which holds
        # the support role)
        await support.resolve_approval(
            approval_id=approval_id, resolver_id="U_SUPPORT", approved=True
        )

        # Then the click is rejected as self-approval — same human, two ids
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.PENDING
        assert gateway.updates == []

    async def test_approval_card_names_the_requester_and_team(self, wire):
        # Given a directory that knows the requester
        _cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]), directory=[DANA])

        # When their request pauses for approval
        await _submit_request(requester_id="U_REQ")

        # Then the card tells the approver who is asking, mention + team
        assert gateway.cards[0]["requester"] == "<@U_REQ> (Data Platform)"

    async def test_agent_input_carries_the_requester_team(self, wire):
        # Given a directory that knows the requester's team
        model = ScriptedModel([[_text("On it.")]])
        _cfg, _gateway, model = wire(model, directory=[DANA])

        # When their request is handled
        await _submit_request(requester_id="U_REQ")

        # Then the agent sees name + team, not a bare channel id
        first_input = json.dumps(model.inputs[0], default=str)
        assert "Dana Data (team: Data Platform)" in first_input


class TestLogContentBoundary:
    async def test_info_logs_never_carry_the_message_text(self, wire, monkeypatch):
        # Given a captured logger and a request whose text is a sensitive sentinel (NFR2)
        _cfg, _gateway, _ = wire(ScriptedModel([[_text("Here is the answer.")]]))
        captured = mock.Mock()
        monkeypatch.setattr(support.logs, "_logger", captured)
        sentinel = "SENSITIVE-VPN-PASSPHRASE-8675309"

        # When a request carrying that text is handled end-to-end
        await support.handle_support_request(
            request=entities.SupportRequest(
                id="Ev-nfr2", user_id="U_REQ", text=sentinel, origin=ORIGIN
            )
        )

        # Then something was logged, but the sentinel appears in no INFO event
        logged = json.dumps(
            [{"args": call.args, "kwargs": call.kwargs} for call in captured.info.call_args_list],
            default=str,
        )
        assert captured.info.call_count > 0
        assert sentinel not in logged


class TestApprovalSweep:
    async def test_an_expired_approval_closes_the_card_and_tells_the_origin(self, wire):
        # Given a pending access request whose approval window has since passed
        cfg, gateway, _ = wire(ScriptedModel([[_access_tool_call()]]))
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]

        # When the maintenance sweep runs far past the expiry window
        await support.sweep_approvals(now=datetime(2999, 1, 1, tzinfo=UTC))

        # Then the approval is terminal — a later click is rejected by the guard
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.EXPIRED
        # Then the triage card is closed out and the requester is told at the origin
        assert any("Expired" in text for _, _, text in gateway.updates)
        assert any(
            channel == "D1" and "expired" in text.lower() for channel, _, text in gateway.messages
        )

    async def test_a_still_pending_approval_gets_a_threaded_reminder(self, wire):
        # Given a pending approval whose expiry window outruns the test clock,
        # so only the reminder fires (created ~now, swept at year 2999)
        cfg, gateway, _ = wire(
            ScriptedModel([[_access_tool_call()]]), approval_expiry_minutes=1_000_000_000
        )
        await _submit_request()
        approval_id = gateway.cards[0]["approval_id"]

        # When the sweep runs past the reminder interval
        await support.sweep_approvals(now=datetime(2999, 1, 1, tzinfo=UTC))

        # Then a reminder is posted under the card and nothing is closed out
        assert any(
            thread_ts == "200.1" and "Still waiting" in text
            for _, thread_ts, text in gateway.messages
        )
        assert gateway.updates == []
        assert (await cfg.approvals.get(approval_id)).status is approvals.ApprovalStatus.PENDING


class TestAssistantMode:
    async def test_greeting_welcomes_and_offers_suggested_prompts(self, wire):
        # Given a wired config and a user opening Otto's assistant pane (3.5)
        _cfg, gateway, _ = wire(ScriptedModel([]))

        # When the assistant thread is greeted
        await support.greet_assistant_thread(channel="D1", thread_ts="1.0")

        # Then Otto welcomes them in the thread and offers starter prompts
        assert any(channel == "D1" and "Otto" in text for channel, _, text in gateway.messages)
        assert gateway.prompts
        assert gateway.prompts[0][0] == "D1"

    async def test_a_slack_request_shows_the_thinking_status(self, wire):
        # Given a wired config answering a Slack (DM) request
        _cfg, gateway, _ = wire(ScriptedModel([[_text("Restart the VPN client.")]]))

        # When the request is handled
        await _submit_request()

        # Then Otto shows the assistant "is working" cue on the thread
        assert gateway.statuses
        assert gateway.statuses[0][0] == "D1"

    async def test_a_ticket_request_sets_no_slack_status(self, wire):
        # Given a Jira-origin request — there is no assistant thread to update
        jira = FakeJiraGateway()
        _cfg, gateway, _ = wire(ScriptedModel([[_text("Restart the VPN client.")]]), jira=jira)

        # When it is handled
        await _submit_request(origin=TICKET_ORIGIN)

        # Then no assistant status is set — the cue is Slack-assistant-only
        assert gateway.statuses == []

"""
End-to-end HITL flow (FR4/FR6/FR7): an access request pauses the run, posts
an approval card, and a role-checked click resumes it with the outcome
delivered at the origin. Only the model and Slack are faked — the agent
loop, RunState serialization (NFR6), store, and use-cases are all real.
"""

import json

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
        name="request_access",
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
        self.cards = []
        self.updates = []

    async def post_message(self, *, channel, text, thread_ts=None):
        self.messages.append((channel, thread_ts, text))
        return "100.1"

    async def update_message(self, *, channel, ts, text):
        self.updates.append((channel, ts, text))

    async def post_approval_card(
        self, *, channel, approval_id, requester_id, tool_name, tool_arguments
    ):
        self.cards.append({"channel": channel, "approval_id": approval_id, "tool_name": tool_name})
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

    def _wire(model, jira=None):
        gateway = FakeSlackGateway()
        cfg = config.Configuration(
            settings=Settings(
                _env_file=None,
                slack_triage_channel="C_TRIAGE",
                support_user_ids="U_SUPPORT",
                admin_user_ids="U_ADMIN",
            ),
            slack=gateway,
            triage=slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE"),
            jira=jira,
            approvals=approvals.InMemoryApprovalStore(),
            model=model,
            confluence_mcp=None,
            sailpoint_mcp=None,
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
        assert gateway.cards[0]["tool_name"] == "request_access"
        assert any("human sign-off" in text for _, _, text in gateway.messages)
        # Then the stored approval carries the serialized run state (NFR6)
        pending = await cfg.approvals.get(gateway.cards[0]["approval_id"])
        assert pending.status is approvals.ApprovalStatus.PENDING
        assert pending.run_state_json

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
        assert any("human sign-off" in text for key, text in jira.comments if key == "IT-7")

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


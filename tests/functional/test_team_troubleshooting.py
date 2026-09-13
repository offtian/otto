"""
End-to-end team-owned flow: a request mentioning a team's service routes to
that team's owner agent, which consults its specialist sub-agents (real
nested agent-as-tool runs — the fanout) and answers at the origin. Only the
model, the classifier, and Slack are faked; the graph, routing, owner and
specialist agents, and the agent loop are all real.
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
from otto.domain.identity import users
from otto.domain.support import agent as support_agent
from otto.domain.support import approvals, entities, teams
from otto.settings import Settings
from otto.vendors import llm
from otto.vendors import slack as slack_vendor


def _text(message: str):
    return ResponseOutputMessage(
        id="msg-1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=message, annotations=[])],
    )


def _tool_call(name: str, arguments: dict):
    return ResponseFunctionToolCall(
        id=f"fc-{name}",
        call_id=f"call-{name}",
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
    )


class ScriptedModel(Model):
    """
    Deterministic model shared by owner and specialists: returns the
    scripted turns in order and records the instructions of every agent
    that called it.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []
        self.system_instructions = []

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
        self.system_instructions.append(system_instructions)
        return ModelResponse(output=self.turns.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class FakeSlackGateway:
    def __init__(self):
        self.messages = []  # (channel, thread_ts, text)
        self.answers = []  # (channel, thread_ts, text, feedback_value)
        self.statuses = []
        self.thread = []

    async def post_message(self, *, channel, text, thread_ts=None):
        self.messages.append((channel, thread_ts, text))
        return "100.1"

    async def post_answer(self, *, channel, text, thread_ts, feedback_value):
        self.answers.append((channel, thread_ts, text, feedback_value))
        return "100.2"

    async def set_status(self, *, channel, thread_ts, status):
        self.statuses.append((channel, thread_ts, status))

    async def fetch_thread(self, *, channel, thread_ts, limit):
        return list(self.thread)[-limit:]


class FakeIntentClassifier:
    def __init__(self, reading):
        self.reading = reading

    async def classify(self, *, text):
        return self.reading


REGISTRY = teams.TeamRegistry(
    teams=(
        teams.TeamProfile(
            name="platform",
            description="Owns Coder and CI",
            instructions="You own developer-platform issues.",
            services=("coder", "jenkins"),
            specialists=("coder", "jenkins"),
        ),
    ),
    specialists={
        "coder": teams.SpecialistProfile(
            name="coder",
            description="Reads Coder workspaces and templates.",
            instructions="You are the Coder specialist.",
        ),
        "jenkins": teams.SpecialistProfile(
            name="jenkins",
            description="Reads Jenkins jobs and builds.",
            instructions="You are the Jenkins specialist.",
        ),
    },
)

ORIGIN = entities.SlackThread(channel_id="D1", thread_ts="1.0")

SPECIALIST_FINDING = "The template pins a deprecated base image."


@pytest.fixture
def wire(monkeypatch):
    """
    Wire a full Configuration around a scripted model, the example team
    registry, and a classifier that routes to the platform team.
    """

    def _wire(model, reading):
        gateway = FakeSlackGateway()
        team_agents = teams.build_team_agents(
            registry=REGISTRY, model=model, specialist_mcp_tools={}
        )
        cfg = config.Configuration(
            settings=Settings(
                _env_file=None,
                slack_triage_channel="C_TRIAGE",
                support_user_ids="U_SUPPORT",
            ),
            slack=gateway,
            triage=slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE"),
            jira=None,
            directory=users.UserDirectory(users=()),
            approvals=approvals.InMemoryApprovalStore(),
            model=model,
            confluence_mcp=None,
            sailpoint_mcp=None,
            agent=support_agent.build_agent(model=model),
            access_agent=support_agent.build_access_agent(model=model),
            intent_classifier=FakeIntentClassifier(reading),
            team_registry=REGISTRY,
            team_agents=team_agents,
        )
        monkeypatch.setattr(config, "get_config", lambda: cfg)
        return cfg, gateway, team_agents

    return _wire


async def _submit_request(text="My Coder template build keeps failing"):
    request = entities.SupportRequest(id="Ev-1", user_id="U_REQ", text=text, origin=ORIGIN)
    await support.handle_support_request(request=request)
    return request


TROUBLESHOOTING_CODER = llm.IntentReading(intent="troubleshooting", services=("coder",))


class TestTeamOwnedTroubleshooting:
    async def test_the_owning_team_agent_consults_its_specialist(self, wire):
        # Given a request routed to platform whose owner consults the Coder
        # specialist once, then answers
        model = ScriptedModel(
            [
                [_tool_call("consult_coder", {"input": "Why does the template build fail?"})],
                [_text(SPECIALIST_FINDING)],
                [_text("Your template pins a deprecated image — bump the base image.")],
            ]
        )
        _cfg, gateway, team_agents = wire(model, TROUBLESHOOTING_CODER)

        # When the request is handled
        await _submit_request()

        # Then the owner ran first, the nested specialist run really executed,
        # and the owner spoke last — three model calls, two agents
        owner_instructions = str(team_agents["platform"].instructions)
        assert model.system_instructions[0] == owner_instructions
        assert "Coder specialist" in str(model.system_instructions[1])
        assert model.system_instructions[2] == owner_instructions

        # Then the specialist's finding fed back into the owner's context
        assert SPECIALIST_FINDING in json.dumps(model.inputs[-1], default=str)

        # Then the final answer lands at the origin with a feedback vote
        channel, thread_ts, text, _vote = gateway.answers[0]
        assert (channel, thread_ts) == ("D1", "1.0")
        assert "deprecated image" in text

    async def test_the_owner_fans_out_to_several_specialists_in_one_turn(self, wire):
        # Given an owner that consults both specialists in a single turn (the
        # SDK runs same-turn tool calls concurrently, so both replies are
        # scripted identically to stay order-independent)
        model = ScriptedModel(
            [
                [
                    _tool_call("consult_coder", {"input": "Check the template"}),
                    _tool_call("consult_jenkins", {"input": "Check the build"}),
                ],
                [_text(SPECIALIST_FINDING)],
                [_text(SPECIALIST_FINDING)],
                [_text("Both point at the template's base image.")],
            ]
        )
        _cfg, gateway, _team_agents = wire(
            model, llm.IntentReading(intent="troubleshooting", services=("coder", "jenkins"))
        )

        # When the request is handled
        await _submit_request()

        # Then both specialist agents really ran between the owner's turns
        consulted = {str(instructions) for instructions in model.system_instructions[1:3]}
        assert any("Coder specialist" in instructions for instructions in consulted)
        assert any("Jenkins specialist" in instructions for instructions in consulted)

        # Then the synthesized answer lands at the origin
        assert "base image" in gateway.answers[0][2]

    async def test_an_unowned_service_still_gets_the_general_agent(self, wire):
        # Given a reading whose service no team owns
        model = ScriptedModel([[_text("Restart the VPN client.")]])
        _cfg, gateway, _team_agents = wire(
            model, llm.IntentReading(intent="troubleshooting", services=("vpn",))
        )

        # When the request is handled
        await _submit_request(text="VPN keeps dropping")

        # Then the general agent answered — team flows never remove capability
        assert model.system_instructions[0] == support_agent.INSTRUCTIONS
        assert gateway.answers

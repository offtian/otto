"""
Deterministic record/replay tests of the Otto agent loop (D5).

Scripted model turns drive real tool selection and execution with no network,
so these gate agent *orchestration* regressions (which tool runs, does its
output feed back, does the run finish) in the normal pytest job. Answer
*quality* is exercised by the live golden evals (`just eval`), never here — a
prompt tweak that changes wording must not fail CI, only a broken loop should.
"""

import json
import pathlib

import agents
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from otto.domain.support import agent as support_agent


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
    Deterministic model: returns the scripted turns in order (each turn is a
    list of output items) and records every input it was called with — the
    replay half of a hand-authored cassette.
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


class RecordingBackend:
    def __init__(self):
        self.escalations = []

    async def escalate(self, *, subject, summary, urgency, requester_id, origin_ref):
        self.escalations.append({"subject": subject, "urgency": urgency})
        return "triage#test"


async def _run(model: ScriptedModel, text: str):
    backend = RecordingBackend()
    agent = support_agent.build_agent(model=model)
    result = await agents.Runner.run(
        agent,
        text,
        context=support_agent.SupportContext(
            requester_id="U_TEST",
            origin_ref="https://slack.com/archives/C/p10",
            runbooks_dir=pathlib.Path("runbooks"),
            ticket_backend=backend,
        ),
    )
    return result, backend


def _fed_back(model: ScriptedModel) -> str:
    """Return the tool output the loop fed into the model's final turn."""
    return json.dumps(model.inputs[-1], default=str)


class TestAgentToolLoops:
    async def test_knowledge_question_runs_search_then_answers(self):
        # Given a scripted run that searches the KB then answers
        model = ScriptedModel(
            [
                [_tool_call("search_knowledge", {"query": "USB drive policy"})],
                [_text("I couldn't verify that — want me to escalate?")],
            ]
        )

        # When Otto handles a knowledge question
        result, _ = await _run(model, "What's the firm's policy on USB drives?")

        # Then search_knowledge ran and its (stub) output fed back into the loop
        assert "No knowledge base is connected" in _fed_back(model)
        assert str(result.final_output) == "I couldn't verify that — want me to escalate?"

    async def test_runbook_question_reads_the_named_runbook(self):
        # Given a scripted run that reads the VPN runbook then walks step one
        model = ScriptedModel(
            [
                [_tool_call("read_runbook", {"name": "vpn"})],
                [_text("Step 1: open the GlobalConnect client.")],
            ]
        )

        # When Otto is asked for VPN help
        result, _ = await _run(model, "walk me through fixing my VPN")

        # Then read_runbook returned the real runbook content into the loop
        assert "GlobalConnect" in _fed_back(model)
        assert result.final_output

    async def test_escalation_reaches_the_ticket_backend(self):
        # Given a scripted run that escalates to a human
        model = ScriptedModel(
            [
                [
                    _tool_call(
                        "escalate_to_human",
                        {
                            "subject": "Laptop won't boot",
                            "summary": "black screen",
                            "urgency": "high",
                        },
                    )
                ],
                [_text("I've raised this with the support team.")],
            ]
        )

        # When Otto handles a request it hands off
        _result, backend = await _run(model, "my laptop won't turn on, I need a person")

        # Then the escalation reached the backend with its structured fields
        assert backend.escalations == [{"subject": "Laptop won't boot", "urgency": "high"}]

    async def test_plain_answer_calls_no_tools(self):
        # Given a scripted run that answers directly, no tool call
        model = ScriptedModel([[_text("I can only help with tech-support questions.")]])

        # When Otto handles an off-topic message
        result, backend = await _run(model, "what's the weather today?")

        # Then the loop finished in one turn with no tool side effects
        assert len(model.inputs) == 1
        assert not result.interruptions
        assert backend.escalations == []

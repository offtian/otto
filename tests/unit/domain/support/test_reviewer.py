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

from otto.domain.support import changes, reviewer


README = "Quotas live in coder/quotas.tfvars; a change must name the team lead."
QUOTAS = "workspace_quotas = {\n  ai = { cpu = 16 }\n}\n"
INFRA = changes.RepoProfile(
    name="acme/infra-live", description="Workspace quotas.", paths=("coder/*",)
)
STAGED = [
    changes.StagedFile(path="coder/quotas.tfvars", before=QUOTAS, after=QUOTAS.replace("16", "32"))
]


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
    Deterministic model: returns the scripted turns in order and records
    every input and the tools it was offered.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []
        self.offered_tools = []

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
        self.offered_tools.append([tool.name for tool in tools])
        return ModelResponse(output=self.turns.pop(0), usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class FakeRepoHost:
    async def read_file(self, *, repo, path, ref):
        return README if path == "README.md" else None

    async def list_directory(self, *, repo, path, ref):
        return ["README.md", "coder/"]


class TestParseReview:
    def test_reads_a_clean_approval(self):
        # Given an answer that approves with nothing to add
        # When it is parsed
        review = reviewer.parse_review("VERDICT: approve", reviewer="any-model")

        # Then the review approves, with no findings, attributed to the model
        assert review == changes.Review(approves=True, findings=(), reviewer="any-model")

    def test_reads_concerns_and_their_findings_past_a_model_thinking_aloud(self):
        # Given an answer that rambles first, then gives the verdict and bullets
        answer = (
            "Let me look at this carefully.\n\n"
            "VERDICT: concerns\n"
            "- Doubles the quota without naming the team lead.\n"
            "• Touches a line the summary does not mention.\n"
        )

        # When it is parsed
        review = reviewer.parse_review(answer, reviewer="any-model")

        # Then only the verdict and the bullets after it count
        assert review.approves is False
        assert review.findings == (
            "Doubles the quota without naming the team lead.",
            "Touches a line the summary does not mention.",
        )

    def test_refuses_an_answer_without_a_verdict(self):
        # Given an answer that never states a verdict
        # When it is parsed
        # Then it is an error — silence must never read as approval
        with pytest.raises(ValueError, match="no verdict"):
            reviewer.parse_review("Looks fine to me!", reviewer="any-model")


class TestAgentChangeReviewer:
    async def test_reads_the_repository_and_returns_its_verdict(self):
        # Given a reviewer model that opens the README, then states a concern
        model = ScriptedModel(
            [
                [_tool_call("read_repo_file", {"repo": "acme/infra-live", "path": "README.md"})],
                [_text("VERDICT: concerns\n- The change does not name the team lead.")],
            ]
        )
        change_reviewer = reviewer.AgentChangeReviewer(
            model=model, model_name="any-model", host=FakeRepoHost()
        )

        # When it reviews a staged change
        review = await change_reviewer.review(
            profile=INFRA,
            title="Raise the AI team's CPU quota",
            summary="Requested for the Q4 training runs.",
            files=STAGED,
        )

        # Then it was shown the title and the diff, could only look — never
        # propose or write — read the README it asked for, and its verdict
        # comes back attributed to its model
        first_input = json.dumps(model.inputs[0], default=str)
        assert "Raise the AI team's CPU quota" in first_input
        assert "+  ai = { cpu = 32 }" in first_input
        assert model.offered_tools[0] == ["list_repo_files", "read_repo_file"]
        assert README in json.dumps(model.inputs[1], default=str)
        assert review == changes.Review(
            approves=False,
            findings=("The change does not name the team lead.",),
            reviewer="any-model",
        )

    async def test_asks_once_more_when_the_answer_carries_no_verdict(self):
        # Given a reviewer model that first answers with nothing at all, then
        # states its verdict
        model = ScriptedModel([[_text("")], [_text("VERDICT: approve")]])
        change_reviewer = reviewer.AgentChangeReviewer(
            model=model, model_name="any-model", host=FakeRepoHost()
        )

        # When it reviews a staged change
        review = await change_reviewer.review(
            profile=INFRA, title="Raise the AI team's CPU quota", summary="", files=STAGED
        )

        # Then the second answer is the review
        assert review.approves
        assert len(model.inputs) == 2

    async def test_gives_up_after_a_second_answer_without_a_verdict(self):
        # Given a reviewer model that never states a verdict
        model = ScriptedModel(
            [[_text("")], [_text("Looks fine to me!")], [_text("VERDICT: approve")]]
        )
        change_reviewer = reviewer.AgentChangeReviewer(
            model=model, model_name="any-model", host=FakeRepoHost()
        )

        # When it reviews a staged change
        # Then that is an error after two answers, not a third question
        with pytest.raises(ValueError, match="no verdict"):
            await change_reviewer.review(
                profile=INFRA, title="Raise the AI team's CPU quota", summary="", files=STAGED
            )
        assert len(model.inputs) == 2

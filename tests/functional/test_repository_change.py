"""
End-to-end repository change: for a provisioning request the general agent
reads a catalogued repository, previews its edit and asks to open a pull
request — the run pauses, the approval card shows the exact edit, and only an
approved click commits it and opens the pull request. Only the model, Slack
and the repository host are faked; the graph, the agent, the change tools,
the approval store and the use-cases are all real.
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
from otto.domain.support import approvals, catalog, changes, entities
from otto.settings import Settings
from otto.vendors import slack as slack_vendor


QUOTAS = """\
workspace_quotas = {
  ai            = { cpu = 16, memory = 64, disk = 200 }
  data-platform = { cpu = 8, memory = 32, disk = 100 }
}
"""
PULL_REQUEST_URL = "https://github.com/acme/infra-live/pull/7"
RAISE_AI_CPU = {
    "path": "coder/quotas.tfvars",
    "old": "ai            = { cpu = 16,",
    "new": "ai            = { cpu = 32,",
}
PROPOSAL = {
    "repo": "acme/infra-live",
    "title": "Raise the AI team's CPU quota",
    "summary": "Requested for the Q4 training runs.",
    "edits": [RAISE_AI_CPU],
}

CATALOG = catalog.Catalog(
    repos=(
        changes.RepoProfile(
            name="acme/infra-live", description="Workspace quotas.", paths=("coder/*",)
        ),
    ),
)
ORIGIN = entities.SlackThread(channel_id="D1", thread_ts="1.0")


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

    async def post_answer(self, *, channel, text, thread_ts, feedback_value):
        self.messages.append((channel, thread_ts, text))
        return "100.2"

    async def set_status(self, *, channel, thread_ts, status):
        pass

    async def update_message(self, *, channel, ts, text):
        self.updates.append((channel, ts, text))

    async def post_approval_card(self, *, channel, approval_id, requester, tool_name, summary):
        self.cards.append({"approval_id": approval_id, "tool_name": tool_name, "summary": summary})
        return "200.1"

    async def fetch_thread(self, *, channel, thread_ts, limit):
        return []


class FakeRepoHost:
    def __init__(self):
        self.pull_requests = []

    async def read_file(self, *, repo, path, ref):
        return QUOTAS if path == "coder/quotas.tfvars" else None

    async def list_directory(self, *, repo, path, ref):
        return ["coder/quotas.tfvars"]

    async def open_pull_request(self, *, repo, base, branch, title, body, files):
        self.pull_requests.append({"repo": repo, "base": base, "files": dict(files)})
        return PULL_REQUEST_URL


class FakeReviewer:
    """
    Stand-in for the independent reviewer: answers with a fixed opinion (or
    fails) and records what it was shown.
    """

    def __init__(self, review=None, error=None):
        self.review_to_give = review
        self.error = error
        self.seen = []

    async def review(self, *, profile, title, summary, files):
        self.seen.append((profile.name, title, [file.after for file in files]))
        if self.error is not None:
            raise self.error
        return self.review_to_give


@pytest.fixture
def wire(monkeypatch):
    """
    Wire a full Configuration around a scripted model, a catalog with one
    repository, and a fake repository host; return (gateway, host).
    """

    def _wire(model, reviewer=None):
        gateway = FakeSlackGateway()
        host = FakeRepoHost()
        cfg = config.Configuration(
            settings=Settings(
                _env_file=None, slack_triage_channel="C_TRIAGE", support_user_ids="U_SUPPORT"
            ),
            slack=gateway,
            triage=slack_vendor.SlackTriageBackend(gateway=gateway, triage_channel="C_TRIAGE"),
            jira=None,
            directory=users.UserDirectory(users=()),
            approvals=approvals.InMemoryApprovalStore(),
            model=model,
            confluence_mcp=None,
            sailpoint_mcp=None,
            agent=catalog.mount(
                support_agent.build_agent(model=model),
                catalog=CATALOG,
                model=model,
                specialist_mcp_tools={},
                repo_host=host,
            ),
            catalog=CATALOG,
            repo_host=host,
            change_reviewer=reviewer,
        )
        monkeypatch.setattr(config, "get_config", lambda: cfg)
        return gateway, host

    return _wire


async def _ask_for_more_cpu():
    await support.handle_support_request(
        request=entities.SupportRequest(
            id="Ev-1",
            user_id="U_REQ",
            text="Please raise the AI team's Coder CPU quota to 32",
            origin=ORIGIN,
        )
    )


def _proposing_model(*, proposal=None, after_decision="Done."):
    """
    A model that reads the file, previews its edit, asks to open the pull
    request, and — once the human has decided — says one last thing.
    """
    proposal = proposal or PROPOSAL
    return ScriptedModel(
        [
            [
                _tool_call(
                    "read_repo_file", {"repo": "acme/infra-live", "path": RAISE_AI_CPU["path"]}
                )
            ],
            [_tool_call("preview_change", {"repo": proposal["repo"], "edits": proposal["edits"]})],
            [_tool_call("open_pull_request", proposal)],
            [_text(after_decision)],
        ]
    )


class TestRepositoryChange:
    async def test_the_request_pauses_on_a_card_that_shows_the_exact_edit(self, wire):
        # Given a provisioning request the agent turns into a proposal
        gateway, host = wire(_proposing_model())

        # When the request is handled
        await _ask_for_more_cpu()

        # Then nothing was opened; the approver's card names the repository
        # and shows the line going out and the line coming in
        (card,) = gateway.cards
        assert host.pull_requests == []
        assert card["tool_name"] == "open_pull_request"
        assert "`acme/infra-live`" in card["summary"]
        assert "- ai            = { cpu = 16," in card["summary"]
        assert "+ ai            = { cpu = 32," in card["summary"]

    async def test_the_agent_previewed_a_real_diff_before_asking(self, wire):
        # Given a provisioning request the agent turns into a proposal
        model = _proposing_model()
        wire(model)

        # When the request is handled
        await _ask_for_more_cpu()

        # Then the model's last call saw the file it read and the diff its
        # preview produced — the proposal was grounded, not guessed
        seen = json.dumps(model.inputs[-1], default=str)
        assert "data-platform = { cpu = 8" in seen
        assert "+  ai            = { cpu = 32" in seen

    async def test_approval_opens_the_pull_request_and_reports_back(self, wire):
        # Given a paused proposal
        gateway, host = wire(
            _proposing_model(after_decision=f"I've opened a pull request: {PULL_REQUEST_URL}")
        )
        await _ask_for_more_cpu()

        # When an authorized approver approves it
        await support.resolve_approval(
            approval_id=gateway.cards[0]["approval_id"], resolver_id="U_SUPPORT", approved=True
        )

        # Then one pull request carries the whole edited file against main,
        # and the requester hears where it is
        (pull_request,) = host.pull_requests
        assert (pull_request["repo"], pull_request["base"]) == ("acme/infra-live", "main")
        assert (
            "ai            = { cpu = 32, memory = 64"
            in pull_request["files"][RAISE_AI_CPU["path"]]
        )
        assert any(PULL_REQUEST_URL in text for _, _, text in gateway.messages)

    async def test_a_denied_proposal_opens_nothing(self, wire):
        # Given a paused proposal
        gateway, host = wire(_proposing_model(after_decision="That change was not approved."))
        await _ask_for_more_cpu()

        # When the approver denies it
        await support.resolve_approval(
            approval_id=gateway.cards[0]["approval_id"], resolver_id="U_SUPPORT", approved=False
        )

        # Then no pull request exists and the requester is told
        assert host.pull_requests == []
        assert any("not approved" in text for _, _, text in gateway.messages)

    async def test_approval_cannot_carry_a_change_past_the_path_allowlist(self, wire):
        # Given a proposal that edits CI configuration, outside the
        # repository's allowed paths — and an approver who clicks approve anyway
        off_limits = PROPOSAL | {
            "edits": [{"path": ".github/workflows/apply.yml", "old": "", "new": "on: push\n"}]
        }
        model = _proposing_model(proposal=off_limits, after_decision="I could not open that.")
        gateway, host = wire(model)
        await _ask_for_more_cpu()

        # When the approval is granted
        await support.resolve_approval(
            approval_id=gateway.cards[0]["approval_id"], resolver_id="U_SUPPORT", approved=True
        )

        # Then the tool itself refuses: the allowlist is enforced in code at
        # the moment of writing, whatever was approved
        assert host.pull_requests == []
        assert "outside the paths that may be changed" in json.dumps(model.inputs[-1], default=str)


def _notes_under_the_card(gateway) -> list[str]:
    return [
        text
        for channel, thread_ts, text in gateway.messages
        if (channel, thread_ts) == ("C_TRIAGE", "200.1")
    ]


class TestSecondOpinion:
    CONCERN = "Doubles the quota; the README asks for the team lead to be named."

    async def test_the_approver_reads_an_independent_review_under_the_card(self, wire):
        # Given a reviewer with a concern about the proposed change
        reviewer = FakeReviewer(
            changes.Review(approves=False, findings=(self.CONCERN,), reviewer="claude-opus-5-5")
        )
        gateway, _host = wire(_proposing_model(), reviewer=reviewer)

        # When the request pauses for approval
        await _ask_for_more_cpu()

        # Then the reviewer was shown the file as the change would leave it,
        # and its opinion sits in the card's thread for the approver
        (repo, title, contents) = reviewer.seen[0]
        (note,) = _notes_under_the_card(gateway)
        assert (repo, title) == ("acme/infra-live", PROPOSAL["title"])
        assert "ai            = { cpu = 32," in contents[0]
        assert "claude-opus-5-5" in note
        assert self.CONCERN in note

    async def test_a_reviewer_that_fails_never_holds_up_the_approval(self, wire):
        # Given a reviewer that cannot be reached
        gateway, _host = wire(
            _proposing_model(), reviewer=FakeReviewer(error=TimeoutError("reviewer timed out"))
        )

        # When the request pauses for approval
        await _ask_for_more_cpu()

        # Then the card is up and waiting, just without a second opinion
        assert len(gateway.cards) == 1
        assert _notes_under_the_card(gateway) == []

    async def test_the_approver_is_told_when_a_proposal_cannot_apply(self, wire):
        # Given a proposal outside the repository's allowed paths, and no
        # reviewer wired at all
        off_limits = PROPOSAL | {
            "edits": [{"path": ".github/workflows/apply.yml", "old": "", "new": "on: push\n"}]
        }
        gateway, _host = wire(_proposing_model(proposal=off_limits))

        # When the request pauses for approval
        await _ask_for_more_cpu()

        # Then the card's thread says approving it opens nothing, and why
        (note,) = _notes_under_the_card(gateway)
        assert "will not apply as proposed" in note
        assert "outside the paths that may be changed" in note

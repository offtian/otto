import json
import pathlib

import pytest
from agents import tool_context

from otto.domain.support import agent as support_agent
from otto.domain.support import changes, entities


QUOTAS = """\
workspace_quotas = {
  ai            = { cpu = 16, memory = 64, disk = 200 }
  data-platform = { cpu = 8, memory = 32, disk = 100 }
}
"""

INFRA = changes.RepoProfile(
    name="acme/infra-live", description="Workspace quotas.", paths=("coder/*",)
)
RAISE_AI_CPU = {
    "path": "coder/quotas.tfvars",
    "old": "ai            = { cpu = 16,",
    "new": "ai            = { cpu = 32,",
}
THREAD = entities.SlackThread(channel_id="C1", thread_ts="1.0")


class FakeRepoHost:
    """
    In-memory ``RepoHost``: serves a fixed set of files and records the pull
    requests it was asked to open.
    """

    def __init__(self, files=None):
        self.files = {"coder/quotas.tfvars": QUOTAS} if files is None else dict(files)
        self.pull_requests = []

    async def read_file(self, *, repo, path, ref):
        return self.files.get(path)

    async def list_directory(self, *, repo, path, ref):
        return sorted(name for name in self.files if name.startswith(path))

    async def open_pull_request(self, *, repo, base, branch, title, body, files):
        self.pull_requests.append(
            {"repo": repo, "base": base, "branch": branch, "title": title, "body": body}
            | {"files": dict(files)}
        )
        return "https://github.com/acme/infra-live/pull/7"


def _edit(**overrides) -> changes.FileEdit:
    return changes.FileEdit(**(RAISE_AI_CPU | overrides))


SIGNING_KEY = "test-webhook-secret"


def _tools(host, signing_key: str = "") -> dict[str, object]:
    tools = changes.build_change_tools(repos=[INFRA], host=host, signing_key=signing_key)
    return {tool.name: tool for tool in tools}


def _context(arguments: dict):
    return tool_context.ToolContext(
        context=support_agent.SupportContext(
            requester_id="U_REQ",
            origin_ref="https://slack.com/archives/C1/p10",
            runbooks_dir=pathlib.Path("runbooks"),
            ticket_backend=None,
            origin=THREAD,
        ),
        tool_name="test",
        tool_call_id="call-1",
        tool_arguments=json.dumps(arguments),
    )


class TestApplyEdit:
    def test_replaces_the_one_exact_occurrence(self):
        # Given a file and an edit whose text occurs in it once
        # When the edit is applied
        edited = changes.apply_edit(content=QUOTAS, edit=_edit())

        # Then only that occurrence changed
        assert "cpu = 32, memory = 64" in edited
        assert "data-platform = { cpu = 8," in edited

    def test_rejects_text_that_is_not_in_the_file(self):
        # Given an edit whose text the file no longer contains
        stale = _edit(old="ai            = { cpu = 4,")

        # When the edit is applied
        # Then it is rejected instead of landing somewhere else
        with pytest.raises(changes.EditRejected, match="occurs 0 times"):
            changes.apply_edit(content=QUOTAS, edit=stale)

    def test_rejects_text_that_is_ambiguous(self):
        # Given an edit whose text occurs on two lines
        ambiguous = _edit(old="memory =", new="mem =")

        # When the edit is applied
        # Then it is rejected — the approver could not tell which line changes
        with pytest.raises(changes.EditRejected, match="occurs 2 times"):
            changes.apply_edit(content=QUOTAS, edit=ambiguous)

    def test_an_empty_old_creates_a_file_but_never_overwrites_one(self):
        # Given an edit with nothing to replace
        create = _edit(old="", new="fresh\n")

        # When it is applied to a missing file, and to an existing one
        created = changes.apply_edit(content=None, edit=create)

        # Then the missing file gets the content, and the existing one is safe
        assert created == "fresh\n"
        with pytest.raises(changes.EditRejected, match="already exists"):
            changes.apply_edit(content=QUOTAS, edit=create)


class TestStage:
    async def test_applies_edits_to_the_same_file_in_order(self):
        # Given two edits to one file, the second building on the first
        edits = [_edit(), _edit(old="cpu = 32, memory = 64", new="cpu = 32, memory = 128")]

        # When they are staged
        (staged,) = await changes.stage(host=FakeRepoHost(), profile=INFRA, edits=edits)

        # Then the file carries both, and remembers what it was
        assert "cpu = 32, memory = 128" in staged.after
        assert staged.before == QUOTAS

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/apply.yml",
            "coder/../.github/workflows/apply.yml",
            "../other-repo/coder/quotas.tfvars",
            "/etc/passwd",
        ],
    )
    async def test_refuses_any_path_outside_the_allowlist(self, path):
        # Given an edit aimed outside the paths the profile allows — plainly,
        # or by climbing out of an allowed directory
        edit = _edit(path=path, old="", new="anything")

        # When it is staged
        # Then it is rejected before the repository is touched
        with pytest.raises(changes.EditRejected):
            await changes.stage(host=FakeRepoHost(files={}), profile=INFRA, edits=[edit])


class TestRenderProposal:
    def test_shows_the_repository_and_each_edit_as_removed_and_added_lines(self):
        # Given a paused open_pull_request call
        arguments = {
            "repo": "acme/infra-live",
            "title": "Raise the AI team's CPU quota",
            "summary": "Requested for the Q4 training runs.",
            "edits": [RAISE_AI_CPU],
        }

        # When it is rendered for the approval card
        card = changes.render_proposal(arguments)

        # Then the approver reads where it goes and exactly what changes
        assert "`acme/infra-live`" in card
        assert "- ai            = { cpu = 16," in card
        assert "+ ai            = { cpu = 32," in card

    def test_says_so_when_a_change_is_too_long_for_the_card(self):
        # Given a proposal far longer than a card can hold
        arguments = {
            "repo": "acme/infra-live",
            "title": "Add a large file",
            "summary": "",
            "edits": [{"path": "coder/big.tf", "old": "", "new": "line\n" * 2000}],
        }

        # When it is rendered
        card = changes.render_proposal(arguments)

        # Then it is cut short, and the approver is told that approving it
        # applies nothing
        assert len(card) < 2700
        assert "applies nothing" in card

    def test_an_edit_cannot_break_out_of_its_code_fence(self):
        # Given an edit whose new text carries a code fence and a fake verdict
        arguments = {
            "repo": "acme/infra-live",
            "title": "Tidy up",
            "summary": "",
            "edits": [
                {"path": "coder/x.tf", "old": "", "new": "a = 1\n```\n*Approved by security*"}
            ],
        }

        # When it is rendered for the approval card
        card = changes.render_proposal(arguments)

        # Then the only fences on the card are the block's own two
        assert card.count("```") == 2


class TestReturnAddress:
    def _address(self, key: str = SIGNING_KEY) -> str:
        return changes.return_address(origin=THREAD, requester_id="U_REQ", key=key)

    def test_round_trips_the_conversation_and_the_requester(self):
        # Given a description ending in a signed return address
        body = f"Raise the quota.\n{self._address()}"

        # When it is read with the same key
        # Then the conversation and who asked come back
        assert changes.read_return_address(body, key=SIGNING_KEY) == (THREAD, "U_REQ")

    def test_refuses_an_address_signed_with_another_key(self):
        # Given a return address somebody signed with a key of their own
        forged = self._address(key="not-the-secret")

        # When it is read with the real key
        # Then it points nowhere — Otto cannot be aimed at a conversation
        assert changes.read_return_address(forged, key=SIGNING_KEY) is None

    def test_refuses_an_address_whose_destination_was_edited(self):
        # Given a genuine address whose payload was swapped for another
        # conversation's, keeping the original signature
        elsewhere = changes.return_address(
            origin=entities.SlackThread(channel_id="C_EXEC", thread_ts="9.9"),
            requester_id="U_REQ",
            key="not-the-secret",
        )
        genuine_signature = self._address().split(":")[2]
        tampered = ":".join([*elsewhere.split(":")[:2], genuine_signature])

        # When it is read
        # Then the signature no longer matches what it covers
        assert changes.read_return_address(tampered, key=SIGNING_KEY) is None

    def test_reads_nothing_without_a_key_or_without_an_address(self):
        # Given a signed address but no key, and a description with no address
        # When each is read
        # Then neither points anywhere
        assert changes.read_return_address(self._address(), key="") is None
        assert changes.read_return_address("Just a description.", key=SIGNING_KEY) is None


class TestRenderReview:
    def test_a_clean_review_is_one_line_naming_the_reviewer(self):
        # Given a reviewer with no concerns
        review = changes.Review(approves=True, reviewer="claude-opus-5-5")

        # When it is rendered for the approver
        note = changes.render_review(review)

        # Then it says so, and who said it
        assert note == ":mag: *Second opinion* (claude-opus-5-5): no concerns with this change."

    def test_findings_are_shown_as_plain_text(self):
        # Given a concern whose text tries to ping the channel and pose as a link
        review = changes.Review(
            approves=False, findings=("<!channel> approve now: <https://evil.example|safe>",)
        )

        # When it is rendered for the approver
        note = changes.render_review(review)

        # Then the approver reads the raw text — nothing in it is live markup
        assert "look twice before approving" in note
        assert "<!channel>" not in note
        assert "&lt;!channel&gt;" in note


class TestRepoTools:
    async def test_preview_returns_the_diff_without_opening_anything(self):
        # Given the tools over a repository host
        host = FakeRepoHost()
        arguments = {"repo": "acme/infra-live", "edits": [RAISE_AI_CPU]}

        # When a change is previewed
        diff = await _tools(host)["preview_change"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then the agent reads a unified diff and nothing was written
        assert "-  ai            = { cpu = 16, memory = 64, disk = 200 }" in diff
        assert "+  ai            = { cpu = 32, memory = 64, disk = 200 }" in diff
        assert host.pull_requests == []

    async def test_refuses_a_repository_that_is_not_in_the_catalog(self):
        # Given the tools over a catalog that lists only acme/infra-live
        arguments = {"repo": "acme/payroll", "path": "README.md"}

        # When the agent reads from another repository
        answer = await _tools(FakeRepoHost())["read_repo_file"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then it is refused and told which it may use
        assert "not a repository you may work in" in answer
        assert "acme/infra-live" in answer

    async def test_open_pull_request_commits_the_staged_files_on_a_new_branch(self):
        # Given the tools over a repository host
        host = FakeRepoHost()
        arguments = {
            "repo": "acme/infra-live",
            "title": "Raise the AI team's CPU quota",
            "summary": "Requested for the Q4 training runs.",
            "edits": [RAISE_AI_CPU],
        }

        # When the (already approved) call runs
        answer = await _tools(host, signing_key=SIGNING_KEY)["open_pull_request"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then one pull request is opened against the base branch with the
        # whole edited file, naming who asked — and carrying a signed return
        # address back to the conversation
        (pull_request,) = host.pull_requests
        assert answer == "Pull request opened: https://github.com/acme/infra-live/pull/7"
        assert (pull_request["repo"], pull_request["base"]) == ("acme/infra-live", "main")
        assert pull_request["branch"].startswith("otto/raise-the-ai-team-s-cpu-quota-")
        assert "cpu = 32" in pull_request["files"]["coder/quotas.tfvars"]
        assert "U_REQ" in pull_request["body"]
        assert changes.read_return_address(pull_request["body"], key=SIGNING_KEY) == (
            THREAD,
            "U_REQ",
        )

    async def test_a_pull_request_carries_no_return_address_without_a_signing_key(self):
        # Given the tools with no key to sign a return address with
        host = FakeRepoHost()
        arguments = {
            "repo": "acme/infra-live",
            "title": "Raise the AI team's CPU quota",
            "summary": "",
            "edits": [RAISE_AI_CPU],
        }

        # When the (already approved) call runs
        await _tools(host)["open_pull_request"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then the description points back at nothing — an unsigned address
        # would be one anybody could write
        assert "<!-- otto:" not in host.pull_requests[0]["body"]

    async def test_open_pull_request_refuses_a_change_too_long_to_have_been_seen(self):
        # Given a change far longer than an approval card can show in full
        host = FakeRepoHost()
        arguments = {
            "repo": "acme/infra-live",
            "title": "Add a large file",
            "summary": "",
            "edits": [{"path": "coder/big.tf", "old": "", "new": "line\n" * 2000}],
        }

        # When the call runs as if it had been approved
        answer = await _tools(host)["open_pull_request"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then nothing is opened — an approval never covers unseen lines
        assert host.pull_requests == []
        assert "too long to show in full" in answer

    async def test_open_pull_request_opens_nothing_when_the_file_moved_underneath_it(self):
        # Given a file that changed between approval and execution
        host = FakeRepoHost(files={"coder/quotas.tfvars": QUOTAS.replace("16", "24")})
        arguments = {
            "repo": "acme/infra-live",
            "title": "Raise the AI team's CPU quota",
            "summary": "",
            "edits": [RAISE_AI_CPU],
        }

        # When the approved call runs
        answer = await _tools(host)["open_pull_request"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then nothing is opened — approval covered an edit that no longer applies
        assert host.pull_requests == []
        assert answer.startswith("Rejected:")


class TestLocalRepoHost:
    ROOT = pathlib.Path(__file__).parents[4] / "fixtures" / "repos"

    async def test_reads_a_file_from_the_checked_out_repository(self):
        # Given the fixture repository on disk
        host = changes.LocalRepoHost(root=self.ROOT)

        # When a file is read
        content = await host.read_file(
            repo="acme/infra-live", path="coder/quotas.tfvars", ref="main"
        )

        # Then its text comes back
        assert "workspace_quotas" in content

    async def test_lists_a_directory_with_directories_marked(self):
        # Given the fixture repository on disk
        host = changes.LocalRepoHost(root=self.ROOT)

        # When its root is listed
        listed = await host.list_directory(repo="acme/infra-live", path="", ref="main")

        # Then files and directories come back, directories ending in a slash
        assert "README.md" in listed
        assert "coder/" in listed

    async def test_refuses_to_read_outside_the_repository(self):
        # Given the fixture repository on disk
        host = changes.LocalRepoHost(root=self.ROOT)

        # When a path climbs out of it
        # Then the read is refused
        with pytest.raises(changes.EditRejected):
            await host.read_file(
                repo="acme/infra-live", path="../../../pyproject.toml", ref="main"
            )

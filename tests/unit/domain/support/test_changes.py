import io
import json
import pathlib
import tarfile

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
    In-memory ``RepoHost``: serves a fixed set of files — the same at every
    ref — and at most one existing pull request, and records what it was
    asked to read, open and commit.
    """

    def __init__(self, files=None, pull_request=None):
        self.files = {"coder/quotas.tfvars": QUOTAS} if files is None else dict(files)
        self.pull_request = pull_request
        self.pull_requests = []
        self.commits = []
        self.reads = []

    async def read_file(self, *, repo, path, ref):
        self.reads.append((path, ref))
        return self.files.get(path)

    async def get_pull_request(self, *, repo, number):
        known = self.pull_request is not None and self.pull_request["number"] == number
        return self.pull_request if known else None

    async def push_commit(self, *, repo, branch, parent, message, files):
        self.commits.append(
            {"repo": repo, "branch": branch, "parent": parent, "message": message}
            | {"files": dict(files)}
        )

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
OTHER_THREAD = entities.SlackThread(channel_id="C2", thread_ts="9.0")
PULL_REQUEST_URL = "https://github.com/acme/infra-live/pull/7"
BRANCH = "otto/raise-the-ai-team-s-cpu-quota-1a2b3c4d"


def _pull_request(**overrides) -> dict:
    """
    The fields of an open pull request Otto opened from THREAD.
    """
    address = changes.return_address(origin=THREAD, requester_id="U_REQ", key=SIGNING_KEY)
    return {
        "number": 7,
        "url": PULL_REQUEST_URL,
        "title": "Raise the AI team's CPU quota",
        "body": f"Requested for the Q4 training runs.\n{address}",
        "branch": BRANCH,
        "head": "head-sha",
        "base": "main",
        "open": True,
        "same_repo": True,
    } | overrides


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


class TestRenderRevision:
    def test_shows_a_revision_as_the_pull_request_it_adds_a_commit_to(self):
        # Given a paused update_pull_request call
        arguments = {
            "repo": "acme/infra-live",
            "number": 7,
            "message": "Name the team lead, as the reviewer asked",
            "edits": [RAISE_AI_CPU],
        }

        # When it is rendered for the approval card
        card = changes.render_proposal(arguments, revision=True)

        # Then the approver reads which pull request it revises, why, and
        # the exact lines going out and coming in
        assert "*Revises pull request:* #7" in card
        assert "Name the team lead, as the reviewer asked" in card
        assert "- ai            = { cpu = 16," in card
        assert "+ ai            = { cpu = 32," in card

    def test_shows_nothing_but_a_number_after_the_hash(self):
        # Given a call whose "number" is something a model made up
        arguments = {"repo": "acme/infra-live", "number": "7 <!channel>", "edits": []}

        # When it is rendered for the approval card
        card = changes.render_proposal(arguments, revision=True)

        # Then the card does not repeat it
        assert "*Revises pull request:* #?" in card
        assert "<!channel>" not in card

    def test_a_call_that_opens_a_pull_request_never_reads_as_a_revision(self):
        # Given a paused open_pull_request call that carries a number it has
        # no use for — the tool ignores it and opens a new pull request
        arguments = {
            "repo": "acme/infra-live",
            "title": "Raise the AI team's CPU quota",
            "summary": "Requested for the Q4 training runs.",
            "number": 7,
            "edits": [RAISE_AI_CPU],
        }

        # When it is rendered for the approval card
        card = changes.render_proposal(arguments)

        # Then the card says what approving does: a new pull request
        assert "*Pull request:* Raise the AI team's CPU quota" in card
        assert "Revises" not in card


class TestFindRevisable:
    async def _find(self, host):
        return await changes.find_revisable(
            host=host, profile=INFRA, number=7, origin=THREAD, signing_key=SIGNING_KEY
        )

    async def test_returns_the_pull_request_this_conversation_opened(self):
        # Given an open pull request carrying this conversation's signed
        # return address
        host = FakeRepoHost(pull_request=_pull_request())

        # When it is looked up to be revised
        pull = await self._find(host)

        # Then it comes back with the branch and the commit to build on
        assert (pull.number, pull.branch, pull.head) == (7, BRANCH, "head-sha")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"body": "Opened by a person, with no return address."},
            {
                "body": changes.return_address(
                    origin=OTHER_THREAD, requester_id="U_REQ", key=SIGNING_KEY
                )
            },
            {
                "body": changes.return_address(
                    origin=THREAD, requester_id="U_REQ", key="somebody-elses-key"
                )
            },
            {"branch": "feature/somebody-elses-work"},
            {"same_repo": False},
            {"base": "release"},
        ],
        ids=[
            "no return address",
            "another conversation's address",
            "an address another key signed",
            "a branch Otto did not create",
            "a fork's branch",
            "another base branch",
        ],
    )
    async def test_refuses_a_pull_request_that_is_not_this_conversations(self, overrides):
        # Given an open pull request that differs from one of this
        # conversation's own in a single respect
        host = FakeRepoHost(pull_request=_pull_request(**overrides))

        # When it is looked up to be revised
        # Then it is refused, in the same words whatever the reason
        with pytest.raises(changes.EditRejected, match="was not opened from this conversation"):
            await self._find(host)

    async def test_refuses_a_closed_pull_request_and_one_that_does_not_exist(self):
        # Given a pull request that has been closed, and a repository with none
        closed = FakeRepoHost(pull_request=_pull_request(open=False))
        empty = FakeRepoHost()

        # When each is looked up to be revised
        # Then each is refused for what it is
        with pytest.raises(changes.EditRejected, match="is closed"):
            await self._find(closed)
        with pytest.raises(changes.EditRejected, match="there is no pull request #7"):
            await self._find(empty)


class TestFailedCheckEvent:
    FAILURE = changes.CheckFailure(
        repo="acme/infra-live",
        number=7,
        name="terraform",
        url="https://github.com/acme/infra-live/actions/runs/1",
        commit="head-sha",
    )

    async def test_addresses_the_conversation_that_asked_for_the_pull_request(self):
        # Given an open pull request Otto opened from a conversation
        host = FakeRepoHost(pull_request=_pull_request())

        # When a check fails on it
        event = await changes.failed_check_event(
            host=host, failure=self.FAILURE, signing_key=SIGNING_KEY
        )

        # Then that conversation is told which check, on which pull request
        assert event == changes.PullRequestEvent(
            kind=changes.CHECKS_FAILED,
            url=PULL_REQUEST_URL,
            title="Raise the AI team's CPU quota",
            origin=THREAD,
            requester_id="U_REQ",
            detail="terraform — https://github.com/acme/infra-live/actions/runs/1",
            repo="acme/infra-live",
            number=7,
        )

    @pytest.mark.parametrize(
        "pull_request",
        [None, _pull_request(open=False), _pull_request(body="Opened by a person.")],
        ids=["no such pull request", "a closed one", "one that is not Otto's"],
    )
    async def test_reports_nothing_for_a_pull_request_that_is_not_an_open_one_of_ottos(
        self, pull_request
    ):
        # Given a failed check on a pull request nobody is waiting to hear about
        host = FakeRepoHost(pull_request=pull_request)

        # When the failure is looked up
        event = await changes.failed_check_event(
            host=host, failure=self.FAILURE, signing_key=SIGNING_KEY
        )

        # Then there is no conversation to tell
        assert event is None


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


REVISION = {
    "repo": "acme/infra-live",
    "number": 7,
    "message": "Raise it to 32, as the reviewer asked",
    "edits": [RAISE_AI_CPU],
}


class TestRevisionTools:
    async def test_update_pull_request_commits_on_the_commit_it_staged_against(self):
        # Given the tools over a host holding this conversation's pull request
        host = FakeRepoHost(pull_request=_pull_request())

        # When the (already approved) revision runs
        answer = await _tools(host, signing_key=SIGNING_KEY)["update_pull_request"].on_invoke_tool(
            _context(REVISION), json.dumps(REVISION)
        )

        # Then the file was read at the pull request's commit, and one commit
        # with the whole edited file goes on that same commit of its branch —
        # and no second pull request is opened
        (commit,) = host.commits
        assert answer == f"Pull request updated: {PULL_REQUEST_URL}"
        assert host.reads == [("coder/quotas.tfvars", "head-sha")]
        assert (commit["branch"], commit["parent"]) == (BRANCH, "head-sha")
        assert commit["message"] == "Raise it to 32, as the reviewer asked"
        assert "cpu = 32" in commit["files"]["coder/quotas.tfvars"]
        assert host.pull_requests == []

    async def test_update_pull_request_writes_nothing_to_a_pull_request_that_is_not_its_own(
        self,
    ):
        # Given a pull request on a branch Otto did not create
        host = FakeRepoHost(pull_request=_pull_request(branch="main"))

        # When an approved revision of it runs
        answer = await _tools(host, signing_key=SIGNING_KEY)["update_pull_request"].on_invoke_tool(
            _context(REVISION), json.dumps(REVISION)
        )

        # Then it is refused and no commit is made anywhere
        assert "was not opened from this conversation" in answer
        assert host.commits == []

    async def test_preview_update_returns_the_diff_without_committing(self):
        # Given the tools over a host holding this conversation's pull request
        host = FakeRepoHost(pull_request=_pull_request())
        arguments = {"repo": "acme/infra-live", "number": 7, "edits": [RAISE_AI_CPU]}

        # When a revision is previewed
        diff = await _tools(host, signing_key=SIGNING_KEY)["preview_update"].on_invoke_tool(
            _context(arguments), json.dumps(arguments)
        )

        # Then the agent reads a unified diff and nothing was written
        assert "+  ai            = { cpu = 32, memory = 64, disk = 200 }" in diff
        assert host.commits == []

    async def test_read_pull_request_file_reads_the_pull_request_not_the_base(self):
        # Given the tools over a host holding this conversation's pull request
        host = FakeRepoHost(pull_request=_pull_request())
        arguments = {"repo": "acme/infra-live", "number": 7, "path": "coder/quotas.tfvars"}

        # When one of its files is read
        content = await _tools(host, signing_key=SIGNING_KEY)[
            "read_pull_request_file"
        ].on_invoke_tool(_context(arguments), json.dumps(arguments))

        # Then it is the file at the commit the pull request is at
        assert content == QUOTAS
        assert host.reads == [("coder/quotas.tfvars", "head-sha")]

    def test_the_revision_tools_are_mounted_only_with_a_key_to_recognise_a_pull_request_by(
        self,
    ):
        # Given the tools built with and without a signing key
        revision_tools = {"read_pull_request_file", "preview_update", "update_pull_request"}

        # When each set is listed
        without_key = set(_tools(FakeRepoHost()))
        with_key = _tools(FakeRepoHost(), signing_key=SIGNING_KEY)

        # Then only a key brings the revision tools, and the write is gated
        assert revision_tools.isdisjoint(without_key)
        assert revision_tools <= set(with_key)
        assert with_key["update_pull_request"].needs_approval is True


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

    async def test_packs_the_checked_out_repository_without_its_history(self, tmp_path):
        # Given a checked-out repository with a file, a .gitignore and a
        # .git directory
        repo = tmp_path / "acme" / "infra-live"
        (repo / "coder").mkdir(parents=True)
        (repo / "coder" / "quotas.tfvars").write_text(QUOTAS)
        (repo / ".gitignore").write_text(".terraform/\n")
        (repo / ".git").mkdir()
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main")

        # When it is downloaded
        archive = await changes.LocalRepoHost(root=tmp_path).download(
            repo="acme/infra-live", ref="main"
        )

        # Then the archive holds its files and none of its history
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = {member.name for member in tar.getmembers() if member.isfile()}
        assert names == {"./coder/quotas.tfvars", "./.gitignore"}

    async def test_refuses_to_read_outside_the_repository(self):
        # Given the fixture repository on disk
        host = changes.LocalRepoHost(root=self.ROOT)

        # When a path climbs out of it
        # Then the read is refused
        with pytest.raises(changes.EditRejected):
            await host.read_file(
                repo="acme/infra-live", path="../../../pyproject.toml", ref="main"
            )

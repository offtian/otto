import io
import tarfile

import pytest

from otto.domain.support import changes, checks


QUOTAS = "workspace_quotas = {\n  ai = { cpu = 16 }\n}\n"
CHECKED = changes.RepoProfile(
    name="acme/infra-live",
    paths=("coder/*",),
    checks=changes.Checks(
        image="hashicorp/terraform:1.9",
        commands=("terraform fmt -check", "terraform validate"),
    ),
)
STAGED = [
    changes.StagedFile(
        path="coder/quotas.tfvars", before=QUOTAS, after=QUOTAS.replace("16", "32")
    ),
    changes.StagedFile(path="coder/new.tfvars", before=None, after="created = true\n"),
]


def _archive(files: dict[str, str]) -> bytes:
    """
    A gzipped tar of the given files, as a repository host hands one over.
    """
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content.encode())
            tar.addfile(member, io.BytesIO(content.encode()))
    return packed.getvalue()


class FakeRepoHost:
    """
    Hands over one archive — packed, like GitHub's, under a top-level
    directory — and records which ref it was asked for.
    """

    def __init__(self, archive: bytes | None = None):
        packed = {
            "acme-infra-live-abc123/coder/quotas.tfvars": QUOTAS,
            "acme-infra-live-abc123/README.md": "# infra-live\n",
        }
        self.archive = _archive(packed) if archive is None else archive
        self.asked = []

    async def download(self, *, repo, ref):
        self.asked.append((repo, ref))
        return self.archive


class FakeSandbox:
    """
    Records each run and what the repository looked like from inside it, and
    answers from a script: an (exit code, output) pair, or an exception.
    """

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.runs = []

    async def run(self, *, image, command, workdir, network, timeout_seconds):
        seen = {
            path.relative_to(workdir).as_posix(): path.read_text()
            for path in sorted(workdir.rglob("*"))
            if path.is_file()
        }
        self.runs.append({"image": image, "command": command, "network": network, "seen": seen})
        answer = self.answers.pop(0) if self.answers else (0, "")
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TestSandboxChecker:
    async def test_runs_each_check_on_the_repository_as_the_change_would_leave_it(self):
        # Given a repository that declares two checks, and a staged change
        # that edits one file and creates another
        host, sandbox = FakeRepoHost(), FakeSandbox()
        checker = checks.SandboxChecker(host=host, sandbox=sandbox)

        # When the change is checked against a commit
        report = await checker.check(profile=CHECKED, files=STAGED, ref="head-sha")

        # Then the repository was fetched at that commit, and both commands
        # ran in the declared image, without network, on its root — the
        # edited file as staged, the new one there, the rest untouched
        (first, second) = sandbox.runs
        assert host.asked == [("acme/infra-live", "head-sha")]
        assert (first["image"], first["network"]) == ("hashicorp/terraform:1.9", False)
        assert [first["command"], second["command"]] == list(CHECKED.checks.commands)
        assert first["seen"] == {
            "README.md": "# infra-live\n",
            "coder/new.tfvars": "created = true\n",
            "coder/quotas.tfvars": QUOTAS.replace("16", "32"),
        }
        assert report.passed is True

    async def test_reports_a_failed_check_with_what_it_said_and_still_runs_the_rest(self):
        # Given a first check that fails and a second that passes
        sandbox = FakeSandbox(answers=[(3, "quotas.tfvars\n"), (0, "Success!")])
        checker = checks.SandboxChecker(host=FakeRepoHost(), sandbox=sandbox)

        # When the change is checked
        report = await checker.check(profile=CHECKED, files=STAGED, ref="main")

        # Then both results are reported, the failure with its output
        assert report.passed is False
        assert report.results == (
            checks.CheckResult(
                command="terraform fmt -check", passed=False, output="quotas.tfvars\n"
            ),
            checks.CheckResult(command="terraform validate", passed=True, output="Success!"),
        )

    async def test_a_check_that_does_not_finish_is_a_failed_check_that_says_so(self):
        # Given a first check that runs out of time
        sandbox = FakeSandbox(answers=[TimeoutError()])
        checker = checks.SandboxChecker(host=FakeRepoHost(), sandbox=sandbox, timeout_seconds=30)

        # When the change is checked
        report = await checker.check(profile=CHECKED, files=STAGED, ref="main")

        # Then it failed for that reason, and the next check still ran
        assert report.results[0] == checks.CheckResult(
            command="terraform fmt -check",
            passed=False,
            output="did not finish within 30 seconds",
        )
        assert len(sandbox.runs) == 2

    async def test_checks_nothing_for_a_repository_that_declares_no_checks(self):
        # Given a repository whose catalog entry names no checks
        unchecked = changes.RepoProfile(name="acme/infra-live", paths=("coder/*",))
        host, sandbox = FakeRepoHost(), FakeSandbox()
        checker = checks.SandboxChecker(host=host, sandbox=sandbox)

        # When a change to it is checked
        report = await checker.check(profile=unchecked, files=STAGED, ref="main")

        # Then there is no report, and nothing was fetched or run
        assert (report, host.asked, sandbox.runs) == (None, [], [])

    async def test_refuses_an_archive_that_would_unpack_outside_its_scratch_directory(self):
        # Given a host whose archive holds a path that climbs out
        host = FakeRepoHost(archive=_archive({"../outside.txt": "escaped"}))
        sandbox = FakeSandbox()
        checker = checks.SandboxChecker(host=host, sandbox=sandbox)

        # When a change is checked
        # Then unpacking is refused and no command is run
        with pytest.raises(tarfile.FilterError):
            await checker.check(profile=CHECKED, files=STAGED, ref="main")
        assert sandbox.runs == []


class TestRenderChecks:
    def test_says_in_one_line_that_the_checks_pass(self):
        # Given a report in which both checks passed
        report = checks.CheckReport(
            results=(
                checks.CheckResult(command="terraform fmt -check", passed=True),
                checks.CheckResult(command="terraform validate", passed=True),
            )
        )

        # When it is rendered for the approver
        # Then it is one line naming what ran
        assert checks.render_checks(report) == (
            ":white_check_mark: *The repository's checks pass with this change:* "
            "`terraform fmt -check`, `terraform validate`"
        )

    def test_shows_what_a_failed_check_said_as_plain_text(self):
        # Given a failed check whose output tries to mention a channel and to
        # close its own code block
        report = checks.CheckReport(
            results=(
                checks.CheckResult(
                    command="terraform validate",
                    passed=False,
                    output="Error: <!channel> ```\n*approve this*",
                ),
                checks.CheckResult(command="terraform fmt -check", passed=True),
            )
        )

        # When it is rendered for the approver
        note = checks.render_checks(report)

        # Then each check is listed with how it went, and the failure's
        # output is shown — escaped, and still inside its one block
        assert ":x: *The repository's checks do not pass with this change:*" in note
        assert "• `terraform validate` — failed" in note
        assert "• `terraform fmt -check` — passed" in note
        assert "&lt;!channel&gt;" in note
        assert note.count("```") == 2

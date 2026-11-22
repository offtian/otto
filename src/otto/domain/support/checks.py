"""
Pre-flight checks: what a repository's own tools say about a proposed change,
before a human is asked to approve it.

A catalogued repository may name commands to run on every change proposed to
it (``checks`` in ``otto.yaml``: a formatter, a linter, a validator, tests).
When a proposal pauses for approval the repository is fetched as it is at the
commit the change was staged against, the staged files are laid over it, and
the commands run there. What they said goes under the approval card, beside
the second opinion. Advisory, like the review: a failed check blocks nothing
— it tells the approver.

The commands are the operator's, so they are trusted. What they run *on* is
not: a repository's files plus edits a model wrote, and a test run executes
both. So they run in a ``Sandbox`` — no credentials, no network unless the
repository's entry asks for it, nothing of the host but a scratch copy of the
repository — and what they print is shown as plain text.

Two ports. ``Sandbox`` runs one command over a directory in isolation (a
container, ``vendors/docker``). ``ChangeChecker`` is the whole pre-flight;
``SandboxChecker`` is the implementation that fetches, overlays and runs.

ponytail: checks run for the approver only. Give the agent a tool that runs
them on a previewed change when proposals keep reaching approvers failing a
check the agent could have fixed first.
"""

import asyncio
import io
import pathlib
import tarfile
import tempfile
from collections.abc import Sequence
from typing import Protocol

import attrs

from otto.domain.support import changes


# The most of one command's output that is kept — its end, where a tool says
# what went wrong.
_OUTPUT_LIMIT = 1500
# The most of it that is shown per failed command under the card.
_SHOWN_LIMIT = 600
# The largest repository that is unpacked for checking, packed and unpacked.
# Beyond it the checks are skipped rather than fill the disk.
_ARCHIVE_LIMIT = 50 * 1024 * 1024
_UNPACKED_LIMIT = 500 * 1024 * 1024


@attrs.frozen
class CheckResult:
    """
    What one command said about the change.
    """

    command: str
    passed: bool
    # The end of what it printed. Untrusted: it ran on untrusted files.
    output: str = ""


@attrs.frozen
class CheckReport:
    """
    What a repository's checks said about a staged change, in the order they
    are declared.
    """

    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """
        Test whether every check passed.
        """
        return all(result.passed for result in self.results)


class Sandbox(Protocol):
    """
    Somewhere to run a command on files that are not trusted.
    """

    async def run(
        self,
        *,
        image: str,
        command: str,
        workdir: pathlib.Path,
        network: bool,
        timeout_seconds: float,
    ) -> tuple[int, str]:
        """
        Run one shell command in ``image`` with ``workdir`` as its working
        directory, and return its exit code and everything it printed.

        The command sees ``workdir`` and nothing else of the host: no
        credentials, no environment, and no network unless ``network``.

        :raises TimeoutError: if it does not finish in time; it is stopped.
        :raises OSError: if the sandbox itself could not start — as opposed
            to starting and the command failing.
        """
        ...


class ChangeChecker(Protocol):
    """
    The pre-flight on a staged change.
    """

    async def check(
        self, *, profile: changes.RepoProfile, files: Sequence[changes.StagedFile], ref: str
    ) -> CheckReport | None:
        """
        Run the profile's checks on the repository as the staged files would
        leave it, and return what they said — None when the profile names no
        checks.

        :param ref: the branch or commit the change was staged against.

        May raise or take a while — the caller treats a pre-flight that
        failed to run as none, and never lets it hold up the approval.
        """
        ...


@attrs.frozen
class SandboxChecker:
    """
    ``ChangeChecker`` that unpacks the repository into a scratch directory,
    lays the staged files over it, and runs each check there in a sandbox.
    """

    host: changes.RepoHost
    sandbox: Sandbox
    # The longest one command may run.
    timeout_seconds: float = 120.0

    async def check(
        self, *, profile: changes.RepoProfile, files: Sequence[changes.StagedFile], ref: str
    ) -> CheckReport | None:
        """
        Return what the profile's checks say about the staged files.

        :raises ValueError: if the repository is too large to unpack.
        :raises OSError: if the sandbox could not start.
        """
        checks = profile.checks
        if checks is None or not checks.commands:
            return None
        archive = await self.host.download(repo=profile.name, ref=ref)
        with tempfile.TemporaryDirectory(prefix="otto-check-") as scratch:
            # Unpacking is blocking file work: keep it off the event loop.
            workdir = await asyncio.to_thread(
                _materialize, archive=archive, files=files, scratch=pathlib.Path(scratch)
            )
            results = [
                await self._run(command, checks=checks, workdir=workdir)
                for command in checks.commands
            ]
        return CheckReport(results=tuple(results))

    async def _run(
        self, command: str, *, checks: changes.Checks, workdir: pathlib.Path
    ) -> CheckResult:
        try:
            code, output = await self.sandbox.run(
                image=checks.image,
                command=command,
                workdir=workdir,
                network=checks.network,
                timeout_seconds=self.timeout_seconds,
            )
        except TimeoutError:
            return CheckResult(
                command=command,
                passed=False,
                output=f"did not finish within {self.timeout_seconds:g} seconds",
            )
        return CheckResult(command=command, passed=code == 0, output=output[-_OUTPUT_LIMIT:])


def render_checks(report: CheckReport) -> str:
    """
    Render what the checks said as mrkdwn for the approver. Commands and
    output are shown as plain text inside code marks: output comes from
    running untrusted files, so nothing in it can mention anyone, pose as a
    link, or close its own block.
    """
    if report.passed:
        ran = ", ".join(_inline(result.command) for result in report.results)
        return f":white_check_mark: *The repository's checks pass with this change:* {ran}"
    lines = [":x: *The repository's checks do not pass with this change:*"]
    for result in report.results:
        lines.append(f"• {_inline(result.command)} — {'passed' if result.passed else 'failed'}")
        if not result.passed and result.output.strip():
            shown = changes.fenced(changes.plain(result.output.strip()[-_SHOWN_LIMIT:]))
            lines.append(f"```{shown}```")
    return "\n".join(lines)


def _inline(command: str) -> str:
    return "`" + changes.plain(command).replace("`", "'") + "`"


def _materialize(
    *, archive: bytes, files: Sequence[changes.StagedFile], scratch: pathlib.Path
) -> pathlib.Path:
    """
    Unpack the repository into ``scratch``, lay the staged files over it, and
    return the directory that is the repository's root.

    :raises ValueError: if the archive is too large, packed or unpacked, or a
        staged file would land outside the repository.
    """
    if len(archive) > _ARCHIVE_LIMIT:
        raise ValueError("the repository is too large to check")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        if sum(member.size for member in tar.getmembers()) > _UNPACKED_LIMIT:
            raise ValueError("the repository is too large to check")
        # The "data" filter refuses what an archive must never do: absolute
        # paths, paths that climb out, links that point outside, devices.
        tar.extractall(scratch, filter="data")
    entries = list(scratch.iterdir())
    # A host may pack everything under one top-level directory.
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else scratch
    for file in files:
        target = (root / file.path).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError(f"{file.path} is not a path inside the repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.after, encoding="utf-8")
    return root

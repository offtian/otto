"""
Integration tests for the Docker sandbox against a real daemon — what a
container is actually allowed is something only Docker can say.

Live: needs a running Docker daemon and pulls a small image on first use.
Guarded by RUN_INTEGRATION so the normal test suite and CI skip it, and
skipped when no daemon answers. Run via `just test-integration`.
"""

import os
import pathlib
import subprocess

import pytest

from otto.domain.support import changes, checks
from otto.vendors import docker


def _docker_answers() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"],  # noqa: S607 - docker is found the way the sandbox finds it
                capture_output=True,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="needs Docker — run via `just test-integration`",
    ),
    pytest.mark.skipif(
        bool(os.environ.get("RUN_INTEGRATION")) and not _docker_answers(),
        reason="no Docker daemon is running",
    ),
]

IMAGE = "alpine:3.20"


async def _run(command: str, *, workdir: pathlib.Path, network: bool = False, limit=60.0):
    return await docker.DockerSandbox().run(
        image=IMAGE,
        command=command,
        workdir=workdir,
        network=network,
        timeout_seconds=limit,
    )


def _running_checks() -> str:
    return subprocess.run(
        ["docker", "ps", "--quiet", "--filter", "name=otto-check-"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout.strip()


class TestDockerSandbox:
    async def test_runs_a_command_over_the_directory_and_reports_how_it_ended(self, tmp_path):
        # Given a directory holding one file
        (tmp_path / "quotas.tfvars").write_text("ai = 8\n")

        # When a command reads it, writes beside it, complains and fails
        code, output = await _run(
            "cat quotas.tfvars; echo checked > report.txt; echo too-high >&2; exit 3",
            workdir=tmp_path,
        )

        # Then its exit code and both streams come back, with nothing of
        # Docker's own in between, and what it wrote is in the directory
        assert code == 3
        assert output.splitlines() == ["ai = 8", "too-high"]
        assert (tmp_path / "report.txt").read_text() == "checked\n"

    async def test_fences_the_command_in(self, tmp_path, monkeypatch):
        # Given a secret in this process's environment
        monkeypatch.setenv("OTTO_TEST_SECRET", "must-not-leak")

        # When a command looks around inside the sandbox
        _, output = await _run(
            "id -u; env; ls /sys/class/net; touch /etc/written 2>&1; grep CapEff /proc/self/status",
            workdir=tmp_path,
        )

        # Then it is this process's user and not root, sees none of this
        # process's environment, has no network interface beyond loopback,
        # cannot write outside the directory, and holds no capabilities
        assert output.splitlines()[0] == str(os.getuid())
        assert "OTTO_TEST_SECRET" not in output
        assert "eth0" not in output
        assert "Read-only file system" in output
        assert "CapEff:\t0000000000000000" in output

    async def test_gives_the_network_when_asked(self, tmp_path):
        # Given a check whose repository says it needs the network
        # When a command lists its network interfaces
        _, output = await _run("ls /sys/class/net", workdir=tmp_path, network=True)

        # Then there is one beyond loopback
        assert "eth0" in output

    async def test_stops_a_command_that_does_not_finish(self, tmp_path):
        # Given a command that would run far longer than it is allowed
        # When it is run with a short limit
        with pytest.raises(TimeoutError):
            await _run("sleep 300", workdir=tmp_path, limit=3.0)

        # Then its container is gone, not left running behind the caller.
        # Stopping happens on its own task, so one more short run gives it time.
        await _run("sleep 2", workdir=tmp_path)
        assert _running_checks() == ""

    async def test_says_so_when_the_image_cannot_be_had(self, tmp_path):
        # Given an image that exists nowhere
        sandbox = docker.DockerSandbox()

        # When a command is run in it
        # Then the sandbox itself is reported as unavailable, which is a
        # different thing from a check that ran and failed
        with pytest.raises(docker.SandboxUnavailable):
            await sandbox.run(
                image="otto-test/does-not-exist:nope",
                command="true",
                workdir=tmp_path,
                network=False,
                timeout_seconds=60.0,
            )


class TestPreflightOnDocker:
    async def test_runs_a_repositorys_checks_on_the_change_laid_over_the_repository(self):
        # Given the fixture repository, a staged edit that gives the zeta
        # team a quota, and three checks: one only the edit satisfies, one
        # only the rest of the repository satisfies, and one nothing does
        repos = changes.LocalRepoHost(root=pathlib.Path("fixtures/repos"))
        profile = changes.RepoProfile(
            name="acme/infra-live",
            paths=("coder/*",),
            checks=changes.Checks(
                image=IMAGE,
                commands=(
                    "grep -q zeta coder/quotas.tfvars",
                    "test -f README.md",
                    "grep -q nowhere coder/quotas.tfvars || { echo no quota for nowhere; exit 1; }",
                ),
            ),
        )
        before = await repos.read_file(repo=profile.name, path="coder/quotas.tfvars", ref="main")
        staged = [
            changes.StagedFile(
                path="coder/quotas.tfvars",
                before=before,
                after=before.replace(
                    "  ai  ", "  zeta = { cpu = 4, memory = 16, disk = 50 }\n  ai  "
                ),
            )
        ]
        checker = checks.SandboxChecker(host=repos, sandbox=docker.DockerSandbox())

        # When the pre-flight runs
        report = await checker.check(profile=profile, files=staged, ref="main")

        # Then each check ran on the repository as the change would leave
        # it, and the one that failed says why
        assert report is not None
        assert [result.passed for result in report.results] == [True, True, False]
        assert report.results[2].output.strip() == "no quota for nowhere"
        # And the repository itself was not touched
        assert (
            await repos.read_file(repo=profile.name, path="coder/quotas.tfvars", ref="main")
            == before
        )

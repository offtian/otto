import asyncio
import itertools
import os
import pathlib

import pytest

from otto.vendors import docker


WORKDIR = pathlib.Path("/scratch/otto-check-1/repo")


def _arguments(**overrides) -> list[str]:
    given = {
        "name": "otto-check-abc",
        "image": "hashicorp/terraform:1.9",
        "command": "terraform fmt -check",
        "workdir": WORKDIR,
        "network": False,
    }
    return docker.arguments(**(given | overrides))


def _pairs(arguments: list[str]) -> set[tuple[str, str]]:
    return set(itertools.pairwise(arguments))


class TestArguments:
    def test_fences_the_container_in(self):
        # Given a command to run on a scratch copy of a repository
        # When the docker arguments are built
        arguments = _arguments()

        # Then the container has no network, a read-only root, no
        # capabilities or way to gain any, limits, this process's own user —
        # and sees the scratch directory and nothing else of the host
        assert {
            ("--network", "none"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--user", f"{os.getuid()}:{os.getgid()}"),
            ("--volume", f"{WORKDIR}:/work"),
            ("--workdir", "/work"),
        } <= _pairs(arguments)
        assert {"--rm", "--read-only", "--pids-limit", "--memory", "--cpus"} <= set(arguments)
        assert arguments.count("--volume") == 1
        assert [value for flag, value in _pairs(arguments) if flag == "--env"] == ["HOME=/tmp"]

    def test_runs_the_command_through_a_shell_whatever_the_images_entrypoint(self):
        # Given an image whose entrypoint is its own tool
        # When the docker arguments are built
        arguments = _arguments()

        # Then the shell is the entrypoint and the command its one line
        assert ("--entrypoint", "sh") in _pairs(arguments)
        assert arguments[-3:] == ["hashicorp/terraform:1.9", "-c", "terraform fmt -check"]

    def test_keeps_an_image_download_out_of_the_output(self):
        # Given an image that may not have been downloaded yet
        # When the docker arguments are built
        # Then docker is told to keep the download's progress to itself
        assert "--quiet" in _arguments()

    def test_gives_the_network_only_when_asked(self):
        # Given a repository whose checks declare that they need the network
        # When the docker arguments are built
        # Then the container is on the default network instead of none
        assert ("--network", "bridge") in _pairs(_arguments(network=True))


class FakeProcess:
    def __init__(self, *, returncode=0, output=b"", hangs=False):
        # A process that is still running has no exit code yet.
        self.returncode = None if hangs else returncode
        self.output = output
        self.hangs = hangs
        self.killed = False

    async def communicate(self):
        if self.hangs:
            await asyncio.Event().wait()
        return self.output, None

    async def wait(self):
        return 0

    def kill(self):
        self.killed = True


@pytest.fixture
def spawn(monkeypatch):
    """
    Replace process spawning: every call is recorded, and answered with the
    next scripted process — or raises it, if it is an exception.
    """
    calls, script = [], []

    async def fake_exec(*arguments, **kwargs):
        calls.append(list(arguments))
        answer = script.pop(0) if script else FakeProcess()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls, script


async def _run(**overrides):
    given = {
        "image": "hashicorp/terraform:1.9",
        "command": "terraform fmt -check",
        "workdir": WORKDIR,
        "network": False,
        "timeout_seconds": 5,
    }
    return await docker.DockerSandbox().run(**(given | overrides))


class TestDockerSandbox:
    async def test_returns_the_commands_exit_code_and_what_it_printed(self, spawn):
        # Given a container whose command fails with a message
        calls, script = spawn
        script.append(FakeProcess(returncode=3, output=b"quotas.tfvars\n"))

        # When the command is run
        result = await _run()

        # Then its exit code and output come back, from one docker run
        assert result == (3, "quotas.tfvars\n")
        assert calls[0][:2] == ["docker", "run"]

    @pytest.mark.parametrize(
        "answer",
        [
            FileNotFoundError("docker"),
            FakeProcess(returncode=125, output=b"Cannot connect to the Docker daemon"),
        ],
        ids=["docker is not installed", "the daemon cannot be reached"],
    )
    async def test_says_the_sandbox_is_unavailable_rather_than_that_a_check_failed(
        self, spawn, answer
    ):
        # Given a host where docker cannot start the container at all
        _calls, script = spawn
        script.append(answer)

        # When a command is run
        # Then that is an unavailable sandbox — an OSError — not an exit code
        with pytest.raises(docker.SandboxUnavailable) as raised:
            await _run()
        assert isinstance(raised.value, OSError)

    async def test_stops_the_container_when_the_command_runs_out_of_time(self, spawn):
        # Given a container whose command never finishes
        calls, script = spawn
        hanging = FakeProcess(hangs=True)
        script.append(hanging)

        # When it is run with a short time limit
        with pytest.raises(TimeoutError):
            await _run(timeout_seconds=0.01)
        await asyncio.sleep(0.01)

        # Then the client is ended and the container itself is told to stop
        container = next(value for flag, value in _pairs(calls[0]) if flag == "--name")
        assert hanging.killed is True
        assert calls[1] == ["docker", "kill", container]

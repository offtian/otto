"""
Docker as a sandbox — no business logic. ``DockerSandbox`` structurally
implements the domain ``Sandbox`` port (``domain/support/checks``): one
command, in one throwaway container, over one directory.

The container gets as little as a check needs and nothing else:

  - no network, unless the caller asks for it;
  - no environment from this process, so no credentials;
  - a read-only root filesystem, with ``/tmp`` in memory;
  - no Linux capabilities, and no way to gain privileges;
  - limits on processes, memory and CPU;
  - this process's own user id, never root, so it can write only what this
    process could — which inside the container is the mounted directory.

ponytail: a container shares the host's kernel, so this is a fence, not a
vault. For changes from a source you do not trust at all, run the same
commands under a stronger runtime (gVisor's ``runsc``, a microVM) — the
arguments are one function to change.
"""

import asyncio
import os
import pathlib
import uuid

import attrs


# docker run's own exit code for "the container could not be started".
_COULD_NOT_START = 125

# Stop requests that must outlive the call that made them.
_stopping: set[asyncio.Task[None]] = set()


class SandboxUnavailable(OSError):
    """
    Raised when Docker could not run the command at all: not installed, the
    daemon unreachable, the image unavailable. An ``OSError``, which is what
    the ``Sandbox`` port says a sandbox raises when it cannot start.
    """


def arguments(
    *, name: str, image: str, command: str, workdir: pathlib.Path, network: bool
) -> list[str]:
    """
    Return the ``docker`` arguments that run one shell command in a
    throwaway container, fenced in as this module's docstring says.
    """
    return [
        "run",
        "--rm",
        # An image that is not local yet is downloaded first, and without
        # this the download's progress is the start of the check's output.
        "--quiet",
        "--name",
        name,
        "--network",
        "bridge" if network else "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,size=512m",  # noqa: S108 - a path inside the container, not on this host
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp",
        "--volume",
        f"{workdir}:/work",
        "--workdir",
        "/work",
        # Images often set their tool as the entrypoint; the command is a
        # shell line, so the shell has to be it.
        "--entrypoint",
        "sh",
        image,
        "-c",
        command,
    ]


@attrs.frozen
class DockerSandbox:
    """
    Runs a command in a throwaway container through the ``docker`` CLI.
    """

    binary: str = "docker"

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
        Run one shell command in ``image`` over ``workdir`` and return its
        exit code and everything it printed, both streams in order.

        :raises TimeoutError: if it does not finish in time; the container
            is stopped.
        :raises SandboxUnavailable: if Docker could not start the container.
        """
        name = f"otto-check-{uuid.uuid4().hex[:12]}"
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                *arguments(
                    name=name, image=image, command=command, workdir=workdir, network=network
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"docker could not be run: {exc}") from exc
        try:
            async with asyncio.timeout(timeout_seconds):
                printed, _ = await process.communicate()
        except BaseException:
            # Timed out, or the caller gave up: ending the client does not
            # end the container, so it is told to stop on its own task.
            self._stop(name)
            if process.returncode is None:
                process.kill()
            raise
        output = printed.decode(errors="replace")
        if process.returncode == _COULD_NOT_START:
            raise SandboxUnavailable(output.strip()[-300:])
        return int(process.returncode or 0), output

    def _stop(self, name: str) -> None:
        task = asyncio.get_running_loop().create_task(self._kill(name))
        _stopping.add(task)
        task.add_done_callback(_stopping.discard)

    async def _kill(self, name: str) -> None:
        try:
            killer = await asyncio.create_subprocess_exec(
                self.binary,
                "kill",
                name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            # Docker went away: so did the container.
            return

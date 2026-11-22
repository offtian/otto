"""
Changes to code and configuration: how Otto turns a provisioning or
engineering-change request into a pull request on a catalogued repository.

An agent never writes to a repository directly. It reads files, proposes
exact-match edits, and the one write — ``open_pull_request`` — is a gated
tool: it pauses for a human approval that shows the edits, and only then
commits them on a new branch and opens the pull request. From there the
change is reviewed and merged (or not) under the repository's own rules.

What the approver sees is what gets applied: an edit replaces the one exact
occurrence of ``old`` with ``new``, so if the file moved underneath it the
edit fails instead of landing somewhere else; and a change too long to show
in full on its approval card is refused, so a decision never covers lines
nobody saw. Only the paths a repository's profile allows can be touched.
All three rules are checked in code, when a change is previewed and again
when it is applied — never left to the model.

A pull request Otto opens carries a signed return address — the
conversation that asked for it — so that what later happens to it (merged,
closed, changes requested) can be reported back there (``PullRequestEvent``).

Two ports. ``RepoHost`` is wherever repositories live: GitHub
(``vendors/github``), or a local directory (``LocalRepoHost``) in dev.
``ChangeReviewer`` is an independent second opinion on a staged change,
shown to the approver beside the card — advisory only: it never approves,
blocks or alters anything (an agent on any model, ``reviewer``).
"""

import difflib
import fnmatch
import hashlib
import hmac
import json
import pathlib
import posixpath
import re
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Protocol, TypedDict

import agents
import attrs

from otto.domain.support import agent as support_agent
from otto.domain.support import entities
from otto.utils import logs


OPEN_PULL_REQUEST = "open_pull_request"

# The most of a proposal an approval card shows before it is cut short — a
# Slack section block holds 3,000 characters and the card has its own text.
_CARD_LIMIT = 2400
# The most of one file a read hands back to the model.
_READ_LIMIT = 20_000


@attrs.frozen
class RepoProfile:
    """
    One repository in the catalog: Otto may read it and propose changes to
    the paths it allows.
    """

    # "owner/name", as the host addresses it.
    name: str
    description: str = ""
    # The branch changes are read from and proposed against.
    base: str = "main"
    # Glob patterns of the only paths a change may touch. Empty = none: the
    # repository can be read but never changed.
    paths: tuple[str, ...] = ()

    def allows(self, path: str) -> bool:
        """
        Test whether a change may touch this (already cleaned) path.
        """
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.paths)


@attrs.frozen
class FileEdit:
    """
    One exact-match edit: replace the single occurrence of ``old`` in the
    file with ``new``. An empty ``old`` creates the file with ``new`` as its
    whole content.
    """

    path: str
    old: str
    new: str


@attrs.frozen
class StagedFile:
    """
    One file as a set of edits would leave it. ``before`` is None for a file
    the edits create.
    """

    path: str
    before: str | None
    after: str


class EditRejected(Exception):
    """
    Raised when a proposed change may not or cannot be applied. The message
    is written for the agent that proposed it.
    """


class RepoHost(Protocol):
    """
    Where repositories live. Reads see a branch as it is now; the one write
    never touches an existing branch.
    """

    async def read_file(self, *, repo: str, path: str, ref: str) -> str | None:
        """
        Return the file's text at ``ref``, or None when it does not exist.
        """
        ...

    async def list_directory(self, *, repo: str, path: str, ref: str) -> list[str]:
        """
        Return the paths directly under a directory at ``ref``, sorted,
        directories ending in "/". An unknown directory is an empty list.
        """
        ...

    async def open_pull_request(
        self,
        *,
        repo: str,
        base: str,
        branch: str,
        title: str,
        body: str,
        files: Mapping[str, str],
    ) -> str:
        """
        Commit ``files`` (path → full new content) on a new ``branch`` off
        ``base`` and open a pull request for it. Return the address a human
        opens to review it.
        """
        ...


MERGED = "merged"
CLOSED = "closed"
CHANGES_REQUESTED = "changes_requested"


@attrs.frozen
class PullRequestEvent:
    """
    Something that happened to a pull request Otto opened, addressed to the
    conversation that asked for it.
    """

    # MERGED, CLOSED (without merging) or CHANGES_REQUESTED.
    kind: str
    url: str
    title: str
    origin: entities.Origin
    requester_id: str
    # A reviewer's own words, for CHANGES_REQUESTED. Untrusted.
    detail: str = ""


@attrs.frozen
class Review:
    """
    A second opinion on a staged change, for the human who decides.
    """

    approves: bool
    # What the approver should look at twice, most important first.
    findings: tuple[str, ...] = ()
    # Who gave it (a model name) — shown with the verdict, so the approver
    # knows what they are reading.
    reviewer: str = ""


class ChangeReviewer(Protocol):
    """
    A second pair of eyes on a proposed change, independent of the agent
    that wrote it.
    """

    async def review(
        self, *, profile: RepoProfile, title: str, summary: str, files: Sequence[StagedFile]
    ) -> Review:
        """
        Return an opinion on a staged change: whether it does what its title
        and summary say, and what the approver should look at twice.

        May raise or take a while — the caller treats a failed review as no
        review and never lets it hold up the approval.
        """
        ...


def apply_edit(*, content: str | None, edit: FileEdit) -> str:
    """
    Return a file's content with one edit applied.

    :param content: the file as it is now; None when it does not exist.
    :raises EditRejected: if the edit creates a file that exists, edits one
        that does not, or its text does not occur exactly once.
    """
    if not edit.old:
        if content is not None:
            raise EditRejected(
                f"{edit.path} already exists — to change it, give the exact text to replace"
            )
        return edit.new
    if content is None:
        raise EditRejected(f"{edit.path} does not exist — an empty `old` creates a file")
    occurrences = content.count(edit.old)
    if occurrences != 1:
        raise EditRejected(
            f"the text to replace occurs {occurrences} times in {edit.path}; it must "
            "occur exactly once — include more of the surrounding lines"
        )
    return content.replace(edit.old, edit.new, 1)


async def stage(
    *, host: RepoHost, profile: RepoProfile, edits: Sequence[FileEdit]
) -> list[StagedFile]:
    """
    Return every file the edits touch as they would leave it, read from the
    repository's base branch and edited in the order given.

    :raises EditRejected: if there are no edits, a path is outside the
        profile's allowlist, or an edit does not apply.
    """
    if not edits:
        raise EditRejected("no edits were given")
    before: dict[str, str | None] = {}
    after: dict[str, str] = {}
    for edit in edits:
        path = _clean_path(edit.path)
        if not profile.allows(path):
            raise EditRejected(
                f"{path} is outside the paths that may be changed in {profile.name} "
                f"({', '.join(profile.paths) or 'none'})"
            )
        if path not in before:
            before[path] = await host.read_file(repo=profile.name, path=path, ref=profile.base)
        current = after.get(path, before[path])
        after[path] = apply_edit(content=current, edit=attrs.evolve(edit, path=path))
    return [StagedFile(path=path, before=before[path], after=after[path]) for path in after]


async def stage_proposal(
    *, host: RepoHost, profiles: Mapping[str, RepoProfile], arguments: Mapping[str, Any]
) -> tuple[RepoProfile, list[StagedFile]]:
    """
    Return the repository profile and staged files of an ``open_pull_request``
    proposal — the one check of whether it may and can be applied, run when
    the approver is asked and again when the approved call executes.

    :raises EditRejected: if the repository is not catalogued, the proposal
        is too long to show in full on its approval card, a path is outside
        the allowlist, or an edit does not apply.
    """
    repo = str(arguments.get("repo", ""))
    if repo not in profiles:
        raise EditRejected(
            f"{repo!r} is not a repository you may work in — use one of: " + ", ".join(profiles)
        )
    ensure_reviewable(arguments)
    staged = await stage(
        host=host, profile=profiles[repo], edits=_edits(arguments.get("edits") or [])
    )
    return profiles[repo], staged


def render_diff(files: Sequence[StagedFile]) -> str:
    """
    Return the staged files as one unified diff.
    """
    lines: list[str] = []
    for file in files:
        lines.extend(
            difflib.unified_diff(
                (file.before or "").splitlines(),
                file.after.splitlines(),
                fromfile=f"a/{file.path}" if file.before is not None else "/dev/null",
                tofile=f"b/{file.path}",
                lineterm="",
            )
        )
    return "\n".join(lines)


def render_proposal(arguments: Mapping[str, Any]) -> str:
    """
    Render a paused ``open_pull_request`` call as mrkdwn for its approval
    card: the repository, the title and summary, then every edit as the
    lines it removes and the lines it adds — exactly what approval applies.

    A proposal too long for the card is cut short and says that approving it
    applies nothing: ``ensure_reviewable`` refuses it when the call runs, so
    a decision never covers lines the approver could not see.
    """
    rendered = _rendered(arguments)
    if len(rendered) <= _CARD_LIMIT:
        return rendered
    cut = rendered[:_CARD_LIMIT]
    if cut.count("```") % 2:
        cut += "\n```"
    return (
        cut + "\n_…cut short. This change is too long to show in full, so approving it "
        "applies nothing — it has to be split into smaller ones._"
    )


def ensure_reviewable(arguments: Mapping[str, Any]) -> None:
    """
    Check that a proposal fits on its approval card in full.

    :raises EditRejected: if it would be cut short there.
    """
    size = len(_rendered(arguments))
    if size > _CARD_LIMIT:
        raise EditRejected(
            f"the change is too long to show in full on its approval card ({size} "
            f"characters, limit {_CARD_LIMIT}) — split it into smaller pull requests"
        )


def _rendered(arguments: Mapping[str, Any]) -> str:
    parts = [
        f"*Repository:* `{arguments.get('repo', '')}`",
        f"*Pull request:* {arguments.get('title', '')}",
        str(arguments.get("summary", "")),
    ]
    for edit in arguments.get("edits") or []:
        if not isinstance(edit, Mapping):
            continue
        removed = [f"- {line}" for line in _fenced(edit.get("old")).splitlines()]
        added = [f"+ {line}" for line in _fenced(edit.get("new")).splitlines()]
        parts.append(f"*`{edit.get('path', '')}`*\n```" + "\n".join(removed + added) + "```")
    return "\n".join(parts)


def _fenced(text: object) -> str:
    """
    Return text safe to show inside a code fence: a fence of its own would
    end the block early and let an edit dress the rest of the card up.
    """
    return str(text or "").replace("```", "`\u200b`\u200b`")


# The most findings, and the most of each, a second opinion shows.
_MAX_FINDINGS = 5
_FINDING_LIMIT = 300


def render_review(review: Review) -> str:
    """
    Render a second opinion as mrkdwn for the approver. Findings are a
    model's words about untrusted content, so they are shown as plain text:
    nothing in them can mention a channel or pose as a link.
    """
    reviewer = f" ({_plain(review.reviewer)})" if review.reviewer else ""
    if review.approves and not review.findings:
        return f":mag: *Second opinion*{reviewer}: no concerns with this change."
    verdict = "no objection, with notes" if review.approves else "look twice before approving"
    findings = "\n".join(
        f"• {_plain(finding)[:_FINDING_LIMIT]}" for finding in review.findings[:_MAX_FINDINGS]
    )
    icon = ":mag:" if review.approves else ":warning:"
    return f"{icon} *Second opinion*{reviewer}: {verdict}.\n{findings}"


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pull_request_body(
    *, summary: str, context: support_agent.SupportContext, signing_key: str = ""
) -> str:
    """
    Return the pull request description: the agent's summary, who asked and
    where, and — hidden, when there is a key to sign it with — the return
    address a later event on the pull request is reported back to.
    """
    body = (
        f"{summary}\n\n---\n"
        f"Opened by Otto for `{context.requester_id}` ({context.origin_ref}) after a "
        "human approved the automation. Review the change itself as you would any other."
    )
    if context.origin is not None and signing_key:
        address = return_address(
            origin=context.origin, requester_id=context.requester_id, key=signing_key
        )
        body += f"\n{address}"
    return body


# The hidden line: hex of the address, then hex of its signature.
_RETURN_ADDRESS = re.compile(r"<!-- otto:([0-9a-f]+):([0-9a-f]{64}) -->")


def return_address(*, origin: entities.Origin, requester_id: str, key: str) -> str:
    """
    Return the hidden line a pull request description carries so that what
    happens to it can be reported back: the conversation and who asked,
    signed with ``key``.

    Signed because anyone who can edit a pull request can edit its
    description: unsigned, they could point Otto at any conversation it is
    able to post in.
    """
    payload = (
        json.dumps({"origin": entities.origin_to_json(origin), "requester": requester_id})
        .encode()
        .hex()
    )
    return f"<!-- otto:{payload}:{_sign(payload, key=key)} -->"


def read_return_address(body: str, *, key: str) -> tuple[entities.Origin, str] | None:
    """
    Return the conversation and requester a pull request description points
    back to, or None when it carries no return address, or one that ``key``
    did not sign. Fails closed without a key.
    """
    found = _RETURN_ADDRESS.search(body)
    if found is None or not key:
        return None
    payload, signature = found.groups()
    if not hmac.compare_digest(signature, _sign(payload, key=key)):
        return None
    address = json.loads(bytes.fromhex(payload))
    return entities.origin_from_json(address["origin"]), str(address["requester"])


def _sign(payload: str, *, key: str) -> str:
    # Prefixed, so a signature made for a return address can never be one
    # for anything else signed with the same key.
    message = b"otto-return-address:" + payload.encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


_GUIDANCE = """\
- Repositories: you can propose changes to these, as pull requests:
{repos}
  For a provisioning or configuration request they cover, read the relevant
  files first, call preview_change until your edits apply cleanly, then call
  open_pull_request. It always needs a human approval — tell the user it was
  sent for approval and never promise the outcome. A pull request exists
  only when open_pull_request returned its address: never state that you
  opened or will open one as text alone. Collect what the change needs from
  the user — never invent a name, a size or an owner.
"""


def guidance(repos: Sequence[RepoProfile]) -> str:
    """
    Return what the agent that mounts the repository tools is told about
    them: which repositories, which paths, and the order of work.
    """
    listed = "\n".join(
        f"  - {repo.name} (branch {repo.base}): {repo.description or 'no description'} "
        f"Changeable paths: {', '.join(repo.paths) or 'none'}."
        for repo in repos
    )
    return _GUIDANCE.format(repos=listed)


class EditArguments(TypedDict):
    """
    One edit as an agent passes it to the repository tools.
    """

    path: str
    old: str
    new: str


_UNREACHABLE = "The repository could not be reached — say so and offer to escalate."


@attrs.frozen
class RepoTools:
    """
    The repository tools, bound to the catalogued repositories and the host
    they live on. Listing, reading and previewing run
    freely; ``open_pull_request`` — the only write — always pauses for
    approval, so these belong on top-level agents only.

    Every tool refuses a repository outside ``profiles``, and none raises:
    what went wrong comes back as text for the agent to act on. The method
    docstrings are the tool descriptions the model reads.
    """

    profiles: Mapping[str, RepoProfile]
    host: RepoHost
    # Signs the return address each pull request carries. Empty = none is
    # written, and nothing that happens to the pull request is reported back.
    signing_key: str = attrs.field(default="", repr=False)

    def as_tools(self) -> list[agents.Tool]:
        """
        Return all four tools, ready to mount on a top-level agent.
        """
        return [
            *self.read_only_tools(),
            agents.function_tool(self.preview_change),
            agents.function_tool(self.open_pull_request, needs_approval=True),
        ]

    def read_only_tools(self) -> list[agents.Tool]:
        """
        Return only the tools that look — listing and reading — for an agent
        that must never propose or write, such as the reviewer.
        """
        return [
            agents.function_tool(self.list_repo_files),
            agents.function_tool(self.read_repo_file),
        ]

    async def list_repo_files(self, repo: str, path: str) -> str:
        """
        List the files and directories directly under a path of a catalogued
        repository ("" for its root). Directories end in "/".
        """
        return await _answered("list_repo_files", self._list(repo=repo, path=path))

    async def read_repo_file(self, repo: str, path: str) -> str:
        """
        Return the content of one file of a catalogued repository, as it is
        on the branch changes are proposed against. Read a file before you
        propose an edit to it.
        """
        return await _answered("read_repo_file", self._read(repo=repo, path=path))

    async def preview_change(self, repo: str, edits: list[EditArguments]) -> str:
        """
        Check that a set of edits applies cleanly and return the resulting
        diff, without changing anything. Each edit replaces the one exact
        occurrence of `old` in the file at `path` with `new`; an empty `old`
        creates the file. Always preview before open_pull_request, and fix
        whatever the preview rejects.
        """
        return await _answered("preview_change", self._preview(repo=repo, edits=edits))

    async def open_pull_request(
        self,
        context: agents.RunContextWrapper[support_agent.SupportContext],
        repo: str,
        title: str,
        summary: str,
        edits: list[EditArguments],
    ) -> str:
        """
        Open a pull request with a set of edits (the same shape as
        preview_change) on a catalogued repository. Requires human approval
        before it runs; the approver sees these exact edits. summary is the
        pull request description: what changes and why, for the reviewer.
        """
        return await _answered(
            OPEN_PULL_REQUEST,
            self._open(
                context=context.context, repo=repo, title=title, summary=summary, edits=edits
            ),
        )

    def _profile(self, repo: str) -> RepoProfile:
        if repo not in self.profiles:
            raise EditRejected(
                f"{repo!r} is not a repository you may work in — use one of: "
                + ", ".join(self.profiles)
            )
        return self.profiles[repo]

    async def _list(self, *, repo: str, path: str) -> str:
        profile = self._profile(repo)
        listed = await self.host.list_directory(
            repo=profile.name, path=path.strip("/"), ref=profile.base
        )
        return "\n".join(listed) if listed else f"Nothing found under {path!r}."

    async def _read(self, *, repo: str, path: str) -> str:
        profile = self._profile(repo)
        content = await self.host.read_file(repo=profile.name, path=path, ref=profile.base)
        if content is None:
            return f"{path} does not exist in {repo}."
        if len(content) > _READ_LIMIT:
            return content[:_READ_LIMIT] + "\n…(the file continues; this is its beginning)"
        return content

    async def _preview(self, *, repo: str, edits: Sequence[Mapping[str, Any]]) -> str:
        _, staged = await stage_proposal(
            host=self.host, profiles=self.profiles, arguments={"repo": repo, "edits": edits}
        )
        return render_diff(staged)

    async def _open(
        self,
        *,
        context: support_agent.SupportContext,
        repo: str,
        title: str,
        summary: str,
        edits: Sequence[Mapping[str, Any]],
    ) -> str:
        profile, staged = await stage_proposal(
            host=self.host,
            profiles=self.profiles,
            arguments={"repo": repo, "title": title, "summary": summary, "edits": edits},
        )
        url = await self.host.open_pull_request(
            repo=profile.name,
            base=profile.base,
            branch=f"otto/{_slug(title)}-{uuid.uuid4().hex[:8]}",
            title=title,
            body=pull_request_body(summary=summary, context=context, signing_key=self.signing_key),
            files={file.path: file.after for file in staged},
        )
        return f"Pull request opened: {url}"


async def _answered(tool: str, operation: Awaitable[str]) -> str:
    """
    Await one tool operation and return its answer — or, instead of raising,
    the reason it was refused or the fact that the host is unreachable.
    """
    try:
        return await operation
    except EditRejected as exc:
        return f"Rejected: {exc}"
    except Exception as exc:
        logs.log_exception(exc, params={"tool": tool})
        return _UNREACHABLE


def build_change_tools(
    *, repos: Sequence[RepoProfile], host: RepoHost, signing_key: str = ""
) -> list[agents.Tool]:
    """
    Return the repository tools over ``repos`` for one top-level agent.

    :param signing_key: signs the return address on each pull request; empty
        = pull requests carry none.
    """
    return RepoTools(
        profiles={profile.name: profile for profile in repos}, host=host, signing_key=signing_key
    ).as_tools()


@attrs.frozen
class LocalRepoHost:
    """
    ``RepoHost`` over a directory of checked-out repositories
    (``<root>/<owner>/<name>/…``) — the dev stand-in for a real host. Reads
    are real; "opening a pull request" writes nothing and returns a
    placeholder address, so the whole flow, approval included, runs with no
    GitHub. ``ref`` is ignored: the directory is the branch.
    """

    root: pathlib.Path

    async def read_file(self, *, repo: str, path: str, ref: str) -> str | None:
        target = self._resolve(repo, path)
        return target.read_text(encoding="utf-8") if target.is_file() else None

    async def list_directory(self, *, repo: str, path: str, ref: str) -> list[str]:
        target = self._resolve(repo, path)
        if not target.is_dir():
            return []
        base = self._resolve(repo, "")
        return sorted(
            entry.relative_to(base).as_posix() + ("/" if entry.is_dir() else "")
            for entry in target.iterdir()
        )

    async def open_pull_request(
        self,
        *,
        repo: str,
        base: str,
        branch: str,
        title: str,
        body: str,
        files: Mapping[str, str],
    ) -> str:
        logs.log_event(
            "local_pull_request", params={"repo": repo, "branch": branch, "files": len(files)}
        )
        return f"local://{repo}/pull/{branch}"

    def _resolve(self, repo: str, path: str) -> pathlib.Path:
        base = (self.root / repo).resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not base.is_relative_to(self.root.resolve()):
            raise EditRejected(f"{path} is not a path inside {repo}")
        return target


def _edits(arguments: Sequence[Mapping[str, Any]]) -> list[FileEdit]:
    return [
        FileEdit(
            path=str(edit.get("path", "")),
            old=str(edit.get("old", "")),
            new=str(edit.get("new", "")),
        )
        for edit in arguments
    ]


def _clean_path(path: str) -> str:
    """
    Return a repository-relative path in its one canonical spelling, so the
    allowlist judges the path that will actually be written.

    :raises EditRejected: if the path is empty, absolute, or climbs out of
        the repository.
    """
    cleaned = posixpath.normpath(path.strip().replace("\\", "/"))
    if cleaned in {"", "."} or cleaned.startswith(("/", "../")) or cleaned == "..":
        raise EditRejected(f"{path!r} is not a path inside the repository")
    return cleaned


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"


def proposal_arguments(tool_arguments: str) -> Mapping[str, Any] | None:
    """
    Return a paused call's arguments when they are an ``open_pull_request``
    proposal — a repository plus edits — and None for anything else.
    """
    try:
        arguments = json.loads(tool_arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(arguments, dict) and "repo" in arguments and "edits" in arguments:
        return arguments
    return None

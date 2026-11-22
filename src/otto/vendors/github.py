"""
GitHub REST wrapper — no business logic. ``GitHubRepoHost`` structurally
implements the domain ``RepoHost`` port (``domain/support/changes``): reads
off a branch or a commit, one write that never touches an existing branch —
a new branch, one commit, one pull request — and one that adds a commit to a
branch it is given, never forced. Which branch that may be is the caller's
rule, not this module's. Webhook-signature verification lives here too, so
the interface layer stays translation-only.

The token should be a GitHub App installation token or a fine-grained PAT
scoped to the catalogued repositories with Contents and Pull requests
read/write — nothing else is called here.
"""

import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping
from typing import Any

import attrs
import httpx


_API_VERSION = "2022-11-28"


def verify_webhook(*, secret: str, body: bytes, signature: str) -> bool:
    """
    Test whether an inbound GitHub webhook was signed with the shared secret
    (the ``X-Hub-Signature-256`` header: ``sha256=`` + HMAC-SHA256 of the raw
    body). Fails closed when no secret is configured.
    """
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@attrs.frozen
class GitHubRepoHost:
    """
    Thin async wrapper around the GitHub REST API for one credential.
    """

    client: httpx.AsyncClient

    async def read_file(self, *, repo: str, path: str, ref: str) -> str | None:
        """
        Return the file's text at ``ref``, or None when it does not exist.
        """
        response = await self.client.get(
            f"/repos/{repo}/contents/{urllib.parse.quote(path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.text

    async def list_directory(self, *, repo: str, path: str, ref: str) -> list[str]:
        """
        Return the paths directly under a directory at ``ref``, sorted,
        directories ending in "/". An unknown directory is an empty list.
        """
        response = await self.client.get(
            f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params={"ref": ref}
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return []
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list):
            # The path names a file, which the API answers as one object.
            return [str(entries.get("path", path))]
        return sorted(
            str(entry["path"]) + ("/" if entry.get("type") == "dir" else "") for entry in entries
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
        """
        Commit ``files`` (path → full new content) on a new ``branch`` off
        ``base`` and open a pull request for it; return its web address.

        One commit through the Git Data API: tree on top of the base
        commit's tree, commit, branch ref, pull request. A failure part-way
        leaves at most an unreferenced commit or an orphan branch — never a
        change to ``base``.

        ponytail: every file is written with mode 100644, so an edited
        executable loses its bit. Carry the mode over from the base tree if
        a catalogued repository ever has scripts in its allowed paths.

        :raises httpx.HTTPStatusError: if GitHub refuses any step.
        """
        head = (await self._get(f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base)}"))[
            "object"
        ]["sha"]
        commit = await self._commit(repo=repo, parent=head, message=title, files=files)
        await self._post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        pull = await self._post(
            f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body}
        )
        return str(pull["html_url"])

    async def get_pull_request(self, *, repo: str, number: int) -> Mapping[str, Any] | None:
        """
        Return a pull request's fields as the ``RepoHost`` port names them,
        or None when the repository has no pull request with that number.

        :raises httpx.HTTPStatusError: if GitHub refuses the read.
        """
        response = await self.client.get(f"/repos/{repo}/pulls/{number}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        pull = response.json()
        head, base = pull["head"], pull["base"]
        return {
            "number": pull["number"],
            "url": pull["html_url"],
            "title": pull["title"],
            "body": pull.get("body") or "",
            "branch": head["ref"],
            "head": head["sha"],
            "base": base["ref"],
            "open": pull["state"] == "open",
            # A fork that has since been deleted reports no repository at all.
            "same_repo": (head.get("repo") or {}).get("full_name") == base["repo"]["full_name"],
        }

    async def push_commit(
        self, *, repo: str, branch: str, parent: str, message: str, files: Mapping[str, str]
    ) -> None:
        """
        Add one commit with ``files`` (path → full new content) to an
        existing ``branch``, on top of the commit ``parent``.

        The branch is moved without force, so if it is no longer at
        ``parent`` GitHub refuses the move and the new commit is left
        unreferenced: nobody's work on the branch is written over.

        :raises httpx.HTTPStatusError: if GitHub refuses any step.
        """
        commit = await self._commit(repo=repo, parent=parent, message=message, files=files)
        response = await self.client.patch(
            f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch)}",
            json={"sha": commit, "force": False},
        )
        response.raise_for_status()

    async def download(self, *, repo: str, ref: str) -> bytes:
        """
        Return the whole repository at ``ref`` as a gzipped tar archive, as
        GitHub packs it: every file under one top-level directory.

        :raises httpx.HTTPStatusError: if GitHub refuses the read.
        """
        # GitHub answers with a redirect to where the archive is served.
        response = await self.client.get(
            f"/repos/{repo}/tarball/{urllib.parse.quote(ref)}", follow_redirects=True
        )
        response.raise_for_status()
        return response.content

    async def _commit(
        self, *, repo: str, parent: str, message: str, files: Mapping[str, str]
    ) -> str:
        """
        Create one commit with ``files`` on top of ``parent`` and return its
        id. Nothing refers to it until a branch is pointed at it.
        """
        base_tree = (await self._get(f"/repos/{repo}/git/commits/{parent}"))["tree"]["sha"]
        tree = await self._post(
            f"/repos/{repo}/git/trees",
            {
                "base_tree": base_tree,
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": content}
                    for path, content in files.items()
                ],
            },
        )
        commit = await self._post(
            f"/repos/{repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [parent]},
        )
        return str(commit["sha"])

    async def _get(self, url: str) -> Any:
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def _post(self, url: str, payload: Mapping[str, Any]) -> Any:
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def build_repo_host(*, api_url: str, token: str) -> GitHubRepoHost:
    """
    Return the GitHub repository host for one API endpoint and credential
    (``https://api.github.com``, or a GitHub Enterprise ``/api/v3`` root).
    """
    return GitHubRepoHost(
        client=httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            },
            timeout=15.0,
        )
    )

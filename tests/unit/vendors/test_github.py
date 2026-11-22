import hashlib
import hmac
import json

import httpx

from otto.vendors import github


def _host(handler) -> github.GitHubRepoHost:
    return github.GitHubRepoHost(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(handler)
        )
    )


class TestVerifyWebhook:
    BODY = b'{"action": "closed"}'

    def _signature(self, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), self.BODY, hashlib.sha256).hexdigest()

    def test_accepts_a_body_signed_with_the_shared_secret(self):
        # Given a body and the signature GitHub computes for it
        # When it is verified against the same secret
        # Then it is accepted
        assert github.verify_webhook(
            secret="hook-secret", body=self.BODY, signature=self._signature("hook-secret")
        )

    def test_rejects_a_wrong_signature_and_fails_closed_without_a_secret(self):
        # Given a signature made with another secret, and an unconfigured secret
        # When each is verified
        # Then neither is accepted
        assert not github.verify_webhook(
            secret="hook-secret", body=self.BODY, signature=self._signature("other")
        )
        assert not github.verify_webhook(secret="", body=self.BODY, signature=self._signature(""))


class TestReadFile:
    async def test_returns_the_raw_text_at_the_ref(self):
        # Given a repository that has the file on the asked branch
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.url.params["ref"], request.headers["accept"]))
            return httpx.Response(200, text="workspace_quotas = {}\n")

        # When the file is read
        content = await _host(handler).read_file(
            repo="acme/infra-live", path="coder/quotas.tfvars", ref="main"
        )

        # Then its raw text comes back, asked for at that branch
        assert content == "workspace_quotas = {}\n"
        assert seen == [
            (
                "/repos/acme/infra-live/contents/coder/quotas.tfvars",
                "main",
                "application/vnd.github.raw+json",
            )
        ]

    async def test_returns_none_for_a_file_that_does_not_exist(self):
        # Given a repository without the file
        # When the file is read
        content = await _host(lambda request: httpx.Response(404, json={})).read_file(
            repo="acme/infra-live", path="coder/missing.tfvars", ref="main"
        )

        # Then there is no content — a new file, not an error
        assert content is None


class TestListDirectory:
    async def test_marks_directories_and_sorts(self):
        # Given a directory holding a file and a subdirectory
        entries = [
            {"path": "coder/templates", "type": "dir"},
            {"path": "coder/quotas.tfvars", "type": "file"},
        ]

        # When it is listed
        listed = await _host(lambda request: httpx.Response(200, json=entries)).list_directory(
            repo="acme/infra-live", path="coder", ref="main"
        )

        # Then both come back sorted, the directory ending in a slash
        assert listed == ["coder/quotas.tfvars", "coder/templates/"]


class TestOpenPullRequest:
    async def test_commits_once_on_a_new_branch_and_opens_the_pull_request(self):
        # Given a repository whose main branch is at one commit
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, payload))
            answers = {
                "/repos/acme/infra-live/git/ref/heads/main": {"object": {"sha": "head-sha"}},
                "/repos/acme/infra-live/git/commits/head-sha": {"tree": {"sha": "base-tree"}},
                "/repos/acme/infra-live/git/trees": {"sha": "new-tree"},
                "/repos/acme/infra-live/git/commits": {"sha": "new-commit"},
                "/repos/acme/infra-live/git/refs": {"ref": "refs/heads/otto/raise-quota"},
                "/repos/acme/infra-live/pulls": {
                    "html_url": "https://github.com/acme/infra-live/pull/7"
                },
            }
            return httpx.Response(200, json=answers[request.url.path])

        # When a pull request is opened with one edited file
        url = await _host(handler).open_pull_request(
            repo="acme/infra-live",
            base="main",
            branch="otto/raise-quota",
            title="Raise the AI team's CPU quota",
            body="Requested for the Q4 training runs.",
            files={"coder/quotas.tfvars": "workspace_quotas = {}\n"},
        )

        # Then the file is committed on top of main's tree, the new branch
        # points at that commit, and the pull request targets main
        writes = [(path, payload) for method, path, payload in calls if method == "POST"]
        assert url == "https://github.com/acme/infra-live/pull/7"
        assert writes == [
            (
                "/repos/acme/infra-live/git/trees",
                {
                    "base_tree": "base-tree",
                    "tree": [
                        {
                            "path": "coder/quotas.tfvars",
                            "mode": "100644",
                            "type": "blob",
                            "content": "workspace_quotas = {}\n",
                        }
                    ],
                },
            ),
            (
                "/repos/acme/infra-live/git/commits",
                {
                    "message": "Raise the AI team's CPU quota",
                    "tree": "new-tree",
                    "parents": ["head-sha"],
                },
            ),
            (
                "/repos/acme/infra-live/git/refs",
                {"ref": "refs/heads/otto/raise-quota", "sha": "new-commit"},
            ),
            (
                "/repos/acme/infra-live/pulls",
                {
                    "title": "Raise the AI team's CPU quota",
                    "head": "otto/raise-quota",
                    "base": "main",
                    "body": "Requested for the Q4 training runs.",
                },
            ),
        ]


def _pull_request_payload(**overrides) -> dict:
    return {
        "number": 7,
        "html_url": "https://github.com/acme/infra-live/pull/7",
        "title": "Raise the AI team's CPU quota",
        "body": None,
        "state": "open",
        "head": {
            "ref": "otto/raise-quota",
            "sha": "head-sha",
            "repo": {"full_name": "acme/infra-live"},
        },
        "base": {"ref": "main", "repo": {"full_name": "acme/infra-live"}},
    } | overrides


class TestGetPullRequest:
    async def test_returns_the_fields_the_port_names(self):
        # Given an open pull request from a branch of the same repository
        host = _host(lambda request: httpx.Response(200, json=_pull_request_payload()))

        # When it is read
        fields = await host.get_pull_request(repo="acme/infra-live", number=7)

        # Then its fields come back under the port's names, an absent
        # description as an empty one
        assert fields == {
            "number": 7,
            "url": "https://github.com/acme/infra-live/pull/7",
            "title": "Raise the AI team's CPU quota",
            "body": "",
            "branch": "otto/raise-quota",
            "head": "head-sha",
            "base": "main",
            "open": True,
            "same_repo": True,
        }

    async def test_tells_a_forks_branch_and_a_closed_pull_request_apart(self):
        # Given a closed pull request whose fork has since been deleted
        payload = _pull_request_payload(
            state="closed", head={"ref": "patch-1", "sha": "fork-sha", "repo": None}
        )
        host = _host(lambda request: httpx.Response(200, json=payload))

        # When it is read
        fields = await host.get_pull_request(repo="acme/infra-live", number=7)

        # Then it is neither open nor on a branch of this repository
        assert (fields["open"], fields["same_repo"]) == (False, False)

    async def test_returns_none_when_there_is_no_such_pull_request(self):
        # Given a repository without that pull request
        # When it is read
        fields = await _host(lambda request: httpx.Response(404, json={})).get_pull_request(
            repo="acme/infra-live", number=8
        )

        # Then there is nothing — not an error
        assert fields is None


class TestPushCommit:
    async def test_commits_on_the_given_parent_and_moves_the_branch_without_force(self):
        # Given a branch whose commit is known
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, payload))
            answers = {
                "/repos/acme/infra-live/git/commits/head-sha": {"tree": {"sha": "base-tree"}},
                "/repos/acme/infra-live/git/trees": {"sha": "new-tree"},
                "/repos/acme/infra-live/git/commits": {"sha": "new-commit"},
                "/repos/acme/infra-live/git/refs/heads/otto/raise-quota": {
                    "ref": "refs/heads/otto/raise-quota"
                },
            }
            return httpx.Response(200, json=answers[request.url.path])

        # When a commit is pushed on top of it
        await _host(handler).push_commit(
            repo="acme/infra-live",
            branch="otto/raise-quota",
            parent="head-sha",
            message="Name the team lead",
            files={"coder/quotas.tfvars": "workspace_quotas = {}\n"},
        )

        # Then the commit's parent is that commit, and the branch is moved to
        # it without force — so a branch that had moved on would refuse
        writes = [(method, path, payload) for method, path, payload in calls if method != "GET"]
        assert writes == [
            (
                "POST",
                "/repos/acme/infra-live/git/trees",
                {
                    "base_tree": "base-tree",
                    "tree": [
                        {
                            "path": "coder/quotas.tfvars",
                            "mode": "100644",
                            "type": "blob",
                            "content": "workspace_quotas = {}\n",
                        }
                    ],
                },
            ),
            (
                "POST",
                "/repos/acme/infra-live/git/commits",
                {"message": "Name the team lead", "tree": "new-tree", "parents": ["head-sha"]},
            ),
            (
                "PATCH",
                "/repos/acme/infra-live/git/refs/heads/otto/raise-quota",
                {"sha": "new-commit", "force": False},
            ),
        ]

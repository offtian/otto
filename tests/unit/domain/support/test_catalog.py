import pathlib
import textwrap

import pytest
from agents.models.interface import Model

from otto.domain.support import agent as support_agent
from otto.domain.support import catalog, changes, specialists


CATALOG_YAML = textwrap.dedent(
    """
    specialists:
      - name: coder
        description: Reads Coder workspaces.
        mcp: coder
        tools: [get_workspace, get_template]
        instructions: You are the Coder specialist.
    repos:
      - name: acme/infra-live
        description: Workspace quotas.
        paths: ["coder/*"]
    """
)
REPO_ROOT = pathlib.Path(__file__).parents[4]


class _NeverCalledModel(Model):
    async def get_response(self, *args, **kwargs):
        raise AssertionError("the model must not be called at build time")

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def _write(tmp_path, text: str) -> pathlib.Path:
    path = tmp_path / "otto.yaml"
    path.write_text(text)
    return path


class TestLoadCatalog:
    def test_parses_the_two_flat_lists(self, tmp_path):
        # Given a catalog file declaring one specialist and one repository
        # When it is loaded
        loaded = catalog.load_catalog(_write(tmp_path, CATALOG_YAML))

        # Then both come back as profiles, the repository on its default branch
        assert loaded == catalog.Catalog(
            specialists=(
                specialists.SpecialistProfile(
                    name="coder",
                    description="Reads Coder workspaces.",
                    instructions="You are the Coder specialist.",
                    mcp="coder",
                    tools=("get_workspace", "get_template"),
                ),
            ),
            repos=(
                changes.RepoProfile(
                    name="acme/infra-live",
                    description="Workspace quotas.",
                    base="main",
                    paths=("coder/*",),
                ),
            ),
        )

    def test_a_missing_file_is_an_empty_catalog(self, tmp_path):
        # Given no catalog file
        # When it is loaded
        # Then nothing is declared — the catalog is optional
        assert catalog.load_catalog(tmp_path / "absent.yaml") == catalog.Catalog()

    def test_a_specialist_declared_twice_fails_at_startup(self, tmp_path):
        # Given a catalog file that names the same specialist twice
        duplicated = "specialists:\n  - name: coder\n  - name: coder\n"

        # When it is loaded
        # Then the typo surfaces now, not as a tool-name clash per request
        with pytest.raises(ValueError, match="twice"):
            catalog.load_catalog(_write(tmp_path, duplicated))

    def test_the_example_documented_in_otto_yaml_is_loadable(self, tmp_path):
        # Given the commented example at the bottom of the shipped otto.yaml
        shipped = (REPO_ROOT / "otto.yaml").read_text().splitlines()
        example = shipped[shipped.index("# specialists:") :]
        uncommented = "\n".join(line.removeprefix("#").removeprefix(" ") for line in example)

        # When it is uncommented and loaded
        loaded = catalog.load_catalog(_write(tmp_path, uncommented))

        # Then it declares what the comments promise
        assert [profile.name for profile in loaded.specialists] == ["coder", "jenkins"]
        assert [repo.name for repo in loaded.repos] == ["acme/infra-live"]


class TestMount:
    def _mounted(self, loaded: catalog.Catalog, *, repo_host=None):
        return catalog.mount(
            support_agent.build_agent(model=_NeverCalledModel()),
            catalog=loaded,
            model=_NeverCalledModel(),
            specialist_mcp_tools={},
            repo_host=repo_host,
        )

    def test_an_empty_catalog_leaves_the_agent_as_built(self):
        # Given nothing declared
        # When the catalog is mounted
        agent = self._mounted(catalog.Catalog())

        # Then the agent has its built-in instructions and tools, no more
        built = support_agent.build_agent(model=_NeverCalledModel())
        assert agent.instructions == support_agent.INSTRUCTIONS
        assert [tool.name for tool in agent.tools] == [tool.name for tool in built.tools]

    def test_specialists_mount_as_consult_tools_with_their_guidance(self, tmp_path):
        # Given a catalog declaring a specialist
        loaded = catalog.load_catalog(_write(tmp_path, CATALOG_YAML))

        # When it is mounted
        agent = self._mounted(loaded)

        # Then the agent can consult it and is told how
        assert "consult_coder" in {tool.name for tool in agent.tools}
        assert "consult_*" in str(agent.instructions)

    def test_repositories_mount_only_with_a_host_and_only_the_write_is_gated(self, tmp_path):
        # Given a catalog declaring a repository
        loaded = catalog.load_catalog(_write(tmp_path, CATALOG_YAML))

        # When it is mounted with a repository host, and without one
        with_host = self._mounted(loaded, repo_host=changes.LocalRepoHost(root=tmp_path))
        without_host = self._mounted(loaded)

        # Then only the first can read and propose, its one write is gated,
        # and only its instructions mention the repository
        gates = {tool.name: getattr(tool, "needs_approval", False) for tool in with_host.tools}
        assert gates["open_pull_request"] is True
        assert gates["preview_change"] is False
        assert "acme/infra-live" in str(with_host.instructions)
        assert "open_pull_request" not in {tool.name for tool in without_host.tools}
        assert "acme/infra-live" not in str(without_host.instructions)

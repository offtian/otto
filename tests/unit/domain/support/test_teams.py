import pathlib
import textwrap

import agents
import pytest
from agents.models.interface import Model

from otto.domain.support import agent as support_agent
from otto.domain.support import teams


PROFILE_YAML = textwrap.dedent(
    """
    teams:
      - name: platform
        description: Owns Coder and CI
        services: [Coder, jenkins]
        specialists: [coder]
        instructions: You own developer-platform issues.
      - name: sre
        services: [grafana, kubernetes]
        specialists: []
        instructions: You own infra issues.
    specialists:
      - name: coder
        description: Reads Coder workspaces.
        mcp: coder
        tools: [get_workspace, get_template]
        instructions: You are the Coder specialist.
    """
)


class _NeverCalledModel(Model):
    async def get_response(self, *args, **kwargs):
        raise AssertionError("the model must not be called at build time")

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


@agents.function_tool
async def _read_only_probe() -> str:
    """
    Probe tool without an approval gate.
    """
    return "ok"


@agents.function_tool(needs_approval=True)
async def _gated_probe() -> str:
    """
    Probe tool behind an approval gate.
    """
    return "never"


class TestLoadRegistry:
    def test_parses_teams_and_specialists_from_yaml(self, tmp_path):
        # Given a profiles file declaring two teams and one specialist
        path = tmp_path / "teams.yaml"
        path.write_text(PROFILE_YAML, encoding="utf-8")

        # When the registry is loaded
        registry = teams.load_registry(path)

        # Then the teams carry their lowercased services and specialist names
        assert [team.name for team in registry.teams] == ["platform", "sre"]
        assert registry.teams[0].services == ("coder", "jenkins")
        assert registry.teams[0].specialists == ("coder",)
        # Then the specialist carries its mcp name and tool allowlist
        assert registry.specialists["coder"].mcp == "coder"
        assert registry.specialists["coder"].tools == ("get_workspace", "get_template")

    def test_a_missing_file_yields_an_empty_registry(self):
        # Given no profiles file on disk
        path = pathlib.Path("does/not/exist.yaml")

        # When the registry is loaded
        registry = teams.load_registry(path)

        # Then team flows are simply absent
        assert registry == teams.TeamRegistry()


class TestResolve:
    def _registry(self):
        return teams.TeamRegistry(
            teams=(
                teams.TeamProfile(
                    name="platform",
                    description="",
                    instructions="",
                    services=("coder", "jenkins"),
                ),
                teams.TeamProfile(
                    name="sre", description="", instructions="", services=("grafana", "jenkins")
                ),
            )
        )

    def test_returns_the_team_owning_the_most_mentioned_services(self):
        # Given a request mentioning both of platform's services and one of sre's
        registry = self._registry()

        # When ownership resolves
        team = registry.resolve(services=("coder", "jenkins"))

        # Then the team with the most matches wins
        assert team is not None
        assert team.name == "platform"

    def test_matches_case_insensitively(self):
        # Given a mention that differs in case from the profile
        registry = self._registry()

        # When ownership resolves
        team = registry.resolve(services=("Coder",))

        # Then it still matches
        assert team is not None
        assert team.name == "platform"

    def test_returns_none_when_no_service_matches(self):
        # Given a request mentioning nothing any team owns
        registry = self._registry()

        # When ownership resolves
        # Then no team is assigned — the caller falls back to the general agent
        assert registry.resolve(services=("snowflake",)) is None

    def test_a_tie_goes_to_declaration_order(self):
        # Given a mention of the one service both teams own
        registry = self._registry()

        # When ownership resolves
        team = registry.resolve(services=("jenkins",))

        # Then the first declared team wins
        assert team is not None
        assert team.name == "platform"


class TestBuildSpecialistAgent:
    def test_refuses_an_approval_gated_tool(self):
        # Given a specialist profile handed a gated tool
        profile = teams.SpecialistProfile(name="coder", description="", instructions="x")

        # When the specialist builds
        # Then it fails loudly at startup — a gated tool inside a nested run
        # could never be approved
        with pytest.raises(ValueError, match="read-only"):
            teams.build_specialist_agent(
                profile=profile, model=_NeverCalledModel(), mcp_tools=[_gated_probe]
            )

    def test_builds_with_read_only_tools(self):
        # Given a specialist profile with an ungated tool
        profile = teams.SpecialistProfile(name="coder", description="", instructions="x")

        # When the specialist builds
        agent = teams.build_specialist_agent(
            profile=profile, model=_NeverCalledModel(), mcp_tools=[_read_only_probe]
        )

        # Then the tool mounts and the instructions carry the guardrails
        assert [tool.name for tool in agent.tools] == ["_read_only_probe"]
        assert "untrusted user content" in str(agent.instructions)


class TestBuildTeamAgents:
    def test_builds_one_owner_per_team_with_consult_tools(self, tmp_path):
        # Given the loaded example registry
        path = tmp_path / "teams.yaml"
        path.write_text(PROFILE_YAML, encoding="utf-8")
        registry = teams.load_registry(path)

        # When the team agents build without any specialist MCPs
        owners = teams.build_team_agents(
            registry=registry, model=_NeverCalledModel(), specialist_mcp_tools={}
        )

        # Then each team gets an owner whose tools include its consult tools
        # beside the shared escalation and knowledge tools
        assert set(owners) == {"platform", "sre"}
        platform_tools = [tool.name for tool in owners["platform"].tools]
        assert "consult_coder" in platform_tools
        assert "escalate_to_human" in platform_tools
        assert support_agent.search_knowledge.name in platform_tools

    def test_an_undeclared_specialist_fails_at_startup(self):
        # Given a team referencing a specialist the registry does not declare
        registry = teams.TeamRegistry(
            teams=(
                teams.TeamProfile(
                    name="platform", description="", instructions="", specialists=("ghost",)
                ),
            )
        )

        # When the team agents build
        # Then the profile typo surfaces at startup, never per request
        with pytest.raises(KeyError):
            teams.build_team_agents(
                registry=registry, model=_NeverCalledModel(), specialist_mcp_tools={}
            )

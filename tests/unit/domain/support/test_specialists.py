import agents
import pytest
from agents.models.interface import Model

from otto.domain.support import specialists


CODER = specialists.SpecialistProfile(
    name="coder",
    description="Reads Coder workspaces.",
    instructions="You are the Coder specialist.",
    mcp="coder",
    tools=("get_workspace",),
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


class TestBuildSpecialistAgent:
    def test_refuses_an_approval_gated_tool(self):
        # Given a specialist whose mounted tools include a gated one
        # When the specialist agent is built
        # Then it fails at startup — a nested approval could never be granted
        with pytest.raises(ValueError, match="read-only"):
            specialists.build_specialist_agent(
                profile=CODER, model=_NeverCalledModel(), mcp_tools=[_gated_probe]
            )

    def test_builds_with_read_only_tools(self):
        # Given a specialist whose mounted tools are all ungated
        # When the specialist agent is built
        agent = specialists.build_specialist_agent(
            profile=CODER, model=_NeverCalledModel(), mcp_tools=[_read_only_probe]
        )

        # Then it carries its own instructions and exactly those tools
        assert agent.name == "specialist-coder"
        assert "Coder specialist" in str(agent.instructions)
        assert [tool.name for tool in agent.tools] == ["_read_only_probe"]


class TestBuildConsultTools:
    def test_returns_one_consult_tool_per_specialist(self):
        # Given two specialists, one with mounted MCP tools and one without
        jenkins = specialists.SpecialistProfile(
            name="jenkins", description="", instructions="You are the Jenkins specialist."
        )

        # When the consult tools are built
        tools = specialists.build_consult_tools(
            profiles=[CODER, jenkins],
            model=_NeverCalledModel(),
            mcp_tools={"coder": [_read_only_probe]},
        )

        # Then each specialist is one tool, described by its profile or by name
        assert [(tool.name, tool.description) for tool in tools] == [
            ("consult_coder", "Reads Coder workspaces."),
            ("consult_jenkins", "Consult the jenkins specialist."),
        ]

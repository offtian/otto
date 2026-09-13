import json
import pathlib

import agents
from agents import tool_context

from otto.domain.support import agent as support_agent


class FakeTicketBackend:
    def __init__(self):
        self.escalations = []

    async def escalate(self, *, subject, summary, urgency, requester_id, origin_ref):
        self.escalations.append(
            {
                "subject": subject,
                "summary": summary,
                "urgency": urgency,
                "requester_id": requester_id,
                "origin_ref": origin_ref,
            }
        )
        return "triage#1"


class FakeMemoryStore:
    def __init__(self, passages=None, error=None):
        self.passages = passages or []
        self.error = error
        self.queries = []

    async def ingest(self, *, text):
        raise AssertionError("the search tool must never ingest")

    async def search(self, *, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.passages)


def _context(runbooks_dir: pathlib.Path, backend=None, memory=None):
    return tool_context.ToolContext(
        context=support_agent.SupportContext(
            requester_id="U_REQ",
            origin_ref="https://slack.com/archives/C1/p10",
            runbooks_dir=runbooks_dir,
            ticket_backend=backend or FakeTicketBackend(),
            memory=memory,
        ),
        tool_name="test",
        tool_call_id="call-1",
        tool_arguments="{}",
    )


class TestListRunbooks:
    async def test_returns_sorted_runbook_names(self, tmp_path):
        # Given a runbooks directory with two markdown files
        (tmp_path / "vpn.md").write_text("# VPN")
        (tmp_path / "password-reset.md").write_text("# Password")

        # When the list tool is invoked
        listed = await support_agent.list_runbooks.on_invoke_tool(_context(tmp_path), "{}")

        # Then both names come back, sorted, without extensions
        assert listed == "password-reset\nvpn"

    async def test_says_so_when_the_directory_is_empty(self, tmp_path):
        # Given an empty runbooks directory
        # When the list tool is invoked
        listed = await support_agent.list_runbooks.on_invoke_tool(_context(tmp_path), "{}")

        # Then the agent gets an explicit empty answer, not a crash
        assert listed == "No runbooks are available."


class TestReadRunbook:
    async def test_returns_the_runbook_content_by_name(self, tmp_path):
        # Given a runbook on disk
        (tmp_path / "vpn.md").write_text("# VPN steps")

        # When the read tool is invoked with the bare name
        content = await support_agent.read_runbook.on_invoke_tool(
            _context(tmp_path), json.dumps({"name": "vpn"})
        )

        # Then the markdown content is returned
        assert content == "# VPN steps"

    async def test_rejects_path_traversal_outside_the_runbooks_dir(self, tmp_path):
        # Given a secret file outside the runbooks directory
        runbooks = tmp_path / "runbooks"
        runbooks.mkdir()
        (tmp_path / "secret.md").write_text("secret")

        # When the read tool is asked to traverse out of the directory
        content = await support_agent.read_runbook.on_invoke_tool(
            _context(runbooks), json.dumps({"name": "../secret"})
        )

        # Then the traversal is refused
        assert "Unknown runbook" in content


class TestEscalateToHuman:
    async def test_routes_the_escalation_through_the_ticket_backend(self, tmp_path):
        # Given a fake ticket backend behind the context
        backend = FakeTicketBackend()

        # When the escalate tool is invoked
        outcome = await support_agent.escalate_to_human.on_invoke_tool(
            _context(tmp_path, backend),
            json.dumps({"subject": "VPN down", "summary": "cannot connect", "urgency": "high"}),
        )

        # Then the backend received the structured escalation with the origin
        assert backend.escalations == [
            {
                "subject": "VPN down",
                "summary": "cannot connect",
                "urgency": "high",
                "requester_id": "U_REQ",
                "origin_ref": "https://slack.com/archives/C1/p10",
            }
        ]
        assert "triage#1" in outcome


class TestSearchMemory:
    async def test_returns_the_stored_passages_best_first(self, tmp_path):
        # Given a memory store holding two relevant past cases
        memory = FakeMemoryStore(passages=["Fixed by bumping the base image.", "VPN case."])

        # When the memory tool is invoked
        answer = await support_agent.search_memory.on_invoke_tool(
            _context(tmp_path, memory=memory), json.dumps({"query": "template build fails"})
        )

        # Then the passages come back joined, and the store saw the query
        assert "bumping the base image" in answer
        assert memory.queries == ["template build fails"]

    async def test_says_so_when_no_memory_is_configured(self, tmp_path):
        # Given a context without a memory store
        # When the memory tool is invoked
        answer = await support_agent.search_memory.on_invoke_tool(
            _context(tmp_path), json.dumps({"query": "anything"})
        )

        # Then the agent gets an explicit answer, not a crash
        assert "No long-term memory" in answer

    async def test_fails_soft_when_the_store_errors(self, tmp_path):
        # Given a memory store that raises on search
        memory = FakeMemoryStore(error=RuntimeError("graph store down"))

        # When the memory tool is invoked
        answer = await support_agent.search_memory.on_invoke_tool(
            _context(tmp_path, memory=memory), json.dumps({"query": "anything"})
        )

        # Then the tool degrades to a proceed-without-it message — memory is
        # an assist, never a dependency
        assert "proceed without it" in answer


class TestBuildAgent:
    def test_mounts_stub_tools_when_no_mcp_is_configured(self):
        # Given no MCP servers
        # When the agent is built
        agent = support_agent.build_agent(model="stub-model")

        # Then both stub tools mount alongside the always-on tools
        tool_names = {tool.name for tool in agent.tools}
        assert tool_names == {
            "list_runbooks",
            "read_runbook",
            "escalate_to_human",
            "search_memory",
            "search_knowledge",
            "submit_access_request",
        }
        assert agent.mcp_servers == []

    def test_mounts_supplied_mcp_tools_instead_of_stubs(self):
        # Given pre-wrapped MCP tools for both capabilities
        confluence_search = _wrapped_tool("confluence_search")
        submit_request = _wrapped_tool("submit_access_request")

        # When the agent is built with them
        agent = support_agent.build_agent(
            model="stub-model",
            confluence_tools=[confluence_search],
            sailpoint_tools=[submit_request],
        )

        # Then the wrapped tools mount and no stub sneaks in beside them
        tool_names = {tool.name for tool in agent.tools}
        assert tool_names == {
            "list_runbooks",
            "read_runbook",
            "escalate_to_human",
            "search_memory",
            "confluence_search",
            "submit_access_request",
        }
        assert "search_knowledge" not in tool_names


def _wrapped_tool(name: str) -> agents.FunctionTool:
    async def invoke(tool_run_context, args):
        return ""

    return agents.FunctionTool(
        name=name, description="", params_json_schema={}, on_invoke_tool=invoke
    )

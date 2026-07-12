import json
import pathlib

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


def _context(runbooks_dir: pathlib.Path, backend=None):
    return tool_context.ToolContext(
        context=support_agent.SupportContext(
            requester_id="U_REQ",
            origin_ref="https://slack.com/archives/C1/p10",
            runbooks_dir=runbooks_dir,
            ticket_backend=backend or FakeTicketBackend(),
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
            "search_knowledge",
            "request_access",
        }
        assert agent.mcp_servers == []

import json
import pathlib
from datetime import UTC, datetime, timedelta

import agents
from agents import tool_context

from otto.domain.support import agent as support_agent
from otto.domain.support import entities, followups
from otto.domain.support import memory as support_memory


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
    def __init__(self, memories=None, error=None):
        self.memories = memories or []
        self.error = error
        self.queries = []

    async def remember(self, *, note):
        raise AssertionError("the search tool must never write to memory")

    async def search(self, *, query, limit):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.memories[:limit]


class FakeFollowUpStore:
    def __init__(self, error=None, pending=0):
        self.error = error
        self.pending = pending
        self.scheduled = []

    async def schedule(self, *, origin, requester_id, note, delay):
        if self.error is not None:
            raise self.error
        self.scheduled.append((origin, requester_id, note, delay))

    async def cancel_pending(self, *, origin):
        return self.pending


THREAD = entities.SlackThread(channel_id="C1", thread_ts="1.0")


def _context(runbooks_dir: pathlib.Path, backend=None, memory=None, follow_ups=None):
    return tool_context.ToolContext(
        context=support_agent.SupportContext(
            requester_id="U_REQ",
            origin_ref="https://slack.com/archives/C1/p10",
            runbooks_dir=runbooks_dir,
            ticket_backend=backend or FakeTicketBackend(),
            memory=memory,
            origin=THREAD if follow_ups is not None else None,
            follow_ups=follow_ups,
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
    async def test_returns_past_cases_best_first_with_how_often_each_held(self, tmp_path):
        # Given a memory store holding two relevant past cases
        memory = FakeMemoryStore(
            memories=[
                support_memory.Memory(
                    id=1,
                    note="Fixed by bumping the base image.",
                    times_seen=2,
                    last_seen=datetime(2026, 11, 20, tzinfo=UTC),
                ),
                support_memory.Memory(
                    id=2,
                    note="VPN case.",
                    times_seen=1,
                    last_seen=datetime(2026, 10, 1, tzinfo=UTC),
                ),
            ]
        )

        # When the memory tool is invoked
        answer = await support_agent.search_memory.on_invoke_tool(
            _context(tmp_path, memory=memory), json.dumps({"query": "template build fails"})
        )

        # Then both cases come back in the store's order, each headed by its
        # history, and the store saw the query
        assert answer.index("bumping the base image") < answer.index("VPN case.")
        assert "Past case (seen 2 times, last on 2026-11-20):" in answer
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
        memory = FakeMemoryStore(error=RuntimeError("database down"))

        # When the memory tool is invoked
        answer = await support_agent.search_memory.on_invoke_tool(
            _context(tmp_path, memory=memory), json.dumps({"query": "anything"})
        )

        # Then the tool degrades to a proceed-without-it message — memory is
        # an assist, never a dependency
        assert "proceed without it" in answer


class TestScheduleFollowUp:
    ARGUMENTS = json.dumps({"delay_minutes": 30, "note": "check Jenkins build 4521"})

    async def test_schedules_on_the_conversation_for_its_requester(self, tmp_path):
        # Given a conversation with a follow-up store
        store = FakeFollowUpStore()

        # When the agent schedules a follow-up in half an hour
        reply = await support_agent.schedule_follow_up.on_invoke_tool(
            _context(tmp_path, follow_ups=store), self.ARGUMENTS
        )

        # Then it is booked on this thread, for this requester, with the note
        assert store.scheduled == [
            (THREAD, "U_REQ", "check Jenkins build 4521", timedelta(minutes=30))
        ]
        assert "30 minutes" in reply

    async def test_says_so_when_follow_ups_are_unavailable(self, tmp_path):
        # Given an environment without a follow-up store
        # When the agent tries to schedule one
        reply = await support_agent.schedule_follow_up.on_invoke_tool(
            _context(tmp_path), self.ARGUMENTS
        )

        # Then it is told plainly, so it does not promise to check back
        assert "not available" in reply

    async def test_refuses_a_delay_beyond_thirty_days(self, tmp_path):
        # Given a conversation with a follow-up store
        store = FakeFollowUpStore()

        # When the agent asks to wait two months
        reply = await support_agent.schedule_follow_up.on_invoke_tool(
            _context(tmp_path, follow_ups=store),
            json.dumps({"delay_minutes": 60 * 24 * 60, "note": "check again"}),
        )

        # Then nothing is booked and the allowed range is named
        assert store.scheduled == []
        assert "between 1 and 43200" in reply

    async def test_tells_the_agent_to_escalate_once_the_conversation_is_at_its_limit(
        self, tmp_path
    ):
        # Given a conversation that already holds its allowed follow-ups
        store = FakeFollowUpStore(error=followups.FollowUpLimitReached("limit"))

        # When the agent schedules one more
        reply = await support_agent.schedule_follow_up.on_invoke_tool(
            _context(tmp_path, follow_ups=store), self.ARGUMENTS
        )

        # Then it is steered to a human instead of waking itself again
        assert "escalate" in reply


class TestCancelFollowUps:
    async def test_cancels_what_is_still_scheduled_on_the_conversation(self, tmp_path):
        # Given a conversation with two follow-ups still waiting
        store = FakeFollowUpStore(pending=2)

        # When the agent cancels them
        reply = await support_agent.cancel_follow_ups.on_invoke_tool(
            _context(tmp_path, follow_ups=store), "{}"
        )

        # Then it reports how many were cancelled
        assert reply == "Cancelled 2 scheduled follow-up(s)."


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
            "schedule_follow_up",
            "cancel_follow_ups",
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
            "schedule_follow_up",
            "cancel_follow_ups",
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

from unittest import mock

import agents
from mcp import types as mcp_types

from otto.vendors import mcp


WRITE_VERBS = ("create", "update", "delete", "add", "remove", "put", "post", "write", "move")


def _spec(**overrides):
    fields = {"name": "sailpoint", "url": "http://mcp.local", "token": "t", **overrides}
    return mcp.MCPSpec(**fields)


def _remote_tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(name=name, inputSchema={"type": "object", "properties": {}})


class TestConfluenceReadOnly:
    def test_allowlist_contains_no_write_tools(self):
        # Given the Confluence tool allowlist (A1 read-only enforcement)
        allowlist = mcp.CONFLUENCE_READ_TOOLS

        # When each tool name is checked against write-style verbs
        offenders = [name for name in allowlist if any(verb in name for verb in WRITE_VERBS)]

        # Then no write-capable tool can ever mount
        assert offenders == []


class TestBuildMount:
    def test_installs_the_static_allowlist_filter_when_allowed_tools_is_set(self):
        # Given a spec restricted to the Confluence read tools
        spec = _spec(name="confluence", allowed_tools=mcp.CONFLUENCE_READ_TOOLS)

        # When the mount is built
        mount = mcp.build_mount(spec=spec)

        # Then the server statically allows exactly those tool names
        assert mount.server.tool_filter == {"allowed_tool_names": list(mcp.CONFLUENCE_READ_TOOLS)}

    def test_mounts_every_server_tool_when_allowed_tools_is_none(self):
        # Given a spec with no allowlist (SailPoint mounts all, gated by approval)
        spec = _spec(allowed_tools=None)

        # When the mount is built
        mount = mcp.build_mount(spec=spec)

        # Then no tool filter is installed
        assert mount.server.tool_filter is None


class TestFunctionTools:
    async def test_wraps_remote_tools_and_gates_all_but_the_ungated_allowlist(self):
        # Given a mount whose server offers one cleared read and one write
        mount = mcp.build_mount(spec=_spec(ungated_tools=frozenset({"a_read"})))
        remote_tools = [_remote_tool("a_read"), _remote_tool("a_write")]

        # When the tools are wrapped as FunctionTools
        with mock.patch.object(
            mount.server, "list_tools", mock.AsyncMock(return_value=remote_tools)
        ):
            wrapped = await mount.function_tools()

        # Then every tool is a FunctionTool and only the unlisted one is gated
        # — default-deny, so a new server-side tool can never run ungated
        assert all(isinstance(tool, agents.FunctionTool) for tool in wrapped)
        assert {tool.name: tool.needs_approval for tool in wrapped} == {
            "a_read": False,
            "a_write": True,
        }

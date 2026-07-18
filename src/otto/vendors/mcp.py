"""
MCP integrations (Streamable HTTP transport): declarative specs, server
construction, and the wrapping of remote tools into ``FunctionTool``s.

Each integration is an ``MCPSpec`` built into an ``MCPServerMount``. The
agent never mounts the server object itself — ``MCPServerMount.function_tools``
returns the remote tools as the same ``FunctionTool``s a local
``@agents.function_tool`` produces, with ``needs_approval`` stamped per tool.
Default-deny: any mounted tool not on ``spec.ungated_tools`` pauses for
human approval, so a new server-side tool can never run ungated.

Each integration is optional: when its URL is unset, ``config.py`` skips
the mount and the agent falls back to a local stub tool, so the full loop
still runs in dev with zero external dependencies.

Servers must be ``connect()``-ed before the first wrap and ``cleanup()``-ed
on shutdown — the FastAPI lifespan in ``interfaces/app.py`` owns that.
"""

import agents
import attrs
from agents import mcp as agents_mcp


# Read-only enforcement (A1): the dev mcp-atlassian image ships write tools,
# so only this allowlist may mount — an unknown or write tool simply never
# reaches the agent. Re-verify against the real server's tool list at
# Phase 1 (step 1.4); a too-narrow list breaks search loudly, not silently.
CONFLUENCE_READ_TOOLS = (
    "confluence_search",
    "confluence_get_page",
    "confluence_get_page_children",
    "confluence_get_comments",
    "confluence_get_labels",
    "confluence_search_user",
)


@attrs.frozen
class MCPSpec:
    """
    Declarative description of one MCP integration: where the server is,
    which of its tools may mount, and which mounted tools run without
    human approval.
    """

    name: str
    url: str
    token: str
    # None = mount every tool the server offers.
    allowed_tools: tuple[str, ...] | None = None
    # Default-deny: any mounted tool NOT listed here pauses for approval.
    ungated_tools: frozenset[str] = frozenset()


@attrs.frozen
class MCPServerMount:
    """
    A built MCP server paired with the spec that produced it.
    """

    spec: MCPSpec
    server: agents_mcp.MCPServerStreamableHttp

    async def function_tools(self) -> list[agents.FunctionTool]:
        """
        Return the server's tools wrapped as ``FunctionTool``s, each with
        ``needs_approval`` set from the spec (default-deny). The server's
        static tool filter applies before wrapping, so only
        ``spec.allowed_tools`` can ever reach the agent.

        :raises agents.exceptions.UserError: if the server is not connected
            or the tool list cannot be fetched.
        """
        wrapped = []
        for mcp_tool in await self.server.list_tools():
            tool = agents_mcp.MCPUtil.to_function_tool(
                mcp_tool, self.server, convert_schemas_to_strict=False
            )
            tool.needs_approval = mcp_tool.name not in self.spec.ungated_tools
            wrapped.append(tool)
        return wrapped


def build_mount(*, spec: MCPSpec) -> MCPServerMount:
    """
    Return the mount for ``spec``: a Streamable HTTP server with the spec's
    static tool filter installed, paired with the spec for wrapping.
    """
    return MCPServerMount(
        spec=spec,
        server=agents_mcp.MCPServerStreamableHttp(
            params={"url": spec.url, "headers": _auth_headers(spec.token)},
            name=spec.name,
            cache_tools_list=True,
            tool_filter=(
                agents_mcp.create_static_tool_filter(allowed_tool_names=list(spec.allowed_tools))
                if spec.allowed_tools is not None
                else None
            ),
        ),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}

"""
MCP server construction (Streamable HTTP transport).

Each integration is optional: when its URL is unset, ``config.py`` skips
the server and the agent falls back to a local stub tool, so the full loop
still runs in dev with zero external dependencies.

Servers must be ``connect()``-ed before the first run and ``cleanup()``-ed
on shutdown — the FastAPI lifespan in ``interfaces/api.py`` owns that.
"""

from agents import mcp as agents_mcp
from agents.mcp import server as agents_mcp_server


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


def build_confluence(*, url: str, token: str) -> agents_mcp.MCPServerStreamableHttp:
    """
    Return the knowledge-base MCP server, filtered to read-only tools (A1).
    """
    return agents_mcp.MCPServerStreamableHttp(
        params={"url": url, "headers": _auth_headers(token)},
        name="confluence",
        cache_tools_list=True,
        tool_filter=agents_mcp.create_static_tool_filter(
            allowed_tool_names=list(CONFLUENCE_READ_TOOLS),
        ),
    )


def build_sailpoint(
    *,
    url: str,
    token: str,
    require_approval: agents_mcp_server.RequireApprovalSetting,
) -> agents_mcp.MCPServerStreamableHttp:
    """
    Return the identity/access MCP server, gated by the given per-tool
    approval policy (2.6). ``require_approval`` is required — not defaulted —
    so a caller can never accidentally mount SailPoint ungated: config passes
    the default-deny sensitivity gate, which pauses every tool it has not
    explicitly cleared.
    """
    return agents_mcp.MCPServerStreamableHttp(
        params={"url": url, "headers": _auth_headers(token)},
        name="sailpoint",
        cache_tools_list=True,
        require_approval=require_approval,
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}

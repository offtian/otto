"""
Mock SailPoint MCP (dev only) — a deterministic stand-in for the firm's
SailPoint MCP server, so the access-automation loop runs end-to-end with no
external dependency (T8 surface, 2.4). SailPoint has no public dev image.

Run it directly:

    uv run python dev/sailpoint_mock.py     # serves streamable-HTTP on :9100/mcp

or via `docker compose --profile sailpoint up`. Point SAILPOINT_MCP_URL at it
(http://localhost:9100/mcp on the host).

Tool *sensitivity* is enforced app-side by the 2.6 policy, never here: the reads
(search/list) flow freely, the writes (submit/approve) pause for HITL. This
service only behaves like SailPoint; it never decides what needs approval.

ponytail: in-memory, deterministic, non-persistent on purpose — a mock only has
to be predictable for evals and the gate-coverage test, not durable.
"""

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("sailpoint-mock", host="0.0.0.0", port=9100)

# Deterministic fixtures so evals and the gate-coverage test are stable.
_CATALOG: dict[str, list[str]] = {
    "snowflake": ["reporting-read", "reporting-write", "admin"],
    "github": ["read", "write", "org-admin"],
    "salesforce": ["viewer", "editor"],
}
_CURRENT_ACCESS: dict[str, list[str]] = {"U_EVAL": ["snowflake:reporting-read"]}
_requests: dict[str, dict[str, str]] = {}


@mcp.tool()
def search_entitlements(system: str, query: str) -> list[str]:
    """Search a system's entitlement catalogue (read-only)."""
    entitlements = _CATALOG.get(system.lower(), [])
    hits = [e for e in entitlements if query.lower() in e.lower()]
    return hits or entitlements


@mcp.tool()
def list_identity_access(identity: str) -> list[str]:
    """List the entitlements an identity currently holds (read-only)."""
    return _CURRENT_ACCESS.get(identity, [])


@mcp.tool()
def list_access_requests() -> list[dict[str, str]]:
    """List the access requests submitted so far this session (read-only)."""
    return list(_requests.values())


@mcp.tool()
def submit_access_request(system: str, entitlement: str, justification: str) -> dict[str, str]:
    """Submit an access request. The app gates this behind human approval."""
    request_id = f"REQ-{len(_requests) + 1}"
    _requests[request_id] = {
        "request_id": request_id,
        "system": system,
        "entitlement": entitlement,
        "justification": justification,
        "status": "pending",
    }
    return _requests[request_id]


@mcp.tool()
def approve_access_request(request_id: str) -> dict[str, str]:
    """Approve a submitted access request. The app gates this behind approval."""
    request = _requests.get(request_id)
    if request is None:
        return {"request_id": request_id, "status": "not_found"}
    request["status"] = "approved"
    return request


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

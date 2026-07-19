"""
Gate-coverage (2.4): connect to the running mock SailPoint MCP, wrap the tools
it *actually* mounts, and assert ``needs_approval`` — the operative flag on
every wrapped FunctionTool — pauses every write and clears every read. The
project's worst-failure guard — a write slipping past approval — checked
against the real tool list, not assumptions.

Live: needs the mock (`docker compose --profile sailpoint up`, or
`uv run python dev/sailpoint_mock.py`). Guarded by RUN_INTEGRATION; run via
`just test-integration`.
"""

import os
import socket
import urllib.parse

import pytest

from otto.domain.support import policy
from otto.vendors import mcp as mcp_vendor


SAILPOINT_URL = os.environ.get("SAILPOINT_MCP_URL", "http://localhost:9100/mcp")


def _mock_reachable() -> bool:
    """
    Test whether the mock SailPoint MCP is listening — the fixture otherwise
    dies mid-connect with an opaque ConnectError instead of a clean skip.
    """
    parsed = urllib.parse.urlparse(SAILPOINT_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="needs the mock SailPoint MCP — run via `just test-integration`",
    ),
    pytest.mark.skipif(
        bool(os.environ.get("RUN_INTEGRATION")) and not _mock_reachable(),
        reason="mock SailPoint MCP not reachable — `docker compose --profile sailpoint up -d`",
    ),
    # The MCP streamable-HTTP client emits a DeprecationWarning from inside the
    # SDK transport on connect; it is not ours to fix, so don't let the suite's
    # filterwarnings=error turn SDK noise into a gate-coverage failure.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

# The reads cleared by the T8 surface (2026-07-12). Everything else the server
# mounts — the two writes and any future tool — must be gated by default-deny.
EXPECTED_READS = {"search_entitlements", "list_identity_access", "list_access_requests"}


@pytest.fixture
async def approval_by_tool_name():
    # Build the sailpoint mount exactly as config does (2.6 policy wired in)
    # and wrap the tools it really exposes.
    mount = mcp_vendor.build_mount(
        spec=mcp_vendor.MCPSpec(
            name="sailpoint",
            url=SAILPOINT_URL,
            token="",
            ungated_tools=policy.SENSITIVITY_POLICY.ungated,
        )
    )
    await mount.server.connect()
    try:
        tools = await mount.function_tools()
    finally:
        await mount.server.cleanup()
    return {tool.name: tool.needs_approval for tool in tools}


class TestSailpointGateCoverage:
    async def test_no_mounted_tool_is_silently_ungated(self, approval_by_tool_name):
        # Given every tool the mock actually mounts, wrapped as the agent sees it
        assert approval_by_tool_name, "mock exposed no tools — is it running?"

        # When each tool that is not an explicitly cleared read is checked
        # Then it is gated — default-deny, so a write can never slip past approval
        for name, needs_approval in approval_by_tool_name.items():
            if name not in EXPECTED_READS:
                assert needs_approval is True, name

    async def test_cleared_reads_run_without_approval(self, approval_by_tool_name):
        # Given the mounted read tools
        reads = set(approval_by_tool_name) & EXPECTED_READS
        assert reads, "expected the mock to mount the cleared read tools"

        # When each is checked
        # Then it runs without approval — reads flow freely (the 2.4 read un-gate)
        assert not any(approval_by_tool_name[name] for name in reads)

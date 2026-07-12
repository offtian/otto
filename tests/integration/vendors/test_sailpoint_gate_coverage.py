"""
Gate-coverage (2.4): connect to the running mock SailPoint MCP, list the tools
it *actually* mounts, and assert the 2.6 sensitivity policy pauses every write
and clears every read. The project's worst-failure guard — a write slipping
past approval — checked against the real tool list, not assumptions.

Live: needs the mock (`docker compose --profile sailpoint up`, or
`uv run python dev/sailpoint_mock.py`). Guarded by RUN_INTEGRATION; run via
`just test-integration`.
"""

import os

import pytest

from otto.domain.support import policy
from otto.vendors import mcp as mcp_vendor


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="needs the mock SailPoint MCP — run via `just test-integration`",
    ),
    # The MCP streamable-HTTP client emits a DeprecationWarning from inside the
    # SDK transport on connect; it is not ours to fix, so don't let the suite's
    # filterwarnings=error turn SDK noise into a gate-coverage failure.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

SAILPOINT_URL = os.environ.get("SAILPOINT_MCP_URL", "http://localhost:9100/mcp")

# The reads cleared by the T8 surface (2026-07-12). Everything else the server
# mounts — the two writes and any future tool — must be gated by default-deny.
EXPECTED_READS = {"search_entitlements", "list_identity_access", "list_access_requests"}


@pytest.fixture
async def mounted_tool_names():
    # Build the sailpoint server exactly as config does (2.6 gate wired in) and
    # ask it for the tools it really exposes.
    server = mcp_vendor.build_sailpoint(
        url=SAILPOINT_URL, token="", require_approval=policy.SENSITIVITY_GATE
    )
    await server.connect()
    try:
        tools = await server.list_tools()
    finally:
        await server.cleanup()
    return {tool.name for tool in tools}


class TestSailpointGateCoverage:
    async def test_no_mounted_tool_is_silently_ungated(self, mounted_tool_names):
        # Given every tool the mock actually mounts
        assert mounted_tool_names, "mock exposed no tools — is it running?"

        # When each tool that is not an explicitly cleared read is checked
        # Then it is gated — default-deny, so a write can never slip past approval
        for name in mounted_tool_names:
            if name not in EXPECTED_READS:
                assert policy.SENSITIVITY_POLICY.is_sensitive(name) is True, name

    async def test_cleared_reads_run_without_approval(self, mounted_tool_names):
        # Given the mounted read tools
        reads = mounted_tool_names & EXPECTED_READS
        assert reads, "expected the mock to mount the cleared read tools"

        # When each is checked
        # Then it runs without approval — reads flow freely (the 2.4 read un-gate)
        assert not any(policy.SENSITIVITY_POLICY.is_sensitive(name) for name in reads)

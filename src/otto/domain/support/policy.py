"""
Per-tool sensitivity policy (2.6): which tool calls must pause for a human
approval before they run.

**Default-deny.** ``is_sensitive`` returns True for every tool the policy does
not explicitly clear, so a newly-mounted tool — a new SailPoint action, a Jira
transition (D10) — is gated until someone deliberately adds it to
``UNGATED_TOOLS`` *with a sign-off reference*. Un-gating is therefore always a
reviewed, marked change: the policy test fails CI on an ungated entry that
carries no marker ("policy changes may only widen the gated set").

The policy is enforced at wrap time: ``config.py`` feeds ``SENSITIVITY_POLICY``
into the SailPoint ``MCPSpec``, and ``vendors.mcp.MCPServerMount.function_tools``
stamps ``needs_approval`` onto every wrapped tool from it — never the SDK's
dict form, which defaults *unlisted* tools to ungated, the silent un-gate this
policy exists to forbid.
"""

import attrs


# Tools cleared to run without human approval. The value is the sign-off
# reference that authorized un-gating each one — never leave it empty (the
# policy test enforces this). The SailPoint writes (submit/approve access
# request, whatever their native names) and any Jira transition are absent on
# purpose: default-deny gates them without needing to be enumerated here.
#
# Names must match the mounted SailPoint tools exactly (T8/2.4). A mismatch
# fails *safe*: an unrecognized name is gated, never silently ungated.
UNGATED_TOOLS: dict[str, str] = {
    # SailPoint reads (T8 surface, 2026-07-12) — discovery only, no state change.
    "search_entitlements": "T8/2.6 2026-07-12: read-only entitlement search",
    "list_identity_access": "T8/2.6 2026-07-12: read-only current-access list",
    "list_access_requests": "T8/2.6 2026-07-12: read-only request history",
    "get_access_request_status": "T8/2.6 2026-07-12: read-only request status",
}


@attrs.frozen
class SensitivityPolicy:
    """
    Maps a tool name to whether calling it needs human approval.
    """

    ungated: frozenset[str]

    def is_sensitive(self, tool_name: str) -> bool:
        """
        Return whether a tool must pause for approval. Any tool not on the
        ungated allowlist is sensitive (default-deny) — the safe direction
        for a write gate.
        """
        return tool_name not in self.ungated


SENSITIVITY_POLICY = SensitivityPolicy(ungated=frozenset(UNGATED_TOOLS))

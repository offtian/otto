"""
Per-tool sensitivity policy (2.6): which tool calls must pause for a human
approval before they run, and the Agents-SDK approval gate built from it.

**Default-deny.** ``is_sensitive`` returns True for every tool the policy does
not explicitly clear, so a newly-mounted tool — a new SailPoint action, a Jira
transition (D10) — is gated until someone deliberately adds it to
``UNGATED_TOOLS`` *with a sign-off reference*. Un-gating is therefore always a
reviewed, marked change: the policy test fails CI on an ungated entry that
carries no marker ("policy changes may only widen the gated set").

``approval_gate`` adapts the policy to the SDK per-tool approval callback so the
composition root only has to wire the resulting callable. We build a callable,
never the SDK's dict form, which defaults *unlisted* tools to ungated — the
silent un-gate this policy exists to forbid.
"""

from collections.abc import Callable
from typing import Any

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


def approval_gate(policy: SensitivityPolicy) -> Callable[[Any, Any, Any], bool]:
    """
    Return an Agents-SDK per-tool approval callback for ``policy``: the SDK
    invokes it as ``(run_context, agent, tool)`` for each mounted tool, and it
    pauses (returns True) for anything the policy has not explicitly cleared.
    """

    def _requires_approval(run_context: Any, agent: Any, tool: Any) -> bool:
        return policy.is_sensitive(tool.name)

    return _requires_approval


SENSITIVITY_POLICY = SensitivityPolicy(ungated=frozenset(UNGATED_TOOLS))
SENSITIVITY_GATE = approval_gate(SENSITIVITY_POLICY)

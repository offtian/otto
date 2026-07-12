"""
Escalation contract — how Otto hands a request over to humans.

Implementations satisfy this protocol *structurally* (vendors sit below
domain and never import it): today a Slack triage channel
(``vendors.slack.SlackTriageBackend``); ServiceNow or Jira adapters plug
into the same seam later without touching the agent.
"""

from typing import Protocol


class TicketBackend(Protocol):
    async def escalate(
        self,
        *,
        subject: str,
        summary: str,
        urgency: str,
        requester_id: str,
        origin_ref: str,
    ) -> str:
        """
        Create or route the escalation and return a human-readable
        reference (ticket number, triage-thread link, ...).

        ``origin_ref`` is the already-rendered address of the originating
        conversation (Slack thread link, issue key) — backends post it,
        they never interpret it.
        """
        ...

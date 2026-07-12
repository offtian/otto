"""
Slack Web API wrappers (slack-sdk) — no business logic.

``SlackGateway`` is the thin messaging surface the application layer talks
to; ``SlackTriageBackend`` structurally implements the domain
``TicketBackend`` protocol by escalating into a human triage channel.
"""

import attrs
from slack_sdk.web.async_client import AsyncWebClient


APPROVE_ACTION_ID = "otto_approval_approve"
DENY_ACTION_ID = "otto_approval_deny"


@attrs.frozen
class SlackGateway:
    """
    Thin async wrapper around the Slack Web API.
    """

    client: AsyncWebClient

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        """
        Post a message and return its ``ts`` (message id).
        """
        response = await self.client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
        return str(response["ts"])

    async def update_message(self, *, channel: str, ts: str, text: str) -> None:
        """
        Replace a message's content (used to close out approval cards).
        """
        await self.client.chat_update(channel=channel, ts=ts, text=text, blocks=[])

    async def fetch_thread(
        self,
        *,
        channel: str,
        thread_ts: str,
        limit: int,
    ) -> list[tuple[str, str]]:
        """
        Return up to ``limit`` ``(author_id, text)`` pairs from a thread,
        oldest first — the D2 conversation-reconstruction source.
        """
        response = await self.client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )
        messages = response.get("messages") or []
        return [
            (message.get("user") or message.get("bot_id") or "unknown", message.get("text", ""))
            for message in messages
        ]

    async def post_approval_card(
        self,
        *,
        channel: str,
        approval_id: str,
        requester_id: str,
        tool_name: str,
        tool_arguments: str,
    ) -> str:
        """
        Post a Block Kit approve/deny card for a paused agent run and
        return its ``ts``.
        """
        header = (
            f":lock: *Approval needed* — <@{requester_id}>'s request wants to run "
            f"a sensitive action.\n*Tool:* `{tool_name}`\n*Arguments:*\n"
            f"```{tool_arguments}```"
        )
        blocks: list[dict[str, object]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": APPROVE_ACTION_ID,
                        "value": approval_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": DENY_ACTION_ID,
                        "value": approval_id,
                    },
                ],
            },
        ]
        response = await self.client.chat_postMessage(
            channel=channel,
            text=f"Approval needed: {tool_name}",
            blocks=blocks,
        )
        return str(response["ts"])


@attrs.frozen
class SlackTriageBackend:
    """
    Escalation backend that routes to a human triage channel.

    Structurally satisfies ``domain.support.escalation.TicketBackend``;
    swap for a ServiceNow/Jira adapter without touching the agent.
    """

    gateway: SlackGateway
    triage_channel: str

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
        Post a structured escalation card to the triage channel and return
        a triage reference.
        """
        text = (
            f":rotating_light: *Escalation* ({urgency}) — {subject}\n"
            f"*From:* <@{requester_id}>\n"
            f"*Summary:* {summary}\n"
            f"*Origin:* {origin_ref}"
        )
        ts = await self.gateway.post_message(channel=self.triage_channel, text=text)
        return f"triage#{ts}"

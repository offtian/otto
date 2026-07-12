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
FEEDBACK_YES_ACTION_ID = "otto_feedback_yes"
FEEDBACK_NO_ACTION_ID = "otto_feedback_no"
RESOLVE_ACTION_ID = "otto_escalation_resolve"


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

    async def post_answer(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None,
        feedback_value: str,
    ) -> str:
        """
        Post a knowledge answer with a "Did this help?" resolution vote and
        return its ``ts`` (T2 — the Slack-side D6 signal). ``feedback_value``
        is echoed back on click to locate the originating thread.
        """
        blocks: list[dict[str, object]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Did this help?"}]},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Yes, thanks"},
                        "style": "primary",
                        "action_id": FEEDBACK_YES_ACTION_ID,
                        "value": feedback_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No, still stuck"},
                        "action_id": FEEDBACK_NO_ACTION_ID,
                        "value": feedback_value,
                    },
                ],
            },
        ]
        response = await self.client.chat_postMessage(
            channel=channel, text=text, thread_ts=thread_ts, blocks=blocks
        )
        return str(response["ts"])

    async def update_message(self, *, channel: str, ts: str, text: str) -> None:
        """
        Replace a message's content (used to close out approval cards).
        """
        await self.client.chat_update(channel=channel, ts=ts, text=text, blocks=[])

    async def post_escalation(self, *, channel: str, text: str, resolve_value: str) -> str:
        """
        Post an escalation card carrying a "Mark resolved" button and return
        its ``ts``. ``resolve_value`` (the origin ref) is echoed back on click
        so a support agent's resolution can be attributed to the right request
        (the D6 support-agent-marks-resolved signal).
        """
        blocks: list[dict[str, object]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Mark resolved"},
                        "action_id": RESOLVE_ACTION_ID,
                        "value": resolve_value,
                    },
                ],
            },
        ]
        response = await self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return str(response["ts"])

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
        requester: str,
        tool_name: str,
        summary: str,
    ) -> str:
        """
        Post a Block Kit approve/deny card for a paused agent run and return
        its ``ts``. The decision is whether to let Otto *automate and file*
        the request on the requester's behalf — not whether to grant the
        access, which the target system's own approval chain still decides.
        ``requester`` and ``summary`` are pre-rendered mrkdwn (identity
        resolution and argument formatting are the caller's job).
        """
        header = (
            f":lock: *Approve automating this?* Otto is ready to submit a "
            f"sensitive request on behalf of {requester} and needs a human OK "
            f"to file it. This authorizes the submission, not the access "
            f"itself — the target system runs its own approval.\n\n"
            f"{summary}\n\n_Tool:_ `{tool_name}`"
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
            text=f"Approve automating {tool_name} on the requester's behalf?",
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
        ts = await self.gateway.post_escalation(
            channel=self.triage_channel, text=text, resolve_value=origin_ref
        )
        return f"triage#{ts}"

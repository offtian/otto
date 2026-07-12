"""
Pydantic schemas for inbound transport payloads (Slack events, Slack
interactions, Jira webhooks).

These validate the untrusted JSON at the interface boundary and carry the
mapping to channel-neutral domain shapes (``SupportRequest``) and the small
interaction DTOs the routers dispatch on. The application layer never sees
this JSON — it receives only the mapped domain/DTO objects, keeping it
transport-agnostic. Only the fields Otto uses are modelled; extras are
ignored, and every field defaults so a partial payload degrades to "ignore"
rather than raising.
"""

from typing import Self

import pydantic

from otto.application import dispatch
from otto.domain.support import entities
from otto.vendors import slack as slack_vendor


class _Payload(pydantic.BaseModel):
    """
    Base for transport payloads: ignore unmodelled fields, and parse
    leniently — a malformed body maps to None (ack and ignore) rather than
    raising a 500 that makes the sender retry forever.
    """

    model_config = pydantic.ConfigDict(extra="ignore")

    @classmethod
    def parse(cls, payload: object) -> Self | None:
        """
        Validate ``payload`` into this schema, or return None if it does not
        fit (an unexpected or malformed body).
        """
        try:
            return cls.model_validate(payload)
        except pydantic.ValidationError:
            return None


# --- Slack Events API -------------------------------------------------------


class _AssistantThread(_Payload):
    channel_id: str = ""
    thread_ts: str = ""


class _SlackEvent(_Payload):
    type: str = ""
    subtype: str | None = None
    bot_id: str | None = None
    user: str = ""
    text: str = ""
    channel: str = ""
    channel_type: str | None = None
    ts: str = ""
    thread_ts: str | None = None
    # Present only on assistant_thread_started (agent-mode entry, 3.5).
    assistant_thread: _AssistantThread | None = None


class SlackEventEnvelope(_Payload):
    type: str = ""
    event_id: str = ""
    challenge: str = ""
    event: _SlackEvent | None = None

    def to_support_request(self) -> entities.SupportRequest | None:
        """
        Map a Slack event to a ``SupportRequest``, or None for events Otto
        must ignore (bots and message edits — the loop guard — and event
        types it does not handle).
        """
        event = self.event
        if event is None or event.bot_id or event.subtype:
            return None
        is_mention = event.type == "app_mention"
        is_dm = event.type == "message" and event.channel_type == "im"
        if not (is_mention or is_dm):
            return None
        return entities.SupportRequest(
            id=self.event_id,
            user_id=event.user,
            text=event.text,
            origin=entities.SlackThread(
                channel_id=event.channel,
                thread_ts=event.thread_ts or event.ts,
            ),
        )

    def to_assistant_greeting(self) -> dispatch.AssistantGreeting | None:
        """
        Map an ``assistant_thread_started`` event to a greeting instruction
        (agent-mode entry, 3.5), or None for any other event.
        """
        event = self.event
        if event is None or event.type != "assistant_thread_started":
            return None
        thread = event.assistant_thread
        if thread is None or not thread.channel_id:
            return None
        return dispatch.AssistantGreeting(channel=thread.channel_id, thread_ts=thread.thread_ts)


# --- Slack interactivity (Block Kit actions) --------------------------------


class _SlackAction(_Payload):
    action_id: str = ""
    value: str = ""


class _SlackRef(_Payload):
    id: str = ""


class _SlackMessage(_Payload):
    ts: str = ""


class SlackInteraction(_Payload):
    type: str = ""
    user: _SlackRef | None = None
    channel: _SlackRef | None = None
    message: _SlackMessage | None = None
    actions: list[_SlackAction] = pydantic.Field(default_factory=list)

    def _action_and_user(self) -> tuple[_SlackAction, _SlackRef] | None:
        """
        Return the clicked action and the (non-None) clicking user, or None
        when this is not a resolvable block action.
        """
        if self.type != "block_actions" or self.user is None or not self.actions:
            return None
        return self.actions[0], self.user

    def to_approval_decision(self) -> dispatch.ApprovalDecision | None:
        pair = self._action_and_user()
        if pair is None:
            return None
        action, user = pair
        if action.action_id not in (slack_vendor.APPROVE_ACTION_ID, slack_vendor.DENY_ACTION_ID):
            return None
        return dispatch.ApprovalDecision(
            approval_id=action.value,
            resolver_id=user.id,
            approved=action.action_id == slack_vendor.APPROVE_ACTION_ID,
        )

    def to_feedback_vote(self) -> dispatch.FeedbackVote | None:
        pair = self._action_and_user()
        if pair is None:
            return None
        action, user = pair
        if action.action_id not in (
            slack_vendor.FEEDBACK_YES_ACTION_ID,
            slack_vendor.FEEDBACK_NO_ACTION_ID,
        ):
            return None
        channel, _, thread_ts = action.value.partition(":")
        return dispatch.FeedbackVote(
            helpful=action.action_id == slack_vendor.FEEDBACK_YES_ACTION_ID,
            origin=entities.SlackThread(channel_id=channel, thread_ts=thread_ts),
            voter_id=user.id,
        )

    def to_resolve_click(self) -> dispatch.ResolveClick | None:
        pair = self._action_and_user()
        if pair is None:
            return None
        action, user = pair
        if action.action_id != slack_vendor.RESOLVE_ACTION_ID:
            return None
        return dispatch.ResolveClick(
            origin_ref=action.value,
            resolver_id=user.id,
            card_channel=self.channel.id if self.channel else "",
            card_ts=self.message.ts if self.message else "",
        )


# --- Jira webhooks ----------------------------------------------------------


class _JiraUser(_Payload):
    accountId: str = ""


class _JiraStatusCategory(_Payload):
    key: str = ""


class _JiraStatus(_Payload):
    statusCategory: _JiraStatusCategory | None = None


class _JiraFields(_Payload):
    # ponytail: v2 string bodies assumed — description arrives as a doc node
    # on ADF (v3) sites; add a text-walker if the live webhook (0.11) needs it.
    summary: str = ""
    description: str = ""
    reporter: _JiraUser | None = None
    status: _JiraStatus | None = None


class _JiraIssue(_Payload):
    id: str = ""
    key: str = ""
    fields: _JiraFields | None = None


class _JiraComment(_Payload):
    id: str = ""
    author: _JiraUser | None = None
    body: str = ""


class _JiraChangelogItem(_Payload):
    field: str = ""


class _JiraChangelog(_Payload):
    id: str = ""
    items: list[_JiraChangelogItem] = pydantic.Field(default_factory=list)


class JiraWebhook(_Payload):
    webhookEvent: str = ""
    issue: _JiraIssue | None = None
    comment: _JiraComment | None = None
    changelog: _JiraChangelog | None = None

    def to_support_request(self) -> entities.SupportRequest | None:
        """
        Map a created issue or comment to a ``SupportRequest``, or None for
        any other Jira event.
        """
        issue = self.issue
        if issue is None:
            return None
        origin = entities.TicketRef(issue_key=issue.key)
        fields = issue.fields or _JiraFields()
        if self.webhookEvent == "jira:issue_created":
            text = "\n\n".join(part for part in (fields.summary, fields.description) if part)
            return entities.SupportRequest(
                id=f"jira:issue:{issue.id}",
                user_id=fields.reporter.accountId if fields.reporter else "",
                text=text,
                origin=origin,
            )
        if self.webhookEvent == "comment_created":
            comment = self.comment
            if comment is None:
                return None
            return entities.SupportRequest(
                id=f"jira:comment:{comment.id}",
                user_id=comment.author.accountId if comment.author else "",
                text=comment.body,
                origin=origin,
            )
        return None

    def resolution_key(self) -> str | None:
        """
        Return the issue key when this is a transition into a done-category
        status — a native D6 resolution signal — else None. Fires only when
        the changelog carries a status change AND the resulting status sits
        in Jira's fixed ``done`` category.
        """
        if self.webhookEvent != "jira:issue_updated" or self.changelog is None:
            return None
        if not any(item.field == "status" for item in self.changelog.items):
            return None
        issue = self.issue
        if issue is None or issue.fields is None or issue.fields.status is None:
            return None
        category = issue.fields.status.statusCategory
        if category is None or category.key != "done":
            return None
        return issue.key

    def resolution_dedup_id(self) -> str:
        """
        Return a stable id for deduping resolution redeliveries (the
        changelog id of the transition).
        """
        return self.changelog.id if self.changelog else ""

"""
Core support-request entities, agnostic of the interface they arrived from.
"""

import enum
import json

import attrs


class Capability(enum.StrEnum):
    """
    The support capabilities Otto covers. Used to tag eval cases and route
    metrics; new capabilities start by extending this taxonomy.
    """

    KNOWLEDGE = "knowledge"
    ACCESS = "access"
    TICKET = "ticket"
    RUNBOOK = "runbook"


@attrs.frozen
class SlackThread:
    """
    Origin of a request that arrived via Slack (channel mention or DM).
    """

    channel_id: str
    thread_ts: str


@attrs.frozen
class TicketRef:
    """
    Origin of a request that arrived as a ticket (Jira issue key today,
    ServiceNow sys_id at graduation).
    """

    issue_key: str


# Channel-neutral origin reference (D8): replies, escalation links, and the
# Phase 2 ApprovalRecord all address a request through this, never through
# one channel's raw fields.
Origin = SlackThread | TicketRef


def origin_to_json(origin: Origin) -> str:
    """
    Serialize an origin to a channel-tagged JSON string for durable storage
    (the ApprovalRecord ``origin`` column). Round-trips via ``origin_from_json``.
    """
    match origin:
        case SlackThread(channel_id=channel_id, thread_ts=thread_ts):
            payload = {"kind": "slack", "channel_id": channel_id, "thread_ts": thread_ts}
        case TicketRef(issue_key=issue_key):
            payload = {"kind": "ticket", "issue_key": issue_key}
    return json.dumps(payload)


def origin_from_json(raw: str) -> Origin:
    """
    Rebuild an origin from its ``origin_to_json`` form.

    :raises ValueError: if the stored kind is unknown.
    """
    data = json.loads(raw)
    match data.get("kind"):
        case "slack":
            return SlackThread(channel_id=data["channel_id"], thread_ts=data["thread_ts"])
        case "ticket":
            return TicketRef(issue_key=data["issue_key"])
        case other:
            raise ValueError(f"unknown origin kind {other!r}")


@attrs.frozen
class SupportRequest:
    """
    A single employee request as normalized by an interface (Slack today;
    the Jira webhook builds the same shape in Phase 1).
    """

    id: str
    user_id: str
    text: str
    origin: Origin

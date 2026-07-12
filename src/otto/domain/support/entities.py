"""
Core support-request entities, agnostic of the interface they arrived from.
"""

import enum

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

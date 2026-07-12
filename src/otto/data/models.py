"""
SQLModel table definitions.

Every table module must be imported in ``migrations/alembic/env.py`` so
autogenerate sees the full metadata. Split into a ``models/`` package once
this file grows past a handful of tables.
"""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel


class ApprovalRecord(SQLModel, table=True):
    """
    Durable, audited record of one HITL approval — the system of record for
    every gated-tool decision. Mirrors the domain ``PendingApproval`` plus
    audit columns (resolver, resolved-at). Channel-neutral (0.3/D8): ``origin``
    is a serialized ``SlackThread | TicketRef``, never a Slack-shaped column.
    """

    __tablename__ = "approvals"

    id: str = Field(primary_key=True)
    request_id: str
    requester_id: str
    origin: str = Field(sa_column=Column(Text, nullable=False))  # serialized Origin (json)
    request_text: str = Field(sa_column=Column(Text, nullable=False))
    tool_name: str
    tool_arguments: str = Field(sa_column=Column(Text, nullable=False))
    # Nullable so the retention sweep (2.5) can purge the conversation-bearing
    # run state after resolution (A9). Retention default: 30 days post-resolution
    # — confirm before the first audited requests land (2.1 is [NEEDS APPROVAL]).
    run_state_json: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = Field(index=True)
    card_channel: str = ""
    card_ts: str = ""
    resolver_id: str = ""
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

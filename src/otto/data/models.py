"""
SQLModel table definitions.

Every table module must be imported in ``migrations/alembic/env.py`` so
autogenerate sees the full metadata. Split into a ``models/`` package once
this file grows past a handful of tables.
"""

from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Column, DateTime, ForeignKey, Index, String, Text, text
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
    # Which surface owns the row ("slack" | "streamlit") — the sweep and the
    # startup recovery act only on their own surface's approvals.
    channel: str = Field(
        default="slack",
        sa_column=Column(String, nullable=False, server_default="slack", index=True),
    )
    status: str = Field(index=True)
    card_channel: str = ""
    card_ts: str = ""
    resolver_id: str = ""
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Stamped once the decided run's outcome was delivered (B2). Null on a
    # terminal approved/denied row = decided but never executed — the startup
    # recovery sweep resumes it instead of losing the human's decision.
    executed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Last time the triage channel was nudged about this still-pending approval
    # (2.5). Null = never reminded; the reminder sweep falls back to created_at.
    reminded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class RuntimeFlagRecord(SQLModel, table=True):
    """
    One runtime-tunable flag (C1): today only ``otto_enabled`` — the kill
    switch the routers consult per request, flippable without a restart
    (``just otto-off`` / ``just otto-on``). An absent row means the env
    default applies.
    """

    __tablename__ = "runtime_flags"

    name: str = Field(primary_key=True)
    value: str


class AuditEventRecord(SQLModel, table=True):
    """
    One rejected or system-driven HITL event (B5): unauthorized clicks,
    self-approval blocks, unverifiable identities, expiries. Append-only —
    the audit report reads it alongside the approvals projection so attempts
    are as durable as decisions.
    """

    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(index=True)
    actor_id: str
    approval_id: str = ""
    detail: str = ""
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AgentSessionRecord(SQLModel, table=True):
    """
    One conversation session — the openai-agents session store's parent table
    (its items live in ``agent_messages``), extended with Otto's columns: the
    channel tag (streamlit/slack), display title, the trace-root ids that keep
    a whole conversation in one trace, and any paused HITL approval.

    The SDK also writes this table (``agents.extensions.memory
    .sqlalchemy_session``, pinned 0.18.2): it inserts bare ``session_id`` rows
    when none exist and touches ``updated_at`` — so every Otto column must
    keep a server default (or be nullable), and the SDK-shaped columns must
    not drift from the SDK's definition.
    """

    __tablename__ = "agent_sessions"

    session_id: str = Field(primary_key=True)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )
    channel: str = Field(
        default="",
        sa_column=Column(String, nullable=False, server_default="", index=True),
    )
    title: str = Field(default="", sa_column=Column(String, nullable=False, server_default=""))
    # Root-span ids of the conversation's trace: every turn and approval
    # parents into them, so one chat = one trace across process restarts.
    trace_id: str = Field(default="", sa_column=Column(String, nullable=False, server_default=""))
    span_id: str = Field(default="", sa_column=Column(String, nullable=False, server_default=""))
    # Id of the paused approval in the approvals store (D1) — the RunState
    # lives there, exactly as for Slack; null = nothing pending.
    pending_state: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    pending_tool: str = Field(
        default="", sa_column=Column(String, nullable=False, server_default="")
    )
    pending_args: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )


class AgentMessageRecord(SQLModel, table=True):
    """
    One stored conversation item, SDK-shaped (``message_data`` is the
    serialized input item): the openai-agents session store owns all reads
    and writes; this model only keeps alembic in charge of the schema.
    """

    __tablename__ = "agent_messages"
    __table_args__ = (Index("idx_agent_messages_session_time", "session_id", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    message_data: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )


class UserRecord(SQLModel, table=True):
    """
    Durable identity + approver role — the Phase 2 (2.3) home for what the
    ``users.yaml`` directory (D15) and the D3 approver-id settings lists carry
    in the prototype. Surrogate id because a person may have only one of the
    channel ids; role is a single column (``support_user``/``admin``/empty) —
    a user_roles join table is the graduation shape if many roles are needed.
    """

    __tablename__ = "users"

    id: str = Field(primary_key=True)
    name: str
    team: str = ""
    slack_user_id: str = Field(default="", index=True)
    jira_account_id: str = Field(default="", index=True)
    role: str = Field(default="", index=True)

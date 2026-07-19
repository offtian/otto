"""
add agent_sessions + agent_messages

Revision ID: 0004_chat_sessions
Revises: 0003_approval_reminded_at
Create Date: 2026-07-19

The openai-agents session store's tables, created here (not by the SDK's
create_tables) so alembic stays the single schema owner. agent_sessions
carries the SDK-shaped columns (session_id/created_at/updated_at — the SDK
inserts bare session_id rows and touches updated_at, so every Otto column
keeps a server default or is nullable) plus Otto's: the channel tag
(streamlit/slack), display title, trace-root ids, and any paused HITL
approval. agent_messages is purely SDK-shaped. Mirrors
data.models.AgentSessionRecord / AgentMessageRecord; the SDK's expected
shape is agents.extensions.memory.sqlalchemy_session (pinned 0.18.2).
"""

import sqlalchemy as sa
from alembic import op


revision = "0004_chat_sessions"
down_revision = "0003_approval_reminded_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(), server_default="", nullable=False),
        sa.Column("title", sa.String(), server_default="", nullable=False),
        sa.Column("trace_id", sa.String(), server_default="", nullable=False),
        sa.Column("span_id", sa.String(), server_default="", nullable=False),
        sa.Column("pending_state", sa.Text(), nullable=True),
        sa.Column("pending_tool", sa.String(), server_default="", nullable=False),
        sa.Column("pending_args", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(op.f("ix_agent_sessions_channel"), "agent_sessions", ["channel"], unique=False)
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("message_data", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_agent_messages_session_time",
        "agent_messages",
        ["session_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_agent_messages_session_time", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_index(op.f("ix_agent_sessions_channel"), table_name="agent_sessions")
    op.drop_table("agent_sessions")

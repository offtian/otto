"""
add approvals.channel

Revision ID: 0008_approval_channel
Revises: 0007_runtime_flags
Create Date: 2026-07-19

Surface tag for each approval ("slack" | "streamlit"): the server's expiry
sweep and startup recovery act only on their own surface's rows — without
the tag they would close dev-chat cards into a nonexistent Slack channel and
replay chat-approved tools from the server process. Existing rows are all
Slack's, which the server default covers. Hand-written to avoid needing a
live database. Mirrors data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0008_approval_channel"
down_revision = "0007_runtime_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("channel", sa.String, nullable=False, server_default="slack", index=True),
    )


def downgrade() -> None:
    op.drop_column("approvals", "channel")

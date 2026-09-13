"""
add approvals.node

Revision ID: 0009_approval_node
Revises: 0008_approval_channel
Create Date: 2026-09-13

Flow-graph node that suspended for each approval: the resume path re-enters
the support graph there, so the right agent is rebuilt for the paused run.
Existing rows all predate intent routing and belong to the general agent,
which the server default covers. Hand-written to avoid needing a live
database. Mirrors data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0009_approval_node"
down_revision = "0008_approval_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("node", sa.String, nullable=False, server_default="general"),
    )


def downgrade() -> None:
    op.drop_column("approvals", "node")

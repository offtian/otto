"""
drop approvals.graph_state_json

Revision ID: 0014_drop_approval_graph_state
Revises: 0013_follow_ups
Create Date: 2026-11-22

The column carried one thing — the owning team a paused run was routed to —
so it could resume on that team's owner agent. Team-owned flows are gone (the
general agent carries the catalogued specialists and repositories), and a
paused run needs only its node name again. Hand-written to avoid needing a
live database. Mirrors data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0014_drop_approval_graph_state"
down_revision = "0013_follow_ups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("approvals", "graph_state_json")


def downgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("graph_state_json", sa.Text, nullable=False, server_default=""),
    )

"""
add approvals.graph_state_json

Revision ID: 0010_approval_graph_state
Revises: 0009_approval_node
Create Date: 2026-09-13

Durable flow state for a paused run (the resolved owning team, primitives
only): the resume path merges it back before re-entering the support graph
at the suspended node, so a team-owned run resumes on its own owner agent.
Existing rows predate team flows — the empty default is correct for them.
Hand-written to avoid needing a live database. Mirrors
data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0010_approval_graph_state"
down_revision = "0009_approval_node"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("graph_state_json", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("approvals", "graph_state_json")

"""
add approvals.executed_at

Revision ID: 0005_approval_executed_at
Revises: 0004_chat_sessions
Create Date: 2026-07-19

B2: a terminal approved/denied row with a null ``executed_at`` was decided but
its run never completed (crash between resolve and resume) — the startup
recovery sweep resumes it. Backfills terminal rows that predate the column so
the first restart after this migration does not re-run history. Hand-written
to avoid needing a live database. Mirrors data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0005_approval_executed_at"
down_revision = "0004_chat_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE approvals SET executed_at = resolved_at WHERE status != 'pending'")


def downgrade() -> None:
    op.drop_column("approvals", "executed_at")

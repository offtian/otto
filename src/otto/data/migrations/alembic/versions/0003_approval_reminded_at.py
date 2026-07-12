"""
add approvals.reminded_at

Revision ID: 0003_approval_reminded_at
Revises: 0002_users
Create Date: 2026-07-12

The maintenance sweep (2.5) nudges the triage channel about still-pending
approvals; this column records when it last did so, so reminders fire at the
configured interval instead of every sweep tick. Nullable — null means never
reminded, and the sweep falls back to created_at. Hand-written to avoid needing
a live database. Mirrors data.models.ApprovalRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0003_approval_reminded_at"
down_revision = "0002_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approvals", "reminded_at")

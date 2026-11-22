"""
add follow_ups

Revision ID: 0013_follow_ups
Revises: 0012_outbox
Create Date: 2026-11-22

Follow-ups an agent schedules for itself: one row per "check back later",
turned into an inbound request on the same conversation once ``due_at``
passes. Hand-written to avoid needing a live database. Mirrors
data.models.FollowUpRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0013_follow_ups"
down_revision = "0012_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("requester_id", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_follow_ups_origin", "follow_ups", ["origin"])
    op.create_index("ix_follow_ups_status", "follow_ups", ["status"])


def downgrade() -> None:
    op.drop_index("ix_follow_ups_status", table_name="follow_ups")
    op.drop_index("ix_follow_ups_origin", table_name="follow_ups")
    op.drop_table("follow_ups")

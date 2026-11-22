"""
add outbox

Revision ID: 0012_outbox
Revises: 0011_inbound_requests
Create Date: 2026-11-22

The outbox: one row per reply Otto owes a conversation, recorded before it
is sent so a channel outage delays the reply instead of losing it (retried
with backoff, dead after the fifth failure). The unique idempotency key makes
a replayed run's second answer a no-op. Hand-written to avoid needing a live
database. Mirrors data.models.OutboundMessageRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0012_outbox"
down_revision = "0011_inbound_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(), nullable=False, unique=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("vote", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outbox_status", "outbox", ["status"])


def downgrade() -> None:
    op.drop_index("ix_outbox_status", table_name="outbox")
    op.drop_table("outbox")

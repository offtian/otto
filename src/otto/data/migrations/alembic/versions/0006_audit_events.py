"""
add audit_events

Revision ID: 0006_audit_events
Revises: 0005_approval_executed_at
Create Date: 2026-07-19

B5: rejected clicks (unauthorized role, self-approval, unverifiable identity)
and sweep expiries become durable audit rows, not just log events — the audit
report shows who *tried* and was turned away. Hand-written to avoid needing a
live database. Mirrors data.models.AuditEventRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0006_audit_events"
down_revision = "0005_approval_executed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String, nullable=False, index=True),
        sa.Column("actor_id", sa.String, nullable=False),
        sa.Column("approval_id", sa.String, nullable=False, server_default=""),
        sa.Column("detail", sa.String, nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_events")

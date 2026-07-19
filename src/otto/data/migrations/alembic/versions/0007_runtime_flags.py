"""
add runtime_flags

Revision ID: 0007_runtime_flags
Revises: 0006_audit_events
Create Date: 2026-07-19

C1: the kill switch becomes flippable at runtime — routers consult the
``otto_enabled`` row (short-TTL cached) instead of the env value frozen at
process start. An absent row means the env default applies, so the table
ships empty. Hand-written to avoid needing a live database. Mirrors
data.models.RuntimeFlagRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0007_runtime_flags"
down_revision = "0006_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_flags",
        sa.Column("name", sa.String, primary_key=True),
        sa.Column("value", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_flags")

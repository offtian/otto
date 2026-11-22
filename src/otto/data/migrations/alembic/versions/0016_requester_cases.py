"""
add requester_cases

Revision ID: 0016_requester_cases
Revises: 0015_memory_notes
Create Date: 2026-11-22

Per-requester history: which de-identified memory note each resolved case of
a requester was filed under, and when. The one table in which memory names a
person; rows go with their note. Hand-written to avoid needing a live
database. Mirrors data.models.RequesterCaseRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0016_requester_cases"
down_revision = "0015_memory_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requester_cases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requester_id", sa.String(), nullable=False),
        sa.Column(
            "memory_id",
            sa.Integer(),
            sa.ForeignKey("memory_notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_requester_cases_requester", "requester_cases", ["requester_id", "happened_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_requester_cases_requester", table_name="requester_cases")
    op.drop_table("requester_cases")

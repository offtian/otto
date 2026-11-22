"""
add memory_notes

Revision ID: 0015_memory_notes
Revises: 0014_drop_approval_graph_state
Create Date: 2026-11-22

Long-term memory moves into Postgres: one de-identified case note per row,
searched by keyword through a full-text index, with how often and how
recently a resolved case matched it. Replaces the embedded Cognee graph,
whose data lived outside this database and is not migrated. Hand-written to
avoid needing a live database. Mirrors data.models.MemoryNoteRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0015_memory_notes"
down_revision = "0014_drop_approval_graph_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_memory_notes_fts",
        "memory_notes",
        [sa.text("to_tsvector('english', note)")],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_memory_notes_fts", table_name="memory_notes")
    op.drop_table("memory_notes")

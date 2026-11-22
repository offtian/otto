"""
add inbound_requests

Revision ID: 0011_inbound_requests
Revises: 0010_approval_graph_state
Create Date: 2026-11-22

The durable inbox: one row per inbound support request from its intake ack
until its handler finishes, so a request that was in flight when the process
died is replayed at startup instead of dropped silently (FR8). Hand-written
to avoid needing a live database. Mirrors data.models.InboundRequestRecord.
"""

import sqlalchemy as sa
from alembic import op


revision = "0011_inbound_requests"
down_revision = "0010_approval_graph_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("requester_id", sa.String(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("inbound_requests")

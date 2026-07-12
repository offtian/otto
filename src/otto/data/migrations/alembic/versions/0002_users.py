"""
add users table

Revision ID: 0002_users
Revises: 0001_approvals
Create Date: 2026-07-12

The Phase 2 (2.3) durable home for requester identity (D15) and approver role
(D3). Hand-written to avoid needing a live database. Mirrors
data.models.UserRecord; the config wiring that reads it (directory + role
lookup, fail-closed) lands once Postgres is available.
"""

import sqlalchemy as sa
from alembic import op


revision = "0002_users"
down_revision = "0001_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("team", sa.String(), nullable=False),
        sa.Column("slack_user_id", sa.String(), nullable=False),
        sa.Column("jira_account_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
    )
    op.create_index("ix_users_slack_user_id", "users", ["slack_user_id"])
    op.create_index("ix_users_jira_account_id", "users", ["jira_account_id"])
    op.create_index("ix_users_role", "users", ["role"])


def downgrade() -> None:
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_jira_account_id", table_name="users")
    op.drop_index("ix_users_slack_user_id", table_name="users")
    op.drop_table("users")

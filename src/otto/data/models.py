"""
SQLModel table definitions.

Every table module must be imported in ``migrations/alembic/env.py`` so
autogenerate sees the full metadata. Split into a ``models/`` package once
this file grows past a handful of tables.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


class ExampleRecord(SQLModel, table=True):
    """
    Working autogenerate example — replace with real tables.
    """

    __tablename__ = "examples"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

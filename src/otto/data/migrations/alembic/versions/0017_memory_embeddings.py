"""
add memory_notes.embedding

Revision ID: 0017_memory_embeddings
Revises: 0016_requester_cases
Create Date: 2026-11-22

Search by meaning: each note may carry the vector an embedding model made of
it, compared with pgvector. This is the first migration that needs an
extension — the server must offer ``vector`` (the compose image does:
``pgvector/pgvector``), and the migrating role must be allowed to create it.
The column has no fixed length, so the embedding model can change without a
migration. Hand-written to avoid needing a live database. Mirrors
data.models.MemoryNoteRecord.
"""

from alembic import op


revision = "0017_memory_embeddings"
down_revision = "0016_requester_cases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("ALTER TABLE memory_notes ADD COLUMN embedding vector")


def downgrade() -> None:
    # The extension stays: something else in the database may be using it.
    op.execute("ALTER TABLE memory_notes DROP COLUMN embedding")

"""RAG knowledge base: vector extension, kb_documents, kb_chunks (Phase 8, step 1)

Revision ID: d8f3a2b61c05
Revises: c5e1b8a47d92
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "d8f3a2b61c05"
down_revision = "c5e1b8a47d92"
branch_labels = None
depends_on = None

# Frozen on purpose: a migration must not change when app code changes. A model with another
# vector size needs a NEW migration (see integrations/embeddings/base.py).
EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("source_key", "version", name="uq_kb_documents_source_version"),
    )
    # at most ONE active version per source
    op.create_index(
        "uq_kb_documents_one_active",
        "kb_documents",
        ["source_key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index("ix_kb_documents_type_active", "kb_documents", ["doc_type", "is_active"])

    op.create_table(
        "kb_chunks",
        sa.Column("chunk_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "doc_id",
            sa.Integer(),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        # full-text search column, filled by Postgres itself
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(heading_path, '') || ' ' || text)",
                persisted=True,
            ),
        ),
        sa.UniqueConstraint("doc_id", "ordinal", name="uq_kb_chunks_doc_ordinal"),
    )
    op.create_index("ix_kb_chunks_doc_id", "kb_chunks", ["doc_id"])
    op.create_index("ix_kb_chunks_content_hash", "kb_chunks", ["content_hash"])
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], postgresql_using="gin")
    # No vector index yet: with a few hundred chunks a plain scan is fast and exact. Add an HNSW
    # index (vector_cosine_ops) later if the table grows; check the host's pgvector version first.


def downgrade() -> None:
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    # The vector extension stays: dropping it could break something else that uses it.

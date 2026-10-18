"""Handoffs table (Phase 11, step 5)

Revision ID: e6a1b7c3d052
Revises: b9d4e7a21c58
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "e6a1b7c3d052"
down_revision = "b9d4e7a21c58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per request for a person (blueprint 8.6 and the 'handoffs' table of the data model).
    # At most one OPEN row per conversation: the partial unique index below.
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("urgency", sa.String(length=8), nullable=False, server_default="normal"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_handoffs_one_open",
        "handoffs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_handoffs_status_created", "handoffs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("handoffs")

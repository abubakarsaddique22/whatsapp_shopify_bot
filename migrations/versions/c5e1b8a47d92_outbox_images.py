"""add image columns to whatsapp_outbox (Phase 7, step 3)

Revision ID: c5e1b8a47d92
Revises: 7b2e5f1a9c63
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e1b8a47d92"
down_revision = "7b2e5f1a9c63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # kind: 'text' (default, every existing row) or 'image'. For an image row, `body` stays the
    # plain-text fallback (product name + link) that is sent if the picture cannot be delivered.
    op.add_column(
        "whatsapp_outbox",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="text"),
    )
    op.add_column("whatsapp_outbox", sa.Column("media_url", sa.Text(), nullable=True))
    op.add_column("whatsapp_outbox", sa.Column("caption", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("whatsapp_outbox", "caption")
    op.drop_column("whatsapp_outbox", "media_url")
    op.drop_column("whatsapp_outbox", "kind")

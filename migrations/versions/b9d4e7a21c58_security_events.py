"""Security events table (Phase 11, step 3b)

Revision ID: b9d4e7a21c58
Revises: f2a9c4d17b38
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b9d4e7a21c58"
down_revision = "f2a9c4d17b38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per suspected attack or abuse (blueprint 9.3 point 7 and 9.4): a flagged document,
    # an injection attempt, a rate-limit strike. No secrets, no message text, no phone number:
    # wa_hash is the keyed hash of the sender, detail holds short facts (matched pattern names).
    op.create_table(
        "security_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("wa_hash", sa.String(length=64), nullable=True),
        sa.Column("ref", sa.String(length=200), nullable=True),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_security_events_kind_created", "security_events", ["kind", "created_at"])
    op.create_index("ix_security_events_wa_hash", "security_events", ["wa_hash"])


def downgrade() -> None:
    op.drop_table("security_events")

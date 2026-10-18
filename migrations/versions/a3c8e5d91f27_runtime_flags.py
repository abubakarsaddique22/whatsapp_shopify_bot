"""Runtime flags table (Phase 11, step 8)

Revision ID: a3c8e5d91f27
Revises: e6a1b7c3d052
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "a3c8e5d91f27"
down_revision = "e6a1b7c3d052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Switches that the admin API flips while the worker is running (the API and the worker are
    # different processes, so a setting in .env cannot do this). Today only 'kill_switch'.
    op.create_table(
        "runtime_flags",
        sa.Column("name", sa.String(length=40), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_flags")

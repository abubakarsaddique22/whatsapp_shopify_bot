"""Shopify sync: sync_events and sync_runs (Phase 10, step 1)

Revision ID: f2a9c4d17b38
Revises: d8f3a2b61c05
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f2a9c4d17b38"
down_revision = "d8f3a2b61c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per webhook delivery from Shopify. The unique webhook_id makes a second delivery of
    # the same event harmless (blueprint 4.7.4): the insert fails, the endpoint still answers 200.
    op.create_table(
        "sync_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.String(length=100), nullable=False),
        sa.Column("topic", sa.String(length=64), nullable=False),
        sa.Column("shop_domain", sa.String(length=200), nullable=False),
        # Shopify global id of the product / inventory item the event is about (may be missing)
        sa.Column("resource_id", sa.String(), nullable=True),
        # When Shopify says the event happened (header X-Shopify-Triggered-At, VERIFY). Only for
        # logs and ordering checks: handlers always refetch the object, so order does not matter.
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        # Small body of the webhook (product or inventory data only, no customer data)
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # pending | processing | done | failed | ignored
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        # backoff: the worker picks a pending row only when this time has passed
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("webhook_id", name="uq_sync_events_webhook_id"),
    )
    op.create_index("ix_sync_events_status_received", "sync_events", ["status", "received_at"])
    op.create_index("ix_sync_events_resource_id", "sync_events", ["resource_id"])

    # One row per reconciliation run (blueprint 4.7.6): delta, full or policy.
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=16), nullable=False),  # delta | full | policy
        # running | ok | failed
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The next delta run fetches products with updated_at newer than the watermark of the
        # last run that finished 'ok'.
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        # e.g. {"seen": 40, "upserted": 3, "deactivated": 1, "reindexed": 2, "errors": 0}
        sa.Column("counts", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_sync_runs_kind_started", "sync_runs", ["kind", "started_at"])


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("sync_events")

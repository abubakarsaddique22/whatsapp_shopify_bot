"""whatsapp text: conversations, whatsapp_inbox, messages, whatsapp_outbox

Revision ID: 7b2e5f1a9c63
Revises: 3a1c7e9d2b40
Create Date: 2026-10-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "7b2e5f1a9c63"
down_revision = "3a1c7e9d2b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("wa_id_hash", sa.String(length=16), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wa_id", name="uq_conversations_wa_id"),
    )
    op.create_index(op.f("ix_conversations_wa_id_hash"), "conversations", ["wa_id_hash"])

    op.create_table(
        "whatsapp_inbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("wamid", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("wa_id_hash", sa.String(length=16), nullable=False),
        sa.Column("msg_type", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("wa_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wamid", name="uq_whatsapp_inbox_wamid"),
    )
    op.create_index(
        op.f("ix_whatsapp_inbox_conversation_id"), "whatsapp_inbox", ["conversation_id"]
    )
    op.create_index(op.f("ix_whatsapp_inbox_status"), "whatsapp_inbox", ["status"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("wamid", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wamid", name="uq_messages_wamid"),
    )
    op.create_index(op.f("ix_messages_conversation_id"), "messages", ["conversation_id"])

    op.create_table(
        "whatsapp_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("inbox_id", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("wamid", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inbox_id"], ["whatsapp_inbox.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inbox_id", "seq", name="uq_whatsapp_outbox_inbox_seq"),
    )
    op.create_index(
        op.f("ix_whatsapp_outbox_conversation_id"), "whatsapp_outbox", ["conversation_id"]
    )
    op.create_index(op.f("ix_whatsapp_outbox_status"), "whatsapp_outbox", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_whatsapp_outbox_status"), table_name="whatsapp_outbox")
    op.drop_index(op.f("ix_whatsapp_outbox_conversation_id"), table_name="whatsapp_outbox")
    op.drop_table("whatsapp_outbox")
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_whatsapp_inbox_status"), table_name="whatsapp_inbox")
    op.drop_index(op.f("ix_whatsapp_inbox_conversation_id"), table_name="whatsapp_inbox")
    op.drop_table("whatsapp_inbox")
    op.drop_index(op.f("ix_conversations_wa_id_hash"), table_name="conversations")
    op.drop_table("conversations")

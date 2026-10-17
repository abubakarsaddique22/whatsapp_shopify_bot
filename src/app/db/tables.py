"""SQLAlchemy tables. Starts empty; each phase adds its tables and an Alembic migration.

Phase 3: catalog mirror | Phase 4: whatsapp_inbox, whatsapp_outbox, conversations, messages
Phase 8: kb_documents, kb_chunks | Phase 10: sync_events, sync_runs | Phase 11: handoffs
(the Phase 10 tables are at the end of this file)
"""

from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.integrations.embeddings.base import EMBEDDING_DIM


class Base(DeclarativeBase):
    pass


# ---- Phase 3: catalog mirror (Blueprint 4.6)
# The mirror is only for FINDING products (search, options, specs, images). Price and stock
# shown to a customer always come from a live Shopify call (gateway.get_variant).


class ProductMirror(Base):
    __tablename__ = "products_mirror"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Shopify global id
    handle: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    vendor: Mapped[str] = mapped_column(Text, default="")
    product_type: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    options: Mapped[list[dict]] = mapped_column(JSONB, default=list)  # [{name, values}]
    specs: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)  # specs_mapping.yaml names
    description_text: Mapped[str] = mapped_column(Text, default="")
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))  # changes when the product changes
    updated_at_shopify: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "ix_products_mirror_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )


class VariantMirror(Base):
    __tablename__ = "variants_mirror"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Shopify global id
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products_mirror.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)  # {"Colour": "Black"}
    price_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))  # for max_price filters only
    currency: Mapped[str] = mapped_column(String(3))
    available_for_sale: Mapped[bool] = mapped_column(Boolean, default=True)
    image_url: Mapped[str | None] = mapped_column(Text)


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products_mirror.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)  # 0 = first (featured) image
    url: Mapped[str] = mapped_column(Text)
    alt: Mapped[str] = mapped_column(Text, default="")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("product_id", "position", name="uq_product_images_pos"),)


# ---- Phase 4: WhatsApp text
# wa_id (the raw phone number) is stored in conversations ONLY, because Meta needs it to send a
# reply. Everything else (logs, analytics, inbox) uses wa_id_hash. Never log wa_id.


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_id: Mapped[str] = mapped_column(String(32))
    wa_id_hash: Mapped[str] = mapped_column(String(16), index=True)
    lang: Mapped[str | None] = mapped_column(String(8))  # set by Phase 5
    state: Mapped[dict] = mapped_column(JSONB, default=dict)  # ConversationState as JSON
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("wa_id", name="uq_conversations_wa_id"),)


class WhatsAppInbox(Base):
    """One row per message Meta delivers. The unique wamid makes redelivery harmless."""

    __tablename__ = "whatsapp_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wamid: Mapped[str] = mapped_column(String)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    wa_id_hash: Mapped[str] = mapped_column(String(16))
    msg_type: Mapped[str] = mapped_column(String(16))  # text | audio | image | other
    text: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[str | None] = mapped_column(String)  # used by Phase 6 and 7
    mime_type: Mapped[str | None] = mapped_column(String)
    wa_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("wamid", name="uq_whatsapp_inbox_wamid"),)
    # status values: pending | processing | done | failed | ignored


class Message(Base):
    """The conversation transcript (customer and bot turns)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    body: Mapped[str] = mapped_column(Text)
    wamid: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("wamid", name="uq_messages_wamid"),)


class WhatsAppOutbox(Base):
    """One row per reply to send. (inbox_id, seq) stops a retry sending the same reply twice."""

    __tablename__ = "whatsapp_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    inbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("whatsapp_inbox.id", ondelete="SET NULL")
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text)  # the text to send; for an image row, the text fallback
    kind: Mapped[str] = mapped_column(String(16), default="text", server_default="text")
    media_url: Mapped[str | None] = mapped_column(Text)  # image rows only: the picture URL
    caption: Mapped[str | None] = mapped_column(Text)  # image rows only: short caption
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    wamid: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("inbox_id", "seq", name="uq_whatsapp_outbox_inbox_seq"),)
    # status values: pending | sent | failed


# ---- Phase 8: RAG knowledge base (Blueprint 7.4)
# Only approved store text goes in: policies, pages, FAQ, product descriptions. Never price, stock,
# customer messages or orders. A changed document gets a NEW row (version + 1) and the old row
# becomes inactive; retrieval reads active documents only.


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # doc_id
    # Stable name of the source, the same for every version: "policy:REFUND_POLICY" or
    # "page:<shopify page id>". This is how ingest finds the previous version of a document.
    source_key: Mapped[str] = mapped_column(String(200))
    doc_type: Mapped[str] = mapped_column(String(32))  # policy_refund | policy_shipping | ...
    source: Mapped[str] = mapped_column(String(32))  # shopify_policy | shopify_page | product
    title: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str | None] = mapped_column(Text)  # the official link a reply may use
    product_id: Mapped[str | None] = mapped_column(String)  # only for product_description
    language: Mapped[str] = mapped_column(String(8), default="en")
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))  # hash of the cleaned text
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source_key", "version", name="uq_kb_documents_source_version"),
        # at most ONE active version per source
        Index(
            "uq_kb_documents_one_active",
            "source_key",
            unique=True,
            postgresql_where=sa_text("is_active"),
        ),
        Index("ix_kb_documents_type_active", "doc_type", "is_active"),
    )


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    chunk_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)  # position inside the document, from 0
    heading_path: Mapped[str | None] = mapped_column(Text)  # "Returns > Refund time"
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # hash of heading_path + text
    embedding_model: Mapped[str] = mapped_column(String(100))  # a model change finds old chunks
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    # full-text search column, filled by Postgres itself (nothing to write from Python)
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(heading_path, '') || ' ' || text)", persisted=True
        ),
    )

    __table_args__ = (
        UniqueConstraint("doc_id", "ordinal", name="uq_kb_chunks_doc_ordinal"),
        Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
    )


# ---- Phase 10: Shopify sync (Blueprint 4.7)
# Webhooks only SAY that something changed; handlers refetch the object by id, so the data in the
# payload is never trusted and an old event cannot overwrite newer data.


class SyncEvent(Base):
    """One row per Shopify webhook delivery. The unique webhook_id makes redelivery harmless."""

    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    webhook_id: Mapped[str] = mapped_column(String(100))  # X-Shopify-Event-Id, else Webhook-Id
    topic: Mapped[str] = mapped_column(String(64))  # products/update, inventory_levels/update ...
    shop_domain: Mapped[str] = mapped_column(String(200))
    resource_id: Mapped[str | None] = mapped_column(String)  # Shopify global id of the object
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("webhook_id", name="uq_sync_events_webhook_id"),
        Index("ix_sync_events_status_received", "status", "received_at"),
        Index("ix_sync_events_resource_id", "resource_id"),
    )
    # status values: pending | processing | done | failed (dead letter) | ignored


class SyncRun(Base):
    """One row per reconciliation run: delta (minutes), full (nightly), policy (hourly)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # delta | full | policy
    status: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the next delta run asks Shopify for products updated after the watermark of the last ok run
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_sync_runs_kind_started", "kind", "started_at"),)
    # status values: running | ok | failed

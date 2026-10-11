"""SQLAlchemy tables. Starts empty; each phase adds its tables and an Alembic migration.

Phase 3: catalog mirror | Phase 4: whatsapp_inbox, whatsapp_outbox, conversations, messages
Phase 8: kb_documents, kb_chunks | Phase 10: sync_events, sync_runs | Phase 11: handoffs
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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

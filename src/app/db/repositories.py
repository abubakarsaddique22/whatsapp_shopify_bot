"""Database reads and writes for the catalogue mirror (Phase 3, step 4).

Plain functions that take an AsyncSession. The caller owns the transaction (commit/rollback).
Only product DATA is stored here; live price and stock always come from Shopify (Blueprint 4.6).
"""

from collections.abc import Collection, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Row, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import ProductImage, ProductMirror, VariantMirror


async def mirror_state(session: AsyncSession) -> dict[str, tuple[str, bool]]:
    """{product_id: (content_hash, is_visible)} for every product already in the mirror."""
    rows = await session.execute(
        select(ProductMirror.id, ProductMirror.content_hash, ProductMirror.is_visible)
    )
    return {pid: (content_hash, visible) for pid, content_hash, visible in rows.all()}


async def upsert_product(
    session: AsyncSession,
    product_row: dict[str, Any],
    variant_rows: list[dict[str, Any]],
    image_rows: list[dict[str, Any]],
) -> None:
    """Insert or update one product and replace its variants and images.

    Variants and images are deleted and re-inserted: a product has few of them, and this way a
    removed variant or image can never stay behind in the mirror.
    """
    stmt = insert(ProductMirror).values(**product_row)
    changes = {k: v for k, v in product_row.items() if k != "id"}
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=changes))

    product_id = product_row["id"]
    await session.execute(delete(VariantMirror).where(VariantMirror.product_id == product_id))
    await session.execute(delete(ProductImage).where(ProductImage.product_id == product_id))
    if variant_rows:
        await session.execute(insert(VariantMirror), variant_rows)
    if image_rows:
        await session.execute(insert(ProductImage), image_rows)


async def hide_missing(session: AsyncSession, seen_ids: Collection[str]) -> int:
    """Mark products that Shopify no longer shows as not visible. Returns how many changed.

    They are hidden, not deleted: search must skip them (is_visible = false) but history stays.
    """
    result = await session.execute(
        update(ProductMirror)
        .where(ProductMirror.is_visible.is_(True), ProductMirror.id.not_in(list(seen_ids)))
        .values(is_visible=False)
    )
    return cast("CursorResult[Any]", result).rowcount or 0


async def product_basics(session: AsyncSession, product_id: str) -> Row[Any] | None:
    """Mirror facts of ONE visible product, or None (missing and hidden look the same)."""
    result = await session.execute(
        select(
            ProductMirror.id,
            ProductMirror.title,
            ProductMirror.url,
            ProductMirror.vendor,
            ProductMirror.product_type,
            ProductMirror.tags,
            ProductMirror.options,
            ProductMirror.description_text,
        ).where(ProductMirror.id == product_id, ProductMirror.is_visible.is_(True))
    )
    return result.one_or_none()


async def variants_of(session: AsyncSession, product_id: str) -> Sequence[Row[Any]]:
    """Variant id, title and options from the mirror. No price and no stock: those are live."""
    result = await session.execute(
        select(VariantMirror.id, VariantMirror.title, VariantMirror.options)
        .where(VariantMirror.product_id == product_id)
        .order_by(VariantMirror.title)
    )
    return cast("Sequence[Row[Any]]", result.all())

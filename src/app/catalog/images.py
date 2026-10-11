"""Product pictures from the mirror (Phase 3, step 6; Blueprint 4.9 'Images').

Rules: a named variant or colour gets that variant's own picture when it has one, otherwise the
product's featured (first) picture. 'More pictures' means up to 3, never the whole gallery. No
picture at all means an honest message plus the product page link: never an invented or unrelated
image. URLs are returned exactly as stored; JPEG conversion and sending are Phase 7.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.normalizer import colour_of_value, colours_of
from app.db.tables import ProductImage, ProductMirror, VariantMirror

MAX_IMAGES = 3
NO_IMAGE_MESSAGE = "I do not have a picture of this product. You can see it on the product page."


class ImageInfo(BaseModel):
    url: str
    alt: str = ""
    source: Literal["variant", "featured", "gallery"] = "gallery"


class ImageResult(BaseModel):
    product_id: str
    title: str
    product_url: str  # always given: the fallback when there is no picture
    images: list[ImageInfo]  # at most MAX_IMAGES, no duplicates
    colour: str | None = None  # the colour that was asked for (canonical)
    colour_image_found: bool | None = None  # None: no colour asked. False: main picture used.
    message: str = ""  # NO_IMAGE_MESSAGE when images is empty


@dataclass(frozen=True)
class VariantImage:
    id: str
    options: dict[str, str]
    image_url: str | None


def _same_picture(url: str) -> str:
    """Shopify adds '?v=123' to the same file; ignore the query string when comparing."""
    return url.split("?", 1)[0]


def pick_images(
    gallery: list[ImageInfo],
    variants: list[VariantImage],
    *,
    variant_id: str | None = None,
    colour: str | None = None,
    max_images: int = 1,
) -> tuple[list[ImageInfo], str | None, bool | None]:
    """Returns (images, canonical colour asked, whether a colour-specific picture was found).

    `gallery` is the product's pictures in order, first = featured. A given `variant_id` wins
    over `colour`.
    """
    limit = max(1, min(max_images, MAX_IMAGES))
    wanted = colour_of_value(colour) if colour else None

    chosen: ImageInfo | None = None
    colour_found: bool | None = None
    own = next((v for v in variants if variant_id and v.id == variant_id), None)
    if own and own.image_url:
        chosen = ImageInfo(url=own.image_url, source="variant")
    elif wanted:
        colour_found = False
        for variant in variants:
            if variant.image_url and wanted in colours_of(variant.options):
                chosen = ImageInfo(url=variant.image_url, source="variant")
                colour_found = True
                break

    images: list[ImageInfo] = []
    seen: set[str] = set()

    def add(image: ImageInfo) -> None:
        key = _same_picture(image.url)
        if key not in seen and len(images) < limit:
            seen.add(key)
            images.append(image)

    if chosen:
        add(chosen)
    elif gallery:
        add(gallery[0].model_copy(update={"source": "featured"}))
    for image in gallery:
        add(image.model_copy(update={"source": "gallery"}))
    return images, wanted, colour_found


async def get_images(
    session: AsyncSession,
    product_id: str,
    *,
    variant_id: str | None = None,
    colour: str | None = None,
    max_images: int = 1,
) -> ImageResult | None:
    """Pictures of one visible product. None if it does not exist or is hidden."""
    product = (
        await session.execute(
            select(ProductMirror.id, ProductMirror.title, ProductMirror.url).where(
                ProductMirror.id == product_id, ProductMirror.is_visible.is_(True)
            )
        )
    ).one_or_none()
    if product is None:
        return None

    gallery_rows = await session.execute(
        select(ProductImage.url, ProductImage.alt)
        .where(ProductImage.product_id == product_id)
        .order_by(ProductImage.position)
    )
    variant_rows = await session.execute(
        select(VariantMirror.id, VariantMirror.options, VariantMirror.image_url)
        .where(VariantMirror.product_id == product_id)
        .order_by(VariantMirror.title)
    )
    images, wanted, colour_found = pick_images(
        [ImageInfo(url=url, alt=alt or "") for url, alt in gallery_rows.all()],
        [VariantImage(vid, options or {}, image) for vid, options, image in variant_rows.all()],
        variant_id=variant_id,
        colour=colour,
        max_images=max_images,
    )
    return ImageResult(
        product_id=product.id,
        title=product.title,
        product_url=product.url,
        images=images,
        colour=wanted,
        colour_image_found=colour_found,
        message="" if images else NO_IMAGE_MESSAGE,
    )

"""Specifications and case sizes from the mirror (Phase 3, step 6; Blueprint T04).

Values come only from the merchant's metafields (names in config/specs_mapping.yaml) and from the
variant options ('Case size: 40mm'). The product description is merchant-written free text and is
NOT used for dimensions. If a value is not there, the answer is 'not available': never a guess.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.normalizer import canonical_option_name, normalize_text, parse_mm
from app.db.tables import ProductMirror, VariantMirror

NOT_AVAILABLE = "That detail is not available for this product."

# Words a customer (or the router) may use -> our spec name (config/specs_mapping.yaml).
SPEC_WORDS: dict[str, frozenset[str]] = {
    "case_diameter_mm": frozenset({"size", "diameter", "case", "dial", "mm"}),
    "thickness_mm": frozenset({"thickness", "thick", "thin", "motai"}),
    "water_resistance": frozenset({"water", "waterproof", "resistance", "resistant", "atm"}),
    "strap_width_mm": frozenset({"strap", "band", "belt", "width"}),
    "warranty": frozenset({"warranty", "waranty", "guarantee"}),
}


class SpecValue(BaseModel):
    name: str  # our spec name, e.g. 'water_resistance'
    value: str  # text as the merchant wrote it, e.g. '200 m'
    source: Literal["metafield", "option"]


class SpecsResult(BaseModel):
    product_id: str
    title: str
    specs: list[SpecValue]  # only values that really exist
    requested: str | None = None  # the one spec that was asked for, if any
    available: bool  # False: say 'not available' (message) and offer a human
    message: str = ""  # NOT_AVAILABLE when nothing was found


def resolve_spec(text: str) -> str | None:
    """'waterproof?' -> 'water_resistance'; 'case size' -> 'case_diameter_mm'.

    None when the words fit no spec, or fit several ('strap size'): then all specs are returned
    and the caller can pick, instead of answering about the wrong one.
    """
    normalized = normalize_text(text)
    canonical = normalized.replace(" ", "_")
    if canonical in SPEC_WORDS:
        return canonical
    words = set(normalized.split())
    matches = [name for name, vocabulary in SPEC_WORDS.items() if words & vocabulary]
    return matches[0] if len(matches) == 1 else None


def build_specs(
    product_id: str,
    title: str,
    specs: dict[str, str],
    variant_options: list[dict[str, str]],
    requested: str | None = None,
) -> SpecsResult:
    """Combine metafield specs and option sizes. A metafield beats an option for the same spec."""
    values = {
        name: SpecValue(name=name, value=value, source="metafield")
        for name, value in specs.items()
        if value
    }
    sizes = sorted(
        {
            mm
            for options in variant_options
            for name, value in options.items()
            if canonical_option_name(name) == "size" and (mm := parse_mm(value))
        }
    )
    if "case_diameter_mm" not in values and sizes:
        values["case_diameter_mm"] = SpecValue(
            name="case_diameter_mm",
            value=", ".join(f"{size} mm" for size in sizes),
            source="option",
        )
    if requested:
        chosen = [values[requested]] if requested in values else []
    else:
        chosen = list(values.values())
    return SpecsResult(
        product_id=product_id,
        title=title,
        specs=chosen,
        requested=requested,
        available=bool(chosen),
        message="" if chosen else NOT_AVAILABLE,
    )


async def get_specs(
    session: AsyncSession, product_id: str, spec: str | None = None
) -> SpecsResult | None:
    """Specs of one visible product. None if it does not exist or is hidden (like not found)."""
    product = (
        await session.execute(
            select(ProductMirror.id, ProductMirror.title, ProductMirror.specs).where(
                ProductMirror.id == product_id, ProductMirror.is_visible.is_(True)
            )
        )
    ).one_or_none()
    if product is None:
        return None
    options = (
        await session.execute(
            select(VariantMirror.options).where(VariantMirror.product_id == product_id)
        )
    ).scalars()
    return build_specs(
        product.id,
        product.title,
        product.specs or {},
        [o or {} for o in options],
        resolve_spec(spec) if spec else None,
    )

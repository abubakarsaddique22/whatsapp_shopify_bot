"""Catalogue models: what the Shopify gateway returns (Blueprint 4.4, 4.10).

Only customer-visible data lives here: a product that is draft, archived or not on the Online
Store never becomes a `Product`. Money is always `Decimal`, never float.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StockState(StrEnum):
    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    BACKORDER = "backorder"
    UNKNOWN = "unknown"  # Shopify error or ambiguous data: never guess
    COLOUR_NOT_OFFERED = "colour_not_offered"  # decided in Phase 3, never set by the mapper


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=3)  # ISO 4217, e.g. "PKR"


class Image(BaseModel):
    url: str
    alt: str = ""
    width: int | None = None
    height: int | None = None


class ProductOption(BaseModel):
    name: str  # free text from the merchant: "Colour", "Color", "Case size"
    values: list[str] = []


class Variant(BaseModel):
    id: str  # Shopify global id
    product_id: str
    title: str
    sku: str | None = None
    price: Money
    compare_at_price: Money | None = None
    options: dict[str, str] = {}  # selectedOptions as {name: value}
    available_for_sale: bool
    inventory_quantity: int | None = None
    stock: StockState
    image_url: str | None = None


class Product(BaseModel):
    id: str  # Shopify global id
    title: str
    handle: str
    url: str  # onlineStoreUrl, never empty for a visible product
    vendor: str = ""
    product_type: str = ""
    tags: list[str] = []
    description: str = ""  # clean text, untrusted (it is merchant-written)
    options: list[ProductOption] = []
    specs: dict[str, str] = {}  # our spec names (config/specs_mapping.yaml) -> value
    images: list[Image] = []
    variants: list[Variant] = []
    updated_at: datetime | None = None


class Policy(BaseModel):
    type: str  # REFUND_POLICY, SHIPPING_POLICY, PRIVACY_POLICY, TERMS_OF_SERVICE, ...
    title: str
    body: str  # clean text with headings kept
    url: str | None = None


class Page(BaseModel):
    id: str
    title: str
    handle: str
    body: str  # clean text with headings kept

"""Turn Shopify GraphQL answers into our catalogue models.

Customer-visible rule (Blueprint 4.5): only ACTIVE products that are on the Online Store
(onlineStoreUrl is not null) are mapped. Draft, archived and unpublished products return None, so
they behave exactly like products that do not exist. Money is always `Decimal`.
"""

import json
import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from app.core.errors import ConfigError, ShopifyError
from app.domain.catalog import (
    Image,
    Money,
    Page,
    Policy,
    Product,
    ProductOption,
    StockState,
    Variant,
)

log = logging.getLogger(__name__)

SPECS_MAPPING_PATH = Path(__file__).resolve().parents[4] / "config" / "specs_mapping.yaml"

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_HEADING = re.compile(r"^h[1-6]$")
_UNITS = {
    "MILLIMETERS": "mm",
    "CENTIMETERS": "cm",
    "METERS": "m",
    "INCHES": "in",
    "FEET": "ft",
    "GRAMS": "g",
    "KILOGRAMS": "kg",
    "MILLILITERS": "ml",
    "LITERS": "l",
}


# ---- config


@lru_cache
def load_specs_mapping(path: Path = SPECS_MAPPING_PATH) -> dict[str, str]:
    """{'case_diameter_mm': 'custom.case_diameter', ...} from config/specs_mapping.yaml."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path.name}") from exc
    return {str(name): str(key) for name, key in (data.get("specs") or {}).items()}


# ---- small helpers


def _nodes(connection: dict | None) -> list[dict]:
    return (connection or {}).get("nodes") or []


def is_visible(product: dict) -> bool:
    """ACTIVE and published to the Online Store. Works on a full product or the short form."""
    return product.get("status") == "ACTIVE" and bool(product.get("onlineStoreUrl"))


def money(value: object, currency: str) -> Money:
    try:
        return Money(amount=Decimal(str(value)), currency=currency)
    except ValueError as exc:  # also covers pydantic's ValidationError
        raise ShopifyError("Shopify returned a price that is not a valid amount") from exc
    except ArithmeticError as exc:  # decimal.InvalidOperation
        raise ShopifyError("Shopify returned a price that is not a valid amount") from exc


def html_to_text(html: str | None) -> str:
    """Clean text from merchant HTML: no scripts, styles or hidden parts; headings kept."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "iframe"]):
        tag.decompose()
    hidden = [
        t
        for t in soup.find_all(True)
        if t.has_attr("hidden")
        or t.get("aria-hidden") == "true"
        or _HIDDEN_STYLE.search(str(t.get("style") or ""))
    ]
    for tag in hidden:
        if not tag.decomposed:
            tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for h in soup.find_all(_HEADING):
        h.insert(0, "## ")
        h.insert_before("\n\n")
        h.append("\n\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for block in soup.find_all(["p", "div", "li", "tr", "ul", "ol", "table", "section"]):
        block.append("\n")
    text = soup.get_text().replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---- stock (Blueprint 4.10)


def classify_stock(
    *,
    available_for_sale: bool,
    quantity: int | None,
    tracked: bool,
    policy: str | None,
    low_stock_threshold: int,
) -> StockState:
    if not tracked:
        return StockState.IN_STOCK if available_for_sale else StockState.OUT_OF_STOCK
    if quantity is None:
        return StockState.UNKNOWN
    if quantity <= 0:
        if available_for_sale and policy == "CONTINUE":
            return StockState.BACKORDER
        return StockState.OUT_OF_STOCK if not available_for_sale else StockState.UNKNOWN
    if not available_for_sale:
        return StockState.UNKNOWN  # stock exists but Shopify says it cannot be sold: ambiguous
    return StockState.LOW_STOCK if quantity <= low_stock_threshold else StockState.IN_STOCK


# ---- specs


def _spec_value(metafield: dict) -> str:
    raw = str(metafield.get("value") or "").strip()
    if str(metafield.get("type") or "").startswith(("dimension", "weight", "volume")):
        try:  # measurement metafields are JSON: {"value": 40.0, "unit": "MILLIMETERS"}
            data = json.loads(raw)
            number = f"{Decimal(str(data['value'])).normalize():f}"
            unit = str(data["unit"])
            return f"{number} {_UNITS.get(unit, unit.lower())}"
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return raw
    return raw


def map_specs(metafields: list[dict], mapping: dict[str, str]) -> dict[str, str]:
    by_key = {f"{m.get('namespace')}.{m.get('key')}": m for m in metafields}
    specs: dict[str, str] = {}
    for spec_name, metafield_key in mapping.items():
        metafield = by_key.get(metafield_key)
        value = _spec_value(metafield) if metafield else ""
        if value:
            specs[spec_name] = value
    return specs


# ---- products and variants


def map_variant(
    node: dict, product_id: str, currency: str, *, low_stock_threshold: int
) -> Variant:
    try:
        quantity = node.get("inventoryQuantity")
        available = bool(node["availableForSale"])
        tracked = bool((node.get("inventoryItem") or {}).get("tracked", True))
        compare_at = node.get("compareAtPrice")
        return Variant(
            id=node["id"],
            product_id=product_id,
            title=node["title"],
            sku=node.get("sku") or None,
            price=money(node["price"], currency),
            compare_at_price=money(compare_at, currency) if compare_at else None,
            options={o["name"]: o["value"] for o in node.get("selectedOptions") or []},
            available_for_sale=available,
            inventory_quantity=quantity,
            stock=classify_stock(
                available_for_sale=available,
                quantity=quantity,
                tracked=tracked,
                policy=node.get("inventoryPolicy"),
                low_stock_threshold=low_stock_threshold,
            ),
            image_url=(node.get("image") or {}).get("url"),
        )
    except (KeyError, TypeError) as exc:
        raise ShopifyError("Unexpected variant shape in the Shopify response") from exc


def map_product(
    node: dict | None,
    currency: str,
    *,
    low_stock_threshold: int,
    specs_mapping: dict[str, str],
) -> Product | None:
    """Return a Product, or None if it is draft, archived or not on the Online Store."""
    if not node or not is_visible(node):
        return None
    try:
        variants_connection = node.get("variants") or {}
        if (variants_connection.get("pageInfo") or {}).get("hasNextPage"):
            log.warning("shopify_variants_truncated", extra={"product_id": node["id"]})
        images = [
            Image(
                url=m["image"]["url"],
                alt=m.get("alt") or "",
                width=m["image"].get("width"),
                height=m["image"].get("height"),
            )
            for m in _nodes(node.get("media"))
            if (m.get("image") or {}).get("url")  # skip videos and other media
        ]
        return Product(
            id=node["id"],
            title=node["title"],
            handle=node["handle"],
            url=node["onlineStoreUrl"],
            vendor=node.get("vendor") or "",
            product_type=node.get("productType") or "",
            tags=node.get("tags") or [],
            description=html_to_text(node.get("descriptionHtml")),
            options=[
                ProductOption(name=o["name"], values=o.get("values") or [])
                for o in node.get("options") or []
            ],
            specs=map_specs(_nodes(node.get("metafields")), specs_mapping),
            images=images,
            variants=[
                map_variant(v, node["id"], currency, low_stock_threshold=low_stock_threshold)
                for v in _nodes(variants_connection)
            ],
            updated_at=node.get("updatedAt"),
        )
    except (KeyError, TypeError) as exc:
        raise ShopifyError("Unexpected product shape in the Shopify response") from exc


def map_live_variant(
    node: dict | None, currency: str, *, low_stock_threshold: int
) -> Variant | None:
    """A variant is visible only when its product is visible."""
    if not node:
        return None
    product = node.get("product") or {}
    if not is_visible(product):
        return None
    try:
        product_id = product["id"]
    except KeyError as exc:
        raise ShopifyError("Unexpected variant shape in the Shopify response") from exc
    return map_variant(node, product_id, currency, low_stock_threshold=low_stock_threshold)


# ---- policies and pages


def map_policy(node: dict) -> Policy | None:
    body = html_to_text(node.get("body"))
    if not body:
        return None  # an empty policy is the same as a missing one
    return Policy(
        type=str(node.get("type") or ""),
        title=str(node.get("title") or ""),
        body=body,
        url=node.get("url") or None,
    )


def map_page(node: dict) -> Page | None:
    if not node.get("isPublished"):
        return None
    body = html_to_text(node.get("body"))
    if not body:
        return None
    try:
        return Page(id=node["id"], title=node["title"], handle=node["handle"], body=body)
    except KeyError as exc:
        raise ShopifyError("Unexpected page shape in the Shopify response") from exc

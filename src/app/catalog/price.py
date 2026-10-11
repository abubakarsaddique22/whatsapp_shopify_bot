"""Price formatting (Phase 3, step 3). Money is Decimal, never float (Blueprint 4.4).

'Rs 12,500' is the format the composer prompt asks for (Blueprint 8.3). Other currencies show
their ISO code: 'USD 120'. Whole amounts have no decimals; others show two: 'Rs 12,500.50'.
Thousands use western grouping (12,500 and 100,000), as in the blueprint.
"""

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from app.domain.catalog import Money

_CENTS = Decimal("0.01")
_PREFIX = {"PKR": "Rs"}


def format_amount(amount: Decimal) -> str:
    """Decimal('12500.00') -> '12,500'; Decimal('12500.5') -> '12,500.50'."""
    rounded = amount.quantize(_CENTS, rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        return f"{rounded:,.0f}"
    return f"{rounded:,.2f}"


def format_money(money: Money) -> str:
    code = money.currency.upper()
    return f"{_PREFIX.get(code, code)} {format_amount(money.amount)}"


class PriceInfo(BaseModel):
    """What a tool returns about one price. All values are text: JSON-safe, no float."""

    amount: str  # "12500.00"
    currency: str
    display: str  # "Rs 12,500"
    compare_at_display: str | None = None  # only when the old price is really higher
    on_sale: bool = False


def price_info(price: Money, compare_at: Money | None = None) -> PriceInfo:
    on_sale = (
        compare_at is not None
        and compare_at.currency == price.currency
        and compare_at.amount > price.amount
    )
    return PriceInfo(
        amount=str(price.amount.quantize(_CENTS, rounding=ROUND_HALF_UP)),
        currency=price.currency,
        display=format_money(price),
        compare_at_display=format_money(compare_at) if on_sale and compare_at else None,
        on_sale=on_sale,
    )

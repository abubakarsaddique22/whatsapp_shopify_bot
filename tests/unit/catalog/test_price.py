"""Price formatting (Phase 3, step 3)."""

from decimal import Decimal

import pytest

from app.catalog.price import format_amount, format_money, price_info
from app.domain.catalog import Money


def money(amount: str, currency: str = "PKR") -> Money:
    return Money(amount=Decimal(amount), currency=currency)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("12500", "PKR", "Rs 12,500"),
        ("12500.00", "PKR", "Rs 12,500"),
        ("8999.00", "PKR", "Rs 8,999"),
        ("100000", "PKR", "Rs 100,000"),
        ("12500.5", "PKR", "Rs 12,500.50"),
        ("12500.005", "PKR", "Rs 12,500.01"),  # rounds half up, never float
        ("0", "PKR", "Rs 0"),
        ("120", "USD", "USD 120"),
        ("120.5", "usd", "USD 120.50"),
    ],
)
def test_format_money(amount: str, currency: str, expected: str):
    assert format_money(money(amount, currency)) == expected


def test_format_amount_has_no_float_noise():
    assert format_amount(Decimal("0.1") + Decimal("0.2")) == "0.30"


def test_price_info_plain_price():
    info = price_info(money("18500.00"))
    assert info.amount == "18500.00"
    assert info.currency == "PKR"
    assert info.display == "Rs 18,500"
    assert info.on_sale is False
    assert info.compare_at_display is None


def test_price_info_sale_when_compare_at_is_higher():
    info = price_info(money("18500"), money("24000"))
    assert info.on_sale is True
    assert info.compare_at_display == "Rs 24,000"


@pytest.mark.parametrize("compare_at", ["18500", "9000"])
def test_price_info_no_sale_when_compare_at_is_not_higher(compare_at: str):
    info = price_info(money("18500"), money(compare_at))
    assert info.on_sale is False
    assert info.compare_at_display is None


def test_price_info_ignores_compare_at_in_another_currency():
    assert price_info(money("100", "PKR"), money("200", "USD")).on_sale is False


def test_price_info_dumps_to_plain_json_types():
    data = price_info(money("12500"), money("15000")).model_dump(mode="json")
    assert data == {
        "amount": "12500.00",
        "currency": "PKR",
        "display": "Rs 12,500",
        "compare_at_display": "Rs 15,000",
        "on_sale": True,
    }

"""Tests for the exact-fact evaluators (Phase 12, Step 2). No network, no model."""

import pytest

from app.evals.evaluators import (
    amounts_in,
    check_price,
    check_row,
    check_stock,
    check_values,
    stock_state_of,
)

# ---- price


@pytest.mark.parametrize(
    "reply",
    [
        "The AP Branded Premium Box costs Rs 3,000.",
        "Price: PKR 3000.00",
        "3000 rupay mein mil jayegi",
        "Rs. 3,000, free shipping above Rs 10,000",
        "The price is 3,000.",  # no Rs: any number counts
    ],
)
def test_price_ok(reply: str) -> None:
    assert check_price(reply, {"amount": "3000.00", "currency": "PKR"}).ok


def test_price_wrong_amount_fails() -> None:
    result = check_price("It costs Rs 3,500.", {"amount": "3000.00"})
    assert not result.ok
    assert "3000" in result.reason


def test_price_without_number_fails() -> None:
    assert not check_price("Please check our website.", {"amount": "3000.00"}).ok


def test_price_ignores_digits_in_the_product_name() -> None:
    # Expected 2100, but the reply says Rs 21,000: the '2100' inside 'GA-2100' must not count.
    assert not check_price("GA-2100 ki price Rs 21,000 hai", {"amount": "2100.00"}).ok
    assert check_price("GA-2100 ki price Rs 21,000 hai", {"amount": "21000.00"}).ok


def test_amounts_in_decimal_amount() -> None:
    assert [str(a) for a in amounts_in("Rs 12,500.50")] == ["12500.50"]


# ---- stock


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        ("Available.", "in_stock"),
        ("Jee, available hai.", "in_stock"),
        ("Yes, it is in stock.", "in_stock"),
        ("Currently out of stock.", "out_of_stock"),
        ("Sorry, this is not available right now.", "out_of_stock"),
        ("Ye watch available nahi hai.", "out_of_stock"),
        ("Stock khatam ho gaya hai.", "out_of_stock"),
        ("Limited stock.", "low_stock"),
        ("Can be ordered, but delivery may take longer.", "backorder"),
        ("I cannot check stock right now.", "unknown"),
    ],
)
def test_stock_state_of(reply: str, state: str) -> None:
    assert stock_state_of(reply) == state


def test_stock_check() -> None:
    assert check_stock("Available.", {"stock": "in_stock"}).ok
    result = check_stock("Currently out of stock.", {"stock": "in_stock"})
    assert not result.ok
    assert "out_of_stock" in result.reason


# ---- colours and sizes


def test_values_all_present() -> None:
    expected = {"values": ["Black", "Blue", "Red"]}
    assert check_values("It comes in Black, Blue and Red.", expected).ok


def test_values_missing_one_fails() -> None:
    result = check_values("Black and Blue only.", {"values": ["Black", "Blue", "Red"]})
    assert not result.ok
    assert "Red" in result.reason


def test_values_forbidden_colour_fails() -> None:
    expected = {"values": ["Black", "Blue"]}
    result = check_values("Black, Blue and Green.", expected, forbidden=["Green", "Red"])
    assert not result.ok
    assert "Green" in result.reason


def test_values_forbidden_list_may_contain_an_own_value() -> None:
    assert check_values("Black.", {"values": ["Black"]}, forbidden=["Black", "Red"]).ok


def test_values_match_whole_words_only() -> None:
    # 'red' is inside 'colored' but that is not the colour red
    assert not check_values("A colored strap.", {"values": ["Red"]}).ok


def test_values_sizes_with_digits() -> None:
    assert check_values("Available in 40mm and 42mm.", {"values": ["40mm", "42mm"]}).ok


# ---- one row


def test_check_row_uses_the_kind() -> None:
    row = {"kind": "price", "expected": {"amount": "3000.00", "currency": "PKR"}}
    assert check_row(row, "Rs 3,000").ok
    assert not check_row(row, "Rs 4,000").ok
    stock = {"kind": "stock", "expected": {"stock": "in_stock"}}
    assert check_row(stock, "Available.").ok
    colours = {"kind": "colors", "expected": {"values": ["Black"]}}
    assert check_row(colours, "Black", forbidden=["Red"]).ok


def test_check_row_unknown_kind_fails() -> None:
    result = check_row({"kind": "weather", "expected": {}}, "sunny")
    assert not result.ok
    assert "unknown kind" in result.reason

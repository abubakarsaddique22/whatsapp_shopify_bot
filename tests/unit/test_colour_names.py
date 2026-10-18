"""Colours the way the shop names them (Phase 12): the tool field, the validator, the evaluator."""

from datetime import UTC, datetime

from app.agent.tools import TOOL_OPTIONS, _colour_names
from app.agent.validator import _build_facts
from app.domain.models import ToolResult
from app.evals.evaluators import check_values


def test_colour_names_keeps_the_shops_own_values_once_in_order() -> None:
    options = [
        {"Color": "Orange-Black", "Case size": "40mm"},
        {"Color": "Green"},
        {"Colour": "orange-black"},  # the same value again, other capitals: once
        {"Title": "Default Title"},  # not a colour option
        {"Color": "  "},
    ]
    assert _colour_names(options) == ["Orange-Black", "Green"]


def test_colour_names_of_a_product_without_colour_option_is_empty() -> None:
    assert _colour_names([{"Title": "Default Title"}, {}]) == []


def test_the_validator_accepts_the_colours_inside_the_shops_names() -> None:
    result = ToolResult(
        tool=TOOL_OPTIONS,
        ok=True,
        data={"colours": ["black"], "colour_names": ["Gold & Silver"]},
        source_id="x:1",
        fetched_at=datetime.now(UTC),
    )
    facts = _build_facts([result])
    assert {"black", "gold", "silver"} <= facts.colours


def test_evaluator_accepts_a_pair_written_another_way() -> None:
    expected = {"values": ["BLACK&ROSEGOLD", "GOLD&SILVER", "Orange-Black"]}
    reply = "It comes in Black & Rose Gold, Gold / Silver and Orange - Black."
    assert check_values(reply, expected).ok


def test_evaluator_still_misses_a_dropped_part() -> None:
    # the old bot answer: only the short colours
    expected = {"values": ["BLACK&ROSEGOLD", "Orange-Black"]}
    result = check_values("Available in black and gold.", expected)
    assert not result.ok
    assert "BLACK&ROSEGOLD" in result.reason
    assert "Orange-Black" in result.reason


def test_one_word_values_are_not_found_inside_other_words() -> None:
    assert not check_values("A colored strap.", {"values": ["Red"]}).ok

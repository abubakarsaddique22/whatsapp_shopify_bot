"""Specs and size answers without a database (Phase 3, step 6; T04)."""

import pytest

from app.catalog.specs import NOT_AVAILABLE, build_specs, resolve_spec


def build(specs=None, options=None, requested=None):
    return build_specs("p1", "Casio GA-2100", specs or {}, options or [], requested)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("waterproof?", "water_resistance"),
        ("water resistance", "water_resistance"),
        ("case size", "case_diameter_mm"),
        ("diameter", "case_diameter_mm"),
        ("motai", "thickness_mm"),
        ("strap width", "strap_width_mm"),
        ("warranty", "warranty"),
        ("case_diameter_mm", "case_diameter_mm"),  # our own spec name works too
        ("strap size", None),  # fits two specs: do not guess
        ("colour", None),
        ("", None),
    ],
)
def test_resolve_spec(text: str, expected: str | None):
    assert resolve_spec(text) == expected


def test_t04_metafield_value_is_returned_as_the_merchant_wrote_it():
    result = build(specs={"water_resistance": "200 m"}, requested="water_resistance")
    assert result.available
    assert [(s.name, s.value, s.source) for s in result.specs] == [
        ("water_resistance", "200 m", "metafield")
    ]
    assert result.message == ""


def test_t04_case_size_comes_from_the_options_when_there_is_no_metafield():
    options = [{"Colour": "Black", "Case size": "40mm"}, {"Colour": "Blue", "Case size": "44mm"}]
    result = build(options=options, requested="case_diameter_mm")
    assert result.available
    assert result.specs[0].value == "40 mm, 44 mm"
    assert result.specs[0].source == "option"


def test_a_metafield_beats_the_options_for_the_same_spec():
    result = build(
        specs={"case_diameter_mm": "42 mm"},
        options=[{"Case size": "40mm"}],
        requested="case_diameter_mm",
    )
    assert result.specs[0].value == "42 mm"
    assert result.specs[0].source == "metafield"


def test_t04_absent_value_is_not_available_never_guessed():
    result = build(specs={"warranty": "2 years"}, requested="water_resistance")
    assert not result.available
    assert result.specs == []
    assert result.message == NOT_AVAILABLE


def test_t04_nothing_at_all_is_not_available():
    result = build()
    assert not result.available
    assert result.message == NOT_AVAILABLE


def test_no_requested_spec_returns_everything_that_exists():
    result = build(
        specs={"water_resistance": "200 m", "warranty": ""},  # empty value is absent
        options=[{"Case size": "44mm"}],
    )
    assert {s.name for s in result.specs} == {"water_resistance", "case_diameter_mm"}


def test_strap_size_option_is_not_a_case_size():
    result = build(options=[{"Strap size": "20mm"}], requested="case_diameter_mm")
    assert not result.available

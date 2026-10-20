"""Normalizer: spellings, aliases, option names, colours, case sizes (Phase 3, step 2)."""

import pytest

from app.catalog.normalizer import (
    brand_variants,
    canonical_option_name,
    colour_of_value,
    colours_of,
    compact,
    expand_aliases,
    is_default_title,
    known_colour,
    normalize_text,
    parse_mm,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Men's Classic Chronograph Watch", "mens classic chronograph watch"),
        ("GA-2100", "ga 2100"),
        ("  G-Shock!!  ", "g shock"),
        ("Casio   G-Shock   GA-110", "casio g shock ga 110"),
        ("", ""),
    ],
)
def test_normalize_text(text: str, expected: str):
    assert normalize_text(text) == expected


def test_compact_makes_spaced_and_glued_spellings_equal():
    assert compact("GA-2100") == compact("ga 2100") == compact("ga2100") == "ga2100"


def test_expand_aliases_with_the_real_config():
    # config/aliases.yaml: g-shock: [gshock, g shock]
    assert expand_aliases("gshock ga2100") == "g shock ga2100"
    assert expand_aliases("G-Shock GA-110") == "g shock ga 110"


def test_brand_short_forms_from_the_real_config():
    # config/aliases.yaml brands (client decision 2026-10-20): RLX = Rolex, AP = Audemars Piguet
    assert brand_variants("rolex submariner") == ["rolex submariner", "rlx submariner"]
    assert brand_variants("rlx submariner") == ["rlx submariner", "rolex submariner"]
    assert "audemars piguet royal oak" in brand_variants("ap royal oak")
    assert brand_variants("casio ga 2100") == ["casio ga 2100"]  # no brand short form


def test_brand_short_forms_replace_whole_words_only(tmp_path):
    config = tmp_path / "aliases.yaml"
    config.write_text("brands:\n  rolex: [rlx]\n  omega: [omga]\n", encoding="utf-8")
    assert brand_variants("rlxx submariner", config) == ["rlxx submariner"]
    both = brand_variants("rolex or omega", config)
    assert both[0] == "rolex or omega"
    assert "rlx or omga" in both  # the two brands combine
    assert len(both) <= 4


def test_expand_aliases_replaces_whole_words_only(tmp_path):
    config = tmp_path / "aliases.yaml"
    config.write_text("aliases:\n  casio: [kasio, casio watch]\n", encoding="utf-8")
    assert expand_aliases("kasio kasio black", config) == "casio casio black"
    assert expand_aliases("kasiox black", config) == "kasiox black"  # not a whole word


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Colour", "colour"),
        ("Color", "colour"),
        ("Dial Colour", "colour"),
        ("Case size", "size"),
        ("Size", "size"),
        ("Case Diameter", "size"),
        ("Strap size", "strap size"),  # a strap is not the case
        ("Title", "title"),
        ("Material", "material"),
    ],
)
def test_canonical_option_name(name: str, expected: str):
    assert canonical_option_name(name) == expected


def test_default_title_option_is_detected():
    assert is_default_title("Title", "Default Title")
    assert not is_default_title("Colour", "Black")
    assert not is_default_title("Title", "Gold Edition")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Black", "black"),
        ("kala", "black"),
        ("Matte Black", "black"),
        ("Rose Gold", "gold"),
        ("Navy", "navy"),  # not in the lexicon: keeps its own name
        ("", None),
    ],
)
def test_colour_of_value(value: str, expected: str | None):
    assert colour_of_value(value) == expected


def test_known_colour_only_for_real_colour_words():
    assert known_colour("neela") == "blue"
    assert known_colour("Safed") == "white"
    assert known_colour("watch") is None


def test_colours_of_reads_only_the_colour_option():
    assert colours_of({"Colour": "Black", "Case size": "40mm"}) == ["black"]
    assert colours_of({"Title": "Default Title"}) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("40mm", 40),
        ("44 MM", 44),
        ("Case 38.5mm", 38),
        ("5mm", None),  # one digit: not a case size
        ("100mm", None),  # out of range
        ("no size here", None),
        ("ga2100mm", None),  # glued to a model code
    ],
)
def test_parse_mm(text: str, expected: int | None):
    assert parse_mm(text) == expected

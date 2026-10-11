"""Search helpers that need no database (Phase 3, step 5): query parsing, filters, decision."""

from decimal import Decimal

import pytest

from app.catalog.search import (
    Candidate,
    ColourFit,
    MatchKind,
    VariantRow,
    classify,
    colours_in_text,
    name_colour_fit,
    parse_query,
    variant_matches,
    variant_size_mm,
)


def variant(**changes) -> VariantRow:
    base = {
        "id": "v1",
        "title": "Black / 40mm",
        "options": {"Colour": "Black", "Case size": "40mm"},
        "price": Decimal("9500"),
    }
    return VariantRow(**{**base, **changes})


def candidate(score: float, title: str = "Watch") -> Candidate:
    return Candidate(
        product_id=title, title=title, url="u", vendor="v", score=score, variant_ids=[], colours=[]
    )


# ---- parse_query


@pytest.mark.parametrize(
    ("query", "words", "colour"),
    [
        ("gshock GA-2100 kala", "g shock ga 2100", "black"),
        ("black casio watch", "casio watch", "black"),
        ("GA-2100", "ga 2100", None),
        ("neela", "", "blue"),  # only a colour: no product words left
        ("", "", None),
    ],
)
def test_parse_query(query: str, words: str, colour: str | None):
    assert parse_query(query) == (words, colour)


# ---- variant filters


def test_size_from_option_then_variant_title_then_product_title():
    assert variant_size_mm(variant()) == 40
    assert variant_size_mm(variant(options={"Colour": "Black"}, title="Black 44mm")) == 44
    plain = variant(options={"Colour": "Black"}, title="Black")
    assert variant_size_mm(plain, "GA-2100 42mm") == 42
    assert variant_size_mm(plain, "GA-2100") is None


def test_strap_size_is_not_the_case_size():
    strap = variant(options={"Strap size": "20mm"}, title="Default")
    assert variant_size_mm(strap) is None


def test_no_filters_means_every_variant_matches():
    assert variant_matches(variant(), colour=None, size_mm=None, max_price=None)


def test_colour_filter_uses_the_canonical_colour():
    assert variant_matches(variant(), colour="black", size_mm=None, max_price=None)
    assert not variant_matches(variant(), colour="blue", size_mm=None, max_price=None)


def test_size_filter():
    assert variant_matches(variant(), colour=None, size_mm=40, max_price=None)
    assert not variant_matches(variant(), colour=None, size_mm=44, max_price=None)


def test_price_ceiling_is_inclusive():
    assert variant_matches(variant(), colour=None, size_mm=None, max_price=9500)
    assert not variant_matches(variant(), colour=None, size_mm=None, max_price=9499)


def test_all_filters_must_hold_for_the_same_variant():
    # black exists, 44mm exists, but not in the same variant
    black_40 = variant(id="a", options={"Colour": "Black", "Case size": "40mm"})
    blue_44 = variant(id="b", options={"Colour": "Blue", "Case size": "44mm"})
    for v in (black_40, blue_44):
        assert not variant_matches(v, colour="black", size_mm=44, max_price=None)


# ---- decision (T13 / T14 logic)


def test_nothing_found_is_none():
    assert classify([]) == MatchKind.NONE


def test_one_clear_match():
    assert classify([candidate(0.9)]) == MatchKind.ONE


def test_clear_leader_among_several_is_one():
    assert classify([candidate(1.0, "A"), candidate(0.6, "B")]) == MatchKind.ONE


def test_close_matches_are_ambiguous():
    assert classify([candidate(1.0, "A"), candidate(0.95, "B")]) == MatchKind.AMBIGUOUS


def test_a_weak_single_match_is_not_trusted():
    assert classify([candidate(0.4)]) == MatchKind.AMBIGUOUS


# ---- colour for products that have no colour option (colour only in the name, or nowhere)


def test_colours_in_text_reads_colour_words_from_names_and_tags():
    assert colours_in_text("Casio Black Resin") == {"black"}
    assert colours_in_text("Rose Gold Edition kala") == {"gold", "black"}
    assert colours_in_text("Men's Urban Street Watch") == set()


def test_name_that_says_the_colour_confirms_it():
    assert name_colour_fit("black", "Casio Black Watch") == ColourFit.CONFIRMED


def test_name_that_says_another_colour_excludes_the_product():
    assert name_colour_fit("black", "Casio Blue Watch") == ColourFit.EXCLUDED


def test_name_without_any_colour_is_unverified_not_hidden():
    assert name_colour_fit("black", "Men's Urban Street Watch") == ColourFit.UNVERIFIED

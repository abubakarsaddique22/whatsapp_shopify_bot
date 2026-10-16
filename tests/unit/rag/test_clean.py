"""Phase 8, step 2: clean_text and content_hash."""

from app.rag.clean import clean_text, content_hash


def test_empty_input_gives_empty_text() -> None:
    assert clean_text(None) == ""
    assert clean_text("") == ""
    assert clean_text("  \n\n ") == ""


def test_spaces_and_no_break_spaces_become_one_space() -> None:
    assert clean_text("Refund\u00a0 within   14 days") == "Refund within 14 days"


def test_line_breaks_are_unified_and_blank_lines_limited() -> None:
    assert clean_text("a\r\nb\rc\u2028d\n\n\n\n\ne") == "a\nb\nc\nd\n\ne"


def test_spaces_at_the_ends_of_lines_are_removed() -> None:
    assert clean_text("  one  \n\ttwo\t ") == "one\ntwo"


def test_invisible_characters_are_dropped_but_urdu_joiners_stay() -> None:
    assert clean_text("Refund\u200bpolicy\ufeff\u00ad") == "Refundpolicy"
    assert clean_text("a\u200cb") == "a\u200cb"  # zero-width non-joiner is part of Urdu spelling


def test_control_characters_are_dropped() -> None:
    assert clean_text("a\x00b\x07c") == "abc"


def test_full_width_digits_become_normal_digits() -> None:
    assert clean_text("\uff11\uff14 days") == "14 days"


def test_html_still_in_the_text_is_converted_with_the_shared_cleaner() -> None:
    text = clean_text("<h2>Returns</h2><p>14 days</p><script>alert(1)</script>")
    assert "## Returns" in text
    assert "14 days" in text
    assert "<" not in text
    assert "alert" not in text


def test_a_less_than_sign_is_not_mistaken_for_html() -> None:
    assert clean_text("Refund if it takes <5 days") == "Refund if it takes <5 days"


def test_headings_from_the_mapper_are_kept() -> None:
    assert clean_text("## Returns\n\nWithin 14 days.") == "## Returns\n\nWithin 14 days."


def test_cleaning_twice_changes_nothing() -> None:
    once = clean_text("<p>Refund\u00a0 in   14 days</p>\r\n\r\n\r\n\r\nShipping is free\u200b.")
    assert clean_text(once) == once


def test_hash_is_64_hex_characters_and_follows_the_text() -> None:
    first = content_hash("refund in 14 days")
    assert len(first) == 64
    assert int(first, 16) >= 0
    assert first == content_hash("refund in 14 days")
    assert first != content_hash("refund in 15 days")

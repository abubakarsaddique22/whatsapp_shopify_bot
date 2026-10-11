"""Lexicon loader: config/roman_urdu_lexicon.yaml (Phase 3, step 1)."""

import pytest

from app.core.errors import ConfigError
from app.language.lexicon import get_lexicon


def test_real_lexicon_loads():
    lexicon = get_lexicon()
    assert "kya" in lexicon.roman_urdu_words
    assert lexicon.numbers["lakh"] == 100_000


def test_on_is_a_word_not_a_boolean():
    # YAML reads a bare `on` as True. The file quotes it, so it must arrive as the text "on".
    assert "on" in get_lexicon().english_stopwords


def test_colour_words_map_to_one_colour():
    lexicon = get_lexicon()
    assert lexicon.colour_for("kala") == "black"
    assert lexicon.colour_for("Black") == "black"
    assert lexicon.colour_for("neela") == "blue"
    assert lexicon.colour_for("watch") is None


def test_boolean_word_in_yaml_gives_clear_error(tmp_path):
    bad = tmp_path / "lexicon.yaml"
    bad.write_text("english_stopwords: [the, on]\n", encoding="utf-8")  # bare `on` = True
    with pytest.raises(ConfigError):
        get_lexicon(bad)

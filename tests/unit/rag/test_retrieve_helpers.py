"""Phase 8, step 5: the pure helpers of rag/retrieve.py (no database needed)."""

import pytest

from app.rag.retrieve import (
    MAX_FTS_WORDS,
    MAX_PASSAGES,
    MAX_QUERY_CHARS,
    Passage,
    allowed_doc_types,
    clean_queries,
    select_passages,
    tsquery_text,
)


def make_passage(chunk_id: int, score: float) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id=1,
        doc_type="policy_refund",
        heading_path="Refund policy > Returns",
        text="Within 7 days.",
        source_url=None,
        score=score,
        rrf=0.01,
    )


# ---- doc types


def test_no_intent_searches_every_type():
    assert allowed_doc_types(None) is None
    assert allowed_doc_types("") is None


def test_a_policy_intent_also_searches_the_faq():
    assert allowed_doc_types("policy_refund") == ["policy_refund", "faq"]


def test_a_faq_intent_searches_only_the_faq():
    assert allowed_doc_types("faq") == ["faq"]


# ---- query texts


def test_clean_queries_drops_empty_and_repeated_texts_and_tidies_spaces():
    queries = [None, "  Refund   policy? ", "refund policy?", "", "   "]
    assert clean_queries(queries) == ["Refund policy?"]


def test_clean_queries_keeps_the_order_of_different_texts():
    assert clean_queries(["refund policy", "refund kitne din?"]) == [
        "refund policy",
        "refund kitne din?",
    ]


def test_clean_queries_cuts_a_very_long_text():
    assert len(clean_queries(["a" * 600])[0]) == MAX_QUERY_CHARS


def test_clean_queries_of_nothing_is_empty():
    assert clean_queries([None, None]) == []


# ---- the full-text query


def test_tsquery_joins_the_words_with_or():
    assert tsquery_text("refund days?") == "refund | days"


def test_tsquery_drops_repeated_words():
    assert tsquery_text("refund Refund REFUND days") == "refund | days"


def test_tsquery_lets_no_tsquery_syntax_through():
    assert tsquery_text("a & b | (c) !d' ; --") == "a | b | c | d"


def test_tsquery_splits_on_underscore():
    assert tsquery_text("foo_bar") == "foo | bar"


def test_tsquery_keeps_urdu_script_words():
    assert tsquery_text("کتنے دن") == "کتنے | دن"


def test_tsquery_of_only_punctuation_is_empty():
    assert tsquery_text("!!! ???") == ""


def test_tsquery_is_cut_to_the_word_limit():
    words = " ".join(f"word{n}" for n in range(MAX_FTS_WORDS + 10))
    assert len(tsquery_text(words).split(" | ")) == MAX_FTS_WORDS


# ---- the threshold


def test_select_passages_drops_everything_under_the_threshold():
    found = [make_passage(1, 0.9), make_passage(2, 0.5), make_passage(3, 0.2)]
    assert [p.chunk_id for p in select_passages(found, 0.5)] == [1, 2]  # 0.5 itself stays


def test_select_passages_keeps_the_order_it_was_given():
    found = [make_passage(3, 0.6), make_passage(1, 0.9)]
    assert [p.chunk_id for p in select_passages(found, 0.0)] == [3, 1]  # RRF order, not score


def test_select_passages_stops_at_the_limit():
    found = [make_passage(n, 0.9) for n in range(10)]
    assert len(select_passages(found, 0.0)) == MAX_PASSAGES
    assert len(select_passages(found, 0.0, limit=2)) == 2


def test_nothing_above_the_threshold_gives_an_empty_list():
    assert select_passages([make_passage(1, 0.1)], 0.5) == []
    assert select_passages([], 0.0) == []


# ---- the source id


@pytest.mark.parametrize("chunk_id", [1, 42])
def test_source_id_names_the_chunk(chunk_id):
    assert make_passage(chunk_id, 0.5).source_id == f"kb_chunk:{chunk_id}"

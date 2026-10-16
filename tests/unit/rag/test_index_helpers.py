"""Phase 8, step 3: the pure helpers of rag/index.py (no database needed)."""

import pytest

from app.core.errors import EmbeddingError
from app.integrations.embeddings.base import EMBEDDING_DIM
from app.rag.index import check_vectors, embedding_text, vector_literal


def good_vector(value: float = 0.5) -> list[float]:
    return [value] * EMBEDDING_DIM


def test_embedding_text_puts_the_heading_before_the_text():
    assert embedding_text("Refund policy > Returns", "Within 7 days.") == (
        "Refund policy > Returns\nWithin 7 days."
    )


def test_embedding_text_without_a_heading_is_just_the_text():
    assert embedding_text(None, "Within 7 days.") == "Within 7 days."


def test_vector_literal_is_the_text_form_pgvector_reads():
    assert vector_literal([0.5, -1.0, 2]) == "[0.5,-1.0,2.0]"


def test_check_vectors_accepts_one_good_vector_per_text():
    check_vectors([good_vector(), good_vector(1.0)], 2)


def test_check_vectors_rejects_a_wrong_count():
    with pytest.raises(EmbeddingError):
        check_vectors([good_vector()], 2)


def test_check_vectors_rejects_a_wrong_vector_size():
    with pytest.raises(EmbeddingError):
        check_vectors([[0.1, 0.2, 0.3]], 1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_check_vectors_rejects_nan_and_infinity(bad):
    vector = good_vector()
    vector[10] = bad
    with pytest.raises(EmbeddingError):
        check_vectors([vector], 1)

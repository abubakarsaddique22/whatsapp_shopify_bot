"""Phase 8: the sentence-transformers adapter and its place in the factory. No download, no GPU.

The real model is replaced by a tiny fake one (monkeypatching `_load_model`), so every test shows
what the adapter sends to the model and what it does with the answer.
"""

import math

import pytest

from app.core.errors import ConfigError, EmbeddingError
from app.core.settings import Settings
from app.integrations.embeddings import sentence_transformer_embedder as st
from app.integrations.embeddings.base import EMBEDDING_DIM
from app.integrations.embeddings.factory import get_embedder, get_eval_embedder
from app.integrations.embeddings.sentence_transformer_embedder import (
    DEFAULT_MODEL,
    SentenceTransformerEmbedder,
    pad_vector,
)


class FakeModel:
    """Gives the vector [len(text), 1, 0, ...] scaled to length 1, like a normalized model."""

    def __init__(self, size: int = 4) -> None:
        self.size = size
        self.calls: list[dict] = []

    def encode(self, texts, **options):
        self.calls.append({"texts": list(texts), **options})
        rows = []
        for text in texts:
            row = [float(len(text)), 1.0] + [0.0] * (self.size - 2)
            norm = math.sqrt(sum(v * v for v in row))
            rows.append([v / norm for v in row])
        return rows


def _use(monkeypatch: pytest.MonkeyPatch, model: object) -> None:
    monkeypatch.setattr(st, "_load_model", lambda name: model)


# ---- pad_vector


def test_pad_vector_adds_zeros_up_to_the_column_size():
    padded = pad_vector([0.5, 0.5])
    assert len(padded) == EMBEDDING_DIM
    assert padded[:2] == [0.5, 0.5]
    assert set(padded[2:]) == {0.0}


def test_pad_vector_keeps_a_vector_that_is_already_full():
    full = [0.1] * EMBEDDING_DIM
    assert pad_vector(full) == full


def test_pad_vector_refuses_a_vector_that_is_too_long():
    with pytest.raises(EmbeddingError, match="migration"):
        pad_vector([0.1] * (EMBEDDING_DIM + 1))


# ---- the adapter


def test_the_model_name_marks_the_provider():
    embedder = SentenceTransformerEmbedder("BAAI/bge-m3")
    assert embedder.model == "st:BAAI/bge-m3"
    assert (embedder.name, embedder.dim) == ("sentence-transformers", EMBEDDING_DIM)


async def test_vectors_have_the_column_size_and_keep_length_one(monkeypatch):
    _use(monkeypatch, FakeModel())

    [vector] = await SentenceTransformerEmbedder().embed(["refund policy"])

    assert len(vector) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
    assert set(vector[4:]) == {0.0}  # the fake model gives 4 numbers, the rest is padding


async def test_the_model_is_asked_for_normalized_vectors(monkeypatch):
    model = FakeModel()
    _use(monkeypatch, model)

    await SentenceTransformerEmbedder().embed(["refund policy"])

    [call] = model.calls
    assert call["normalize_embeddings"] is True
    assert call["show_progress_bar"] is False


async def test_the_order_of_the_vectors_matches_the_order_of_the_texts(monkeypatch):
    _use(monkeypatch, FakeModel())

    [short, long] = await SentenceTransformerEmbedder().embed(["ab", "abcdefghij"])
    [again] = await SentenceTransformerEmbedder().embed(["ab"])

    assert short == again
    assert short != long


async def test_an_empty_list_does_not_even_load_the_model(monkeypatch):
    def must_not_load(name: str):
        raise AssertionError("the model was loaded")

    monkeypatch.setattr(st, "_load_model", must_not_load)

    assert await SentenceTransformerEmbedder().embed([]) == []


async def test_a_model_with_too_many_numbers_is_refused(monkeypatch):
    _use(monkeypatch, FakeModel(size=EMBEDDING_DIM + 1))
    with pytest.raises(EmbeddingError, match="migration"):
        await SentenceTransformerEmbedder().embed(["refund"])


async def test_a_wrong_number_of_vectors_is_an_error(monkeypatch):
    class Short(FakeModel):
        def encode(self, texts, **options):
            return super().encode(texts[:1], **options)

    _use(monkeypatch, Short())
    with pytest.raises(EmbeddingError, match="1 vectors for 2 texts"):
        await SentenceTransformerEmbedder().embed(["a", "b"])


async def test_a_model_that_fails_gives_a_clear_error_without_the_texts(monkeypatch):
    class Broken:
        def encode(self, texts, **options):
            raise RuntimeError("out of memory while embedding secret customer text")

    _use(monkeypatch, Broken())

    with pytest.raises(EmbeddingError) as caught:
        await SentenceTransformerEmbedder().embed(["secret customer text"])

    assert "EMBEDDING_MODEL" in caught.value.message
    assert "secret customer text" not in caught.value.message


async def test_a_model_that_cannot_be_loaded_gives_the_same_clear_error(monkeypatch):
    def cannot_load(name: str):
        raise OSError("no such model on Hugging Face")

    monkeypatch.setattr(st, "_load_model", cannot_load)

    with pytest.raises(EmbeddingError, match="failed to load or run"):
        await SentenceTransformerEmbedder("not/a-model").embed(["refund"])


# ---- the factory


def _settings(provider: str, model: str = "") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        embedding_provider=provider,
        embedding_model=model,
    )


def test_factory_builds_the_sentence_transformer_embedder_with_the_default_model():
    embedder = get_embedder(_settings("sentence-transformers"))
    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.model == f"st:{DEFAULT_MODEL}"


def test_factory_uses_the_model_named_in_the_settings():
    embedder = get_embedder(_settings("sentence-transformers", "intfloat/multilingual-e5-base"))
    assert embedder.model == "st:intfloat/multilingual-e5-base"


def test_factory_accepts_an_underscore_and_any_case():
    embedder = get_embedder(_settings(" Sentence_Transformers "))
    assert isinstance(embedder, SentenceTransformerEmbedder)


@pytest.mark.parametrize("provider", ["gemini", "ollama"])
def test_gemini_and_ollama_are_not_embedding_providers_any_more(provider):
    with pytest.raises(ConfigError, match="must be 'fake' or 'sentence-transformers'"):
        get_embedder(_settings(provider))


def test_the_eval_embedder_is_local_whatever_the_provider_says():
    embedder = get_eval_embedder(_settings("fake", "BAAI/bge-m3"))
    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.model == "st:BAAI/bge-m3"

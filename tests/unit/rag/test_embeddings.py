"""Phase 8, step 1: the embedding adapter (fake embedder and factory)."""

import math

import pytest

from app.core.errors import ConfigError
from app.core.settings import Settings
from app.integrations.embeddings.base import EMBEDDING_DIM
from app.integrations.embeddings.factory import get_embedder
from app.integrations.embeddings.fake_embedder import FakeEmbedder


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors have length 1


def _settings(provider: str) -> Settings:
    return Settings(_env_file=None, embedding_provider=provider)  # type: ignore[call-arg]


async def test_vector_has_the_column_size_and_length_one() -> None:
    [vec] = await FakeEmbedder().embed(["Refunds are accepted within 14 days"])
    assert len(vec) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


async def test_same_text_gives_the_same_vector() -> None:
    embedder = FakeEmbedder()
    [first] = await embedder.embed(["delivery takes 3 to 5 days"])
    [second] = await embedder.embed(["delivery takes 3 to 5 days"])
    assert first == second


async def test_case_and_punctuation_do_not_matter() -> None:
    [a, b] = await FakeEmbedder().embed(["Refund policy!", "refund   POLICY"])
    assert a == b


async def test_order_of_results_matches_order_of_texts() -> None:
    embedder = FakeEmbedder()
    [a, b] = await embedder.embed(["refund days", "shipping charges"])
    [a2] = await embedder.embed(["refund days"])
    assert a == a2
    assert a != b


async def test_texts_that_share_words_are_closer() -> None:
    [query, refund, shipping] = await FakeEmbedder().embed(
        [
            "how many days for a refund",
            "You can ask for a refund within 14 days of delivery",
            "Shipping is free for orders above 5000 rupees",
        ]
    )
    assert _cosine(query, refund) > _cosine(query, shipping)


async def test_empty_text_still_gives_a_valid_vector() -> None:
    [vec] = await FakeEmbedder().embed([""])
    assert len(vec) == EMBEDDING_DIM
    assert math.isclose(sum(v * v for v in vec), 1.0)


async def test_empty_list_gives_empty_list() -> None:
    assert await FakeEmbedder().embed([]) == []


async def test_calls_are_recorded_for_the_no_re_embedding_tests() -> None:
    embedder = FakeEmbedder()
    await embedder.embed(["a", "b"])
    await embedder.embed(["c"])
    assert embedder.calls == [["a", "b"], ["c"]]


async def test_a_smaller_dimension_can_be_asked_for() -> None:
    [vec] = await FakeEmbedder(dim=8).embed(["refund"])
    assert len(vec) == 8


def test_factory_builds_the_fake_embedder() -> None:
    embedder = get_embedder(_settings("fake"))
    assert isinstance(embedder, FakeEmbedder)
    assert embedder.dim == EMBEDDING_DIM
    assert embedder.model == "fake-hash-v1"


def test_factory_ignores_case_and_spaces() -> None:
    assert isinstance(get_embedder(_settings("  FAKE ")), FakeEmbedder)


@pytest.mark.parametrize("provider", ["openai", "google", "cohere", "voyage"])
def test_factory_says_clearly_when_a_real_provider_is_not_written(provider: str) -> None:
    with pytest.raises(ConfigError, match="not written yet"):
        get_embedder(_settings(provider))


@pytest.mark.parametrize("provider", ["", "banana"])
def test_factory_never_turns_a_missing_setting_into_a_fake(provider: str) -> None:
    with pytest.raises(ConfigError):
        get_embedder(_settings(provider))

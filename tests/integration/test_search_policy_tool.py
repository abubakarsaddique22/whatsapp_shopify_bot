"""Phase 8, step 6: the search_policy tool of agent/tools.py on the real Postgres.

Same method as test_rag_retrieve.py: three made-up 'Zorbex' documents are ingested inside a
transaction that is rolled back. The relevance threshold is patched in each test, so the result
does not depend on the RAG_MIN_SCORE in your .env. The named tests T06, T07 and T18 are step 8.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agent import tools
from app.agent.tools import TOOL_POLICY, TOOLS, tool_search_policy
from app.core.errors import EmbeddingError
from app.core.settings import get_settings
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.rag.index import SourceDocument
from app.rag.ingest import ingest_documents

REFUND_URL = "https://zorbex.test/refund"
SHIPPING_URL = "https://zorbex.test/shipping"
REFUND_BODY = (
    "## Returns\nZorbex watches can be returned within 7 days of delivery.\n"
    "## Refunds\nZorbex refunds are paid back in 5 working days."
)
SHIPPING_BODY = (
    "## Delivery\nZorbex delivery takes 3 to 5 working days inside Pakistan.\n"
    "## Charges\nZorbex delivery is free above Rs 10,000."
)
FAQ_BODY = "Q: Do Zorbex watches have a warranty?\nA: Zorbex watches have a one year warranty."
REFUND_QUESTION = "Zorbex refunds paid back in how many working days"


def make_doc(key: str, doc_type: str, body: str, url: str | None = None) -> SourceDocument:
    return SourceDocument(
        source_key=key,
        doc_type=doc_type,
        source="shopify_policy",
        title="Zorbex test policy",
        source_url=url,
        body=body,
    )


DOCS = [
    make_doc("test:zorbex_refund", "policy_refund", REFUND_BODY, REFUND_URL),
    make_doc("test:zorbex_shipping", "policy_shipping", SHIPPING_BODY, SHIPPING_URL),
    make_doc("test:zorbex_faq", "faq", FAQ_BODY),
]


def set_threshold(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Make every retrieve() call inside the tool use this relevance threshold."""
    real = tools.retrieve

    async def with_threshold(*args, **kwargs):
        return await real(*args, **{**kwargs, "min_score": value})

    monkeypatch.setattr(tools, "retrieve", with_threshold)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            await ingest_documents(db, FakeEmbedder(), DOCS, managed_prefixes=("test:",))
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- a normal answer


async def test_passages_come_back_with_text_link_and_source_id(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(
        session, REFUND_QUESTION, "policy_refund", embedder=FakeEmbedder()
    )

    assert result.ok is True
    assert result.tool == TOOL_POLICY == "search_policy"
    assert result.source_id.startswith("search_policy:")  # the id of this tool call
    assert result.data["abstain"] is False
    first = result.data["passages"][0]
    assert set(first) == {"source_id", "doc_type", "heading", "text", "url", "score"}
    assert first["source_id"].startswith("kb_chunk:")  # the id of the passage, for the log
    assert first["url"] == REFUND_URL
    assert first["doc_type"] == "policy_refund"
    assert "5 working days" in first["text"]  # the exact number is in the passage


async def test_the_result_can_be_turned_into_json_for_the_model(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(session, REFUND_QUESTION, embedder=FakeEmbedder())
    json.dumps(result.data)  # no Decimal, datetime or other surprise in it


async def test_scores_are_rounded_to_four_decimals(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(session, REFUND_QUESTION, embedder=FakeEmbedder())
    assert result.data["best_score"] == round(result.data["best_score"], 4)
    for passage in result.data["passages"]:
        assert passage["score"] == round(passage["score"], 4)


async def test_a_doc_type_narrows_the_search_to_that_type_and_the_faq(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(
        session, "Zorbex working days", "policy_shipping", embedder=FakeEmbedder()
    )
    assert result.data["doc_type"] == "policy_shipping"
    assert result.data["passages"]
    assert {p["doc_type"] for p in result.data["passages"]} <= {"policy_shipping", "faq"}


# ---- abstention is an answer, not an error


async def test_when_nothing_is_relevant_enough_the_tool_abstains(session, monkeypatch):
    set_threshold(monkeypatch, 1.01)  # nothing can reach it
    result = await tool_search_policy(session, REFUND_QUESTION, embedder=FakeEmbedder())

    assert result.ok is True  # "not confirmed" is a normal result the bot words politely
    assert result.error_code is None
    assert result.data["abstain"] is True
    assert result.data["passages"] == []


# ---- the two texts


async def test_both_the_english_query_and_the_customers_words_are_searched(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    embedder = FakeEmbedder()
    roman_urdu = "paisay wapas kitne din mein"

    result = await tool_search_policy(
        session, "Zorbex refunds paid back", original_query=roman_urdu, embedder=embedder
    )

    assert embedder.calls == [["Zorbex refunds paid back", roman_urdu]]
    assert result.data["passages"][0]["url"] == REFUND_URL


async def test_the_customers_words_are_used_alone_when_the_english_query_is_empty(
    session, monkeypatch
):
    set_threshold(monkeypatch, 0.0)
    embedder = FakeEmbedder()

    result = await tool_search_policy(
        session, "", original_query="Zorbex refunds paid back", embedder=embedder
    )

    assert result.ok is True
    assert embedder.calls == [["Zorbex refunds paid back"]]  # the same text is not sent twice
    assert result.data["passages"][0]["url"] == REFUND_URL


async def test_a_very_long_customer_message_does_not_break_the_tool(session, monkeypatch):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(
        session, "", original_query="Zorbex refunds " * 100, embedder=FakeEmbedder()
    )
    assert result.ok is True


# ---- bad input never reaches the database


async def test_no_question_at_all_is_invalid_input(session):
    embedder = FakeEmbedder()
    for query, original in [("", None), ("   ", "  "), ("", "")]:
        result = await tool_search_policy(
            session, query, original_query=original, embedder=embedder
        )
        assert (result.ok, result.error_code) == (False, "invalid_input")
    assert embedder.calls == []


async def test_a_query_over_200_characters_is_invalid_input(session):
    result = await tool_search_policy(session, "a" * 201, embedder=FakeEmbedder())
    assert (result.ok, result.error_code) == (False, "invalid_input")


@pytest.mark.parametrize("doc_type", ["policy_orders", "product_description", "refund", "faq; --"])
async def test_an_unknown_doc_type_is_invalid_input(session, doc_type):
    embedder = FakeEmbedder()
    result = await tool_search_policy(session, "Zorbex refunds", doc_type, embedder=embedder)
    assert (result.ok, result.error_code) == (False, "invalid_input")
    assert embedder.calls == []


@pytest.mark.parametrize(
    "doc_type", ["policy_refund", "policy_shipping", "policy_privacy", "faq", "page"]
)
async def test_every_known_doc_type_is_accepted(session, monkeypatch, doc_type):
    set_threshold(monkeypatch, 0.0)
    result = await tool_search_policy(session, "Zorbex refunds", doc_type, embedder=FakeEmbedder())
    assert result.ok is True


# ---- provider problems


async def test_a_failing_embedding_provider_gives_an_honest_error_result(session):
    class Down:
        name = "down"
        model = "fake-hash-v1"
        dim = 1536

        async def embed(self, texts):
            raise EmbeddingError("provider down")

    result = await tool_search_policy(session, "Zorbex refunds", embedder=Down())

    assert (result.ok, result.error_code) == (False, "embedding_unavailable")
    assert result.data is None  # nothing is guessed


# ---- registry


def test_the_tool_is_in_the_registry():
    assert TOOLS[TOOL_POLICY] is tool_search_policy

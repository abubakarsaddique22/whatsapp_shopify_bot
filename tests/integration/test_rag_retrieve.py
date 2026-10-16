"""Phase 8, step 5: rag/retrieve.py on the real Postgres (Blueprint 7.7, tests T06 and T07 basics).

Each test works inside a transaction that is rolled back at the end. Three made-up 'Zorbex'
documents are ingested with the fake embedder (it matches WORDS, not meaning, so the tests use
the same words in the question and in the text). Passages are recognised by their source_url,
so real documents already in the knowledge base cannot break an assertion.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import EmbeddingError
from app.core.settings import get_settings
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.rag.index import SourceDocument
from app.rag.ingest import ingest_documents
from app.rag.retrieve import MAX_PASSAGES, candidates, retrieve

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


async def ingest(session: AsyncSession, docs, embedder=None):
    return await ingest_documents(
        session, embedder or FakeEmbedder(), docs, managed_prefixes=("test:",)
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            await ingest(db, DOCS)
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- finding the right passage


async def test_the_matching_passage_comes_first(session):
    found = await retrieve(
        session,
        FakeEmbedder(),
        query_en="Zorbex refunds paid back in how many working days",
        min_score=0.0,
    )
    assert not found.abstained
    first = found.passages[0]
    assert first.source_url == REFUND_URL
    assert "5 working days" in first.text  # the exact number is in the passage
    assert first.source_id == f"kb_chunk:{first.chunk_id}"


async def test_a_shipping_question_finds_the_shipping_passage(session):
    found = await retrieve(
        session,
        FakeEmbedder(),
        query_en="Zorbex delivery takes how many working days inside Pakistan",
        min_score=0.0,
    )
    assert found.passages[0].source_url == SHIPPING_URL
    assert "3 to 5 working days" in found.passages[0].text


async def test_the_english_query_and_the_original_words_are_both_searched(session):
    embedder = FakeEmbedder()
    english = "Zorbex refunds paid back"
    roman_urdu = "paisay wapas kitne din mein"

    found = await retrieve(session, embedder, query_en=english, original=roman_urdu, min_score=0.0)

    assert embedder.calls == [[english, roman_urdu]]  # one call, both texts
    assert found.passages[0].source_url == REFUND_URL  # the English text found it


async def test_the_original_question_alone_is_enough(session):
    found = await retrieve(
        session, FakeEmbedder(), query_en=None, original="Zorbex refunds paid back", min_score=0.0
    )
    assert found.passages[0].source_url == REFUND_URL


# ---- doc_type filter


async def test_a_doc_type_limits_the_search_to_that_type_and_the_faq(session):
    found = await retrieve(
        session,
        FakeEmbedder(),
        query_en="Zorbex working days",
        doc_type="policy_shipping",
        min_score=0.0,
    )
    assert found.passages
    assert {p.doc_type for p in found.passages} <= {"policy_shipping", "faq"}


async def test_without_a_doc_type_every_type_can_answer(session):
    found = await candidates(session, FakeEmbedder(), ["Zorbex working days"], None, top_k=50)
    urls = {p.source_url for p in found}
    assert {REFUND_URL, SHIPPING_URL} <= urls


async def test_a_policy_question_can_still_be_answered_by_the_faq(session):
    found = await retrieve(
        session,
        FakeEmbedder(),
        query_en="Zorbex watches warranty one year",
        doc_type="policy_refund",
        min_score=0.0,
    )
    assert any(p.doc_type == "faq" and "one year warranty" in p.text for p in found.passages)


# ---- threshold and abstention (T06: no guessing)


async def test_a_question_nothing_answers_abstains(session):
    found = await retrieve(
        session, FakeEmbedder(), query_en="qwertyuiop flurb zzzz", min_score=0.3
    )
    assert found.abstained is True
    assert found.passages == []


async def test_a_threshold_above_every_score_abstains_but_still_reports_the_best_score(session):
    found = await retrieve(
        session, FakeEmbedder(), query_en="Zorbex refunds paid back", min_score=1.01
    )
    assert found.abstained is True
    assert found.passages == []
    assert found.best_score is not None and found.best_score > 0.0


async def test_no_question_at_all_abstains_without_calling_the_provider(session):
    embedder = FakeEmbedder()
    found = await retrieve(session, embedder, query_en=None, original="   ", min_score=0.0)
    assert found.abstained is True
    assert found.best_score is None
    assert embedder.calls == []


async def test_the_default_threshold_comes_from_settings(session):
    found = await retrieve(session, FakeEmbedder(), query_en="Zorbex refunds paid back")
    assert found.abstained is (not found.passages)
    assert all(p.score >= get_settings().rag_min_score for p in found.passages)


# ---- limits


async def test_at_most_four_passages_by_default_and_the_limit_can_be_lowered(session):
    query = "Zorbex working days delivery refunds warranty watches"
    many = await retrieve(session, FakeEmbedder(), query_en=query, min_score=0.0)
    assert 0 < len(many.passages) <= MAX_PASSAGES
    two = await retrieve(session, FakeEmbedder(), query_en=query, min_score=0.0, limit=2)
    assert len(two.passages) <= 2


# ---- only current text from the current model


async def test_an_old_version_of_a_policy_is_never_returned(session):
    changed = REFUND_BODY.replace("7 days of delivery", "14 days of delivery")
    await ingest(session, [make_doc("test:zorbex_refund", "policy_refund", changed, REFUND_URL)])

    found = await candidates(session, FakeEmbedder(), ["Zorbex watches returned days"], top_k=50)

    texts = " ".join(p.text for p in found)
    assert "14 days of delivery" in texts
    assert "7 days of delivery" not in texts  # version 1 is inactive


async def test_chunks_made_with_another_embedding_model_are_not_searched(session):
    other = FakeEmbedder()
    other.model = "fake-hash-v2"
    doc = make_doc("test:quibble", "page", "## Odd\nQuibblewort glimmerfax is a made up thing.")
    await ingest(session, [doc], other)

    found = await candidates(session, FakeEmbedder(), ["Quibblewort glimmerfax"], top_k=50)

    assert all("Quibblewort" not in p.text for p in found)  # vectors of two models never mix


# ---- provider problems


async def test_a_provider_that_returns_the_wrong_size_raises(session):
    class WrongSize:
        name = "wrong"
        model = "fake-hash-v1"
        dim = 3

        async def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    with pytest.raises(EmbeddingError):
        await retrieve(session, WrongSize(), query_en="Zorbex refunds", min_score=0.0)

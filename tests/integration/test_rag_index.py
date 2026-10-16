"""Phase 8, step 3: rag/index.py on the real Postgres (versions, reuse of vectors, pruning).

Each test works inside a transaction that is rolled back at the end, with made-up 'Zorbex' texts
and 'test:' source keys, so the real knowledge base is never touched.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import EmbeddingError
from app.core.settings import get_settings
from app.db.tables import KbChunk, KbDocument
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.rag import index
from app.rag.chunk import chunk_document
from app.rag.index import SourceDocument

TITLE = "Zorbex test policy"
BODY = (
    "## Returns\nZorbex returns are accepted within 7 days.\n"
    "## Refunds\nZorbex refunds are paid in 5 working days."
)
CHANGED_BODY = BODY.replace("5 working days", "3 working days")
KEY = "test:zorbex_refund"


def make_doc(key: str = KEY) -> SourceDocument:
    return SourceDocument(
        source_key=key,
        doc_type="policy_refund",
        source="shopify_policy",
        title=TITLE,
        body=BODY,
    )


def make_chunks(body: str = BODY):
    return chunk_document("policy_refund", TITLE, body)


async def add_version(session: AsyncSession, embedder, key: str = KEY) -> int:
    """What ingest does for a changed source: close the old version, store a new one."""
    active = await index.active_documents(session)
    if key in active:
        await index.deactivate_document(session, active[key].id)
    version = await index.next_version(session, key)
    doc_id = await index.insert_document(
        session, make_doc(key), version=version, content_hash=f"h{version}"
    )
    chunks = make_chunks()
    vectors, _, _ = await index.embed_chunks(session, embedder, chunks)
    await index.insert_chunks(session, doc_id, chunks, vectors, embedder.model)
    return doc_id


async def count_chunks(session: AsyncSession, key: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(KbChunk)
        .join(KbDocument, KbDocument.id == KbChunk.doc_id)
        .where(KbDocument.source_key == key)
    )
    return int(result.scalar_one())


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- reuse of vectors ("only embed what changed")


async def test_the_test_text_makes_two_chunks():
    assert len(make_chunks()) == 2  # the other tests count on this


async def test_first_run_embeds_everything_and_second_run_embeds_nothing(session):
    chunks = make_chunks()
    first = FakeEmbedder()
    vectors, embedded, reused = await index.embed_chunks(session, first, chunks)
    assert (embedded, reused) == (2, 0)
    assert len(first.calls) == 1

    await add_version(session, FakeEmbedder())

    second = FakeEmbedder()
    again, embedded, reused = await index.embed_chunks(session, second, chunks)
    assert (embedded, reused) == (0, 2)
    assert second.calls == []  # the provider was not asked at all
    for old, new in zip(vectors, again, strict=True):
        assert new == pytest.approx(old, abs=1e-6)  # the stored vector is the same one


async def test_only_the_changed_chunk_is_embedded(session):
    await add_version(session, FakeEmbedder())
    embedder = FakeEmbedder()
    _, embedded, reused = await index.embed_chunks(session, embedder, make_chunks(CHANGED_BODY))
    assert (embedded, reused) == (1, 1)
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 1
    assert "3 working days" in embedder.calls[0][0]


async def test_the_same_text_twice_in_one_document_is_embedded_once(session):
    one = make_chunks()[0]
    twin = one.model_copy(update={"ordinal": 1})
    embedder = FakeEmbedder()
    vectors, embedded, reused = await index.embed_chunks(session, embedder, [one, twin])
    assert (embedded, reused) == (1, 1)
    assert embedder.calls == [[index.embedding_text(one.heading_path, one.text)]]
    assert vectors[0] == vectors[1]


async def test_a_vector_made_by_another_model_is_not_reused(session):
    await add_version(session, FakeEmbedder())
    other = FakeEmbedder()
    other.model = "fake-hash-v2"
    _, embedded, reused = await index.embed_chunks(session, other, make_chunks())
    assert (embedded, reused) == (2, 0)


async def test_a_provider_that_returns_the_wrong_size_stops_the_run(session):
    class WrongSize:
        name = "wrong"
        model = "wrong-size"
        dim = 3

        async def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    with pytest.raises(EmbeddingError):
        await index.embed_chunks(session, WrongSize(), make_chunks())
    assert await count_chunks(session, KEY) == 0  # nothing was written


# ---- versions


async def test_next_version_starts_at_one_and_counts_up(session):
    assert await index.next_version(session, KEY) == 1
    await add_version(session, FakeEmbedder())
    assert await index.next_version(session, KEY) == 2


async def test_active_documents_reports_hash_version_and_models(session):
    await add_version(session, FakeEmbedder())
    state = (await index.active_documents(session))[KEY]
    assert state.version == 1
    assert state.content_hash == "h1"
    assert state.embedding_models == {"fake-hash-v1"}


async def test_a_new_version_replaces_the_old_one(session):
    await add_version(session, FakeEmbedder())
    await add_version(session, FakeEmbedder())
    rows = await session.execute(
        select(KbDocument.version, KbDocument.is_active)
        .where(KbDocument.source_key == KEY)
        .order_by(KbDocument.version)
    )
    assert [(v, a) for v, a in rows.all()] == [(1, False), (2, True)]
    assert (await index.active_documents(session))[KEY].version == 2


async def test_two_active_versions_of_one_source_are_impossible(session):
    await add_version(session, FakeEmbedder())
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await index.insert_document(session, make_doc(), version=2, content_hash="h2")
    assert (await index.active_documents(session))[KEY].version == 1


# ---- clean-up


async def test_prune_keeps_the_newest_old_versions_and_their_chunks(session):
    embedder = FakeEmbedder()
    for _ in range(6):  # versions 1..6, only 6 is active
        await add_version(session, embedder)
    assert await count_chunks(session, KEY) == 12

    deleted = await index.prune_versions(session, KEY)  # keeps 3 inactive versions

    assert deleted == 2
    rows = await session.execute(
        select(KbDocument.version).where(KbDocument.source_key == KEY).order_by(KbDocument.version)
    )
    assert list(rows.scalars().all()) == [3, 4, 5, 6]
    assert await count_chunks(session, KEY) == 8  # chunks of versions 1 and 2 went with them


async def test_prune_never_deletes_the_active_version(session):
    await add_version(session, FakeEmbedder())
    assert await index.prune_versions(session, KEY, keep=0) == 0
    assert KEY in await index.active_documents(session)


async def test_delete_chunks_removes_the_text_but_keeps_the_document_row(session):
    doc_id = await add_version(session, FakeEmbedder())
    await index.deactivate_document(session, doc_id)
    await index.delete_chunks(session, doc_id)
    assert await count_chunks(session, KEY) == 0
    assert KEY not in await index.active_documents(session)
    versions = await session.execute(
        select(func.count()).select_from(KbDocument).where(KbDocument.source_key == KEY)
    )
    assert versions.scalar_one() == 1  # the row stays, for audit

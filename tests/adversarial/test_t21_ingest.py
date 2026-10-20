"""T21 at ingestion (Blueprint 9.4): a poisoned store text is flagged, never indexed, and the last
good version stays in use. Real Postgres, every test is rolled back (like test_security_events.py).

Every document row of evals/adversarial.jsonl goes through the real ingest_documents():
* the `must_flag` rows (instructions hidden in a policy, page or product text) must be flagged:
  not indexed, one `document_flagged` event that names the patterns and holds no text;
* the normal look-alikes ("You are now eligible for a refund") must be indexed as usual;
* an attack added to a REAL policy (evals/rag_corpus) leaves the previous version active.

KNOWN GAP (adv-048, found by this file): ingest scans the text AFTER cleaning. Cleaning turns
HTML-like markup into plain text, so a wrapper such as <system>...</system> disappears before the
scan, and the sentence inside it has no instruction wording. Shopify bodies are already turned
into plain text by integrations/shopify/mapper.py (html_to_text) before they reach ingest, so the
same happens there. The test below is a strict xfail: it passes the day the gap is closed, and
then the marker must be removed. See docs/threat_model.md.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import get_settings
from app.db.tables import SecurityEvent
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.rag import index
from app.rag.index import SourceDocument
from app.rag.ingest import IngestReport, ingest_documents, load_sources_from_dir
from tests.adversarial.helpers import document_rows, ids

CORPUS = Path(__file__).resolve().parents[2] / "evals" / "rag_corpus"
PREFIXES = ("test:",)
REFUND_KEY = "file:policy_refund"

KNOWN_GAPS = {"adv-048"}
ATTACKS = [row for row in document_rows(True) if row["id"] not in KNOWN_GAPS]
GAPS = [row for row in document_rows(True) if row["id"] in KNOWN_GAPS]
BENIGN = document_rows(False)
SAMPLE = ATTACKS[::3]  # a few attacks of different kinds for the "real policy" test


def make_doc(row: dict, doc_type: str = "policy_refund") -> SourceDocument:
    return SourceDocument(
        source_key=f"test:{row['id']}",
        doc_type=doc_type,
        source="shopify_policy",
        title="Adversarial test policy",
        body=f"## Policy\n{row['text']}",
    )


async def run(session: AsyncSession, docs: list[SourceDocument]) -> IngestReport:
    return await ingest_documents(session, FakeEmbedder(), docs, managed_prefixes=PREFIXES)


async def events_for(session: AsyncSession, ref: str) -> list[SecurityEvent]:
    rows = await session.execute(
        select(SecurityEvent).where(SecurityEvent.ref == ref).order_by(SecurityEvent.id)
    )
    return list(rows.scalars().all())


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


def test_the_adversarial_set_has_poisoned_documents_and_normal_look_alikes() -> None:
    assert len(ATTACKS) + len(GAPS) >= 10
    assert len(BENIGN) >= 8


@pytest.mark.parametrize("row", ATTACKS, ids=ids(ATTACKS))
async def test_t21_a_poisoned_document_is_flagged_and_not_indexed(
    session: AsyncSession, row: dict
) -> None:
    doc = make_doc(row)

    report = await run(session, [doc])

    assert [f.source_key for f in report.flagged] == [doc.source_key]
    assert report.added == 0
    assert doc.source_key not in await index.active_documents(session)
    (event,) = await events_for(session, doc.source_key)
    assert event.kind == "document_flagged"
    assert event.detail["findings"]  # which patterns matched
    assert row["text"] not in json.dumps(event.detail, ensure_ascii=False)  # never the text


@pytest.mark.parametrize("row", BENIGN, ids=ids(BENIGN))
async def test_a_normal_sentence_that_looks_like_an_attack_is_indexed(
    session: AsyncSession, row: dict
) -> None:
    doc = make_doc(row)

    report = await run(session, [doc])

    assert report.flagged == []
    assert report.added == 1
    assert doc.source_key in await index.active_documents(session)
    assert await events_for(session, doc.source_key) == []


@pytest.mark.parametrize("row", SAMPLE, ids=ids(SAMPLE))
async def test_t21_an_attack_added_to_a_real_policy_keeps_the_last_good_version(
    session: AsyncSession, row: dict
) -> None:
    docs = load_sources_from_dir(CORPUS)
    embedder = FakeEmbedder()
    first = await ingest_documents(session, embedder, docs, managed_prefixes=("file:",))
    assert first.flagged == []  # the real policies are clean
    before = (await index.active_documents(session))[REFUND_KEY]

    poisoned = [
        d.model_copy(update={"body": f"{d.body}\n\n{row['text']}"})
        if d.source_key == REFUND_KEY
        else d
        for d in docs
    ]
    report = await ingest_documents(session, embedder, poisoned, managed_prefixes=("file:",))

    assert [f.source_key for f in report.flagged] == [REFUND_KEY]
    assert report.updated == 0
    after = (await index.active_documents(session))[REFUND_KEY]
    assert (after.id, after.version) == (before.id, before.version)  # the good text stays in use


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "KNOWN GAP: ingest scans the text after HTML-like markup is stripped (docs/threat_model.md)"
    ),
)
@pytest.mark.parametrize("row", GAPS, ids=ids(GAPS))
async def test_known_gap_an_instruction_wrapped_in_fake_tags_is_flagged(
    session: AsyncSession, row: dict
) -> None:
    doc = make_doc(row)

    report = await run(session, [doc])

    assert [f.source_key for f in report.flagged] == [doc.source_key]

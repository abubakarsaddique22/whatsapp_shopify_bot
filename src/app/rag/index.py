"""Phase 8, step 3: store documents and chunks in Postgres (Blueprint 7.4 and 7.6).

These are the small database and embedding helpers that rag/ingest.py calls. The caller owns the
transaction (commit or rollback), like db/repositories.py.

How a vector goes in and out
----------------------------
A vector is sent as text ("[0.1,0.2,...]") and cast inside SQL, and read back with `::text`. This
works with asyncpg without registering any pgvector codec, and the column size (1536) is checked
by Postgres itself.

What "unchanged" means
----------------------
A chunk's content_hash covers its heading_path and its text, which are exactly what is embedded
(`embedding_text`). So a chunk with the same hash and the same embedding model has the same
vector, and `embed_chunks` copies it instead of asking the provider again.
"""

import json
import math
from collections.abc import Collection, Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Text, cast, delete, func, insert, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmbeddingError
from app.db.tables import KbChunk, KbDocument
from app.integrations.embeddings.base import EMBEDDING_DIM, Embedder
from app.rag.chunk import Chunk

EMBED_BATCH_SIZE = 64  # texts per call to the provider
KEEP_OLD_VERSIONS = 3  # inactive versions kept per source, for audit (Blueprint 7.6)

# CAST(text AS vector): the parameter is plain text for asyncpg, Postgres makes the vector.
_INSERT_CHUNK = sql_text(
    """
    INSERT INTO kb_chunks
        (doc_id, ordinal, heading_path, text, token_count, content_hash,
         embedding_model, embedding)
    VALUES
        (:doc_id, :ordinal, :heading_path, :text, :token_count, :content_hash,
         :embedding_model, CAST(CAST(:embedding AS text) AS vector))
    """
)


class SourceDocument(BaseModel):
    """One approved text from the store, before it is chunked."""

    source_key: str  # stable name of the source: "policy:REFUND_POLICY", "page:<shopify id>"
    doc_type: str  # policy_refund | policy_shipping | policy_privacy | faq | page | ...
    source: str  # shopify_policy | shopify_page | product
    title: str
    source_url: str | None = None  # the official link a reply may use
    body: str
    language: str = "en"


class DocState(BaseModel):
    """What is stored now for the ACTIVE version of a source."""

    id: int
    version: int
    content_hash: str
    embedding_models: set[str]  # the models of its chunks


def embedding_text(heading_path: str | None, text: str) -> str:
    """What is embedded for a chunk: the section name helps the search, so it goes in too."""
    return f"{heading_path}\n{text}" if heading_path else text


def vector_literal(vector: Sequence[float]) -> str:
    """'[0.5,-1.0]': the text form pgvector reads."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def check_vectors(vectors: Sequence[Sequence[float]], expected: int) -> None:
    """The provider answered with one good vector per text, of the size of the column."""
    if len(vectors) != expected:
        raise EmbeddingError(
            f"The embedding provider returned {len(vectors)} vectors for {expected} texts"
        )
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"Vector size {len(vector)} does not match the kb_chunks column ({EMBEDDING_DIM})"
            )
        if not all(math.isfinite(v) for v in vector):
            raise EmbeddingError("The embedding provider returned a vector with NaN or infinity")


# ---- reads


async def active_documents(session: AsyncSession) -> dict[str, DocState]:
    """{source_key: DocState} for every active document."""
    docs = await session.execute(
        select(
            KbDocument.source_key, KbDocument.id, KbDocument.version, KbDocument.content_hash
        ).where(KbDocument.is_active.is_(True))
    )
    models = await session.execute(select(KbChunk.doc_id, KbChunk.embedding_model).distinct())
    by_doc: dict[int, set[str]] = {}
    for doc_id, model in models.all():
        by_doc.setdefault(doc_id, set()).add(model)
    return {
        key: DocState(
            id=doc_id,
            version=version,
            content_hash=hash_,
            embedding_models=by_doc.get(doc_id, set()),
        )
        for key, doc_id, version, hash_ in docs.all()
    }


async def next_version(session: AsyncSession, source_key: str) -> int:
    """One more than the highest version this source ever had (a removed source can come back)."""
    result = await session.execute(
        select(func.coalesce(func.max(KbDocument.version), 0)).where(
            KbDocument.source_key == source_key
        )
    )
    return int(result.scalar_one()) + 1


async def reusable_embeddings(
    session: AsyncSession, hashes: Collection[str], model: str
) -> dict[str, list[float]]:
    """{content_hash: vector} for chunks already stored with this model (any version)."""
    if not hashes:
        return {}
    rows = await session.execute(
        select(KbChunk.content_hash, cast(KbChunk.embedding, Text)).where(
            KbChunk.content_hash.in_(list(hashes)), KbChunk.embedding_model == model
        )
    )
    return {hash_: [float(x) for x in json.loads(vector)] for hash_, vector in rows.all()}


# ---- embedding


async def embed_chunks(
    session: AsyncSession, embedder: Embedder, chunks: Sequence[Chunk]
) -> tuple[list[list[float]], int, int]:
    """One vector per chunk, in order, plus how many were embedded and how many were reused.

    A vector is reused when a stored chunk has the same hash and the same model. The same text
    twice in one document is embedded once. Nothing is written here.
    """
    known = await reusable_embeddings(session, {c.content_hash for c in chunks}, embedder.model)
    todo: dict[str, str] = {}  # content_hash -> the text to embed
    for chunk in chunks:
        if chunk.content_hash not in known and chunk.content_hash not in todo:
            todo[chunk.content_hash] = embedding_text(chunk.heading_path, chunk.text)

    hashes = list(todo)
    for start in range(0, len(hashes), EMBED_BATCH_SIZE):
        batch = hashes[start : start + EMBED_BATCH_SIZE]
        vectors = await embedder.embed([todo[h] for h in batch])
        check_vectors(vectors, len(batch))
        known.update(zip(batch, vectors, strict=True))

    embedded = len(todo)
    return [known[c.content_hash] for c in chunks], embedded, len(chunks) - embedded


# ---- writes


async def insert_document(
    session: AsyncSession, doc: SourceDocument, *, version: int, content_hash: str
) -> int:
    """A new ACTIVE document row. Deactivate the previous version first (one active per source)."""
    result = await session.execute(
        insert(KbDocument)
        .values(
            source_key=doc.source_key,
            doc_type=doc.doc_type,
            source=doc.source,
            title=doc.title,
            source_url=doc.source_url,
            language=doc.language,
            version=version,
            content_hash=content_hash,
            is_active=True,
        )
        .returning(KbDocument.id)
    )
    return int(result.scalar_one())


async def insert_chunks(
    session: AsyncSession,
    doc_id: int,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
    model: str,
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "doc_id": doc_id,
            "ordinal": chunk.ordinal,
            "heading_path": chunk.heading_path,
            "text": chunk.text,
            "token_count": chunk.token_count,
            "content_hash": chunk.content_hash,
            "embedding_model": model,
            "embedding": vector_literal(vector),
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    if rows:
        await session.execute(_INSERT_CHUNK, rows)


async def deactivate_document(session: AsyncSession, doc_id: int) -> None:
    """The document stays in the table (audit) but retrieval no longer sees it."""
    await session.execute(update(KbDocument).where(KbDocument.id == doc_id).values(is_active=False))


async def delete_chunks(session: AsyncSession, doc_id: int) -> None:
    await session.execute(delete(KbChunk).where(KbChunk.doc_id == doc_id))


async def prune_versions(
    session: AsyncSession, source_key: str, keep: int = KEEP_OLD_VERSIONS
) -> int:
    """Delete the oldest INACTIVE versions of a source, keeping the newest `keep`.

    Their chunks go with them (ON DELETE CASCADE). Returns how many documents were deleted.
    """
    result = await session.execute(
        select(KbDocument.id)
        .where(KbDocument.source_key == source_key, KbDocument.is_active.is_(False))
        .order_by(KbDocument.version.desc())
        .offset(keep)
    )
    old_ids = list(result.scalars().all())
    if old_ids:
        await session.execute(delete(KbDocument).where(KbDocument.id.in_(old_ids)))
    return len(old_ids)

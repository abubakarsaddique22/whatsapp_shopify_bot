"""Phase 8, step 5: find the approved passages that answer a question (Blueprint 7.7).

    found = await retrieve(
        session, embedder, query_en="refund policy", original="refund kitne din?"
    )
    found.abstained          # True: nothing was relevant enough, so the bot must not guess
    found.passages           # up to 4 passages, best first, each with a source_id

How a search works:

1. Both texts are searched: `query_en` (the short English query the router writes) and the
   customer's original words. A Roman Urdu question often misses English documents, the English
   query fixes that.
2. For each text, two searches over ACTIVE chunks only (and only chunks made with the current
   embedding model, because vectors of two models cannot be compared):
   - vector search: the closest chunks by cosine distance (pgvector);
   - full-text search: chunks that contain any of the words (Postgres, English stemming).
3. The lists are merged with reciprocal rank fusion (RRF): a chunk that is high in several
   lists wins. RRF only decides the ORDER.
4. Relevance is a separate number: `score` is the best cosine similarity of the chunk to any of
   the query texts (full-text hits get their cosine too, in the same SQL). A passage under
   RAG_MIN_SCORE is dropped. If nothing is left the answer is "abstain": the bot says it is not
   confirmed and offers a human. The right RAG_MIN_SCORE is chosen from the golden set:
   `uv run python -m app.cli rag-eval` prints the hit rate and abstention accuracy for many
   thresholds.
5. doc_type: when the intent is clear the search is limited to that type plus the FAQ documents
   (a refund question looks in the refund policy and the FAQ).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import TextClause

from app.core.settings import get_settings
from app.integrations.embeddings.base import Embedder
from app.rag.index import check_vectors, vector_literal

DOC_TYPES = ("policy_refund", "policy_shipping", "policy_privacy", "faq", "page")
MAX_PASSAGES = 4  # what goes to the composer (Blueprint 7.7: 3 or 4)
RRF_K = 60  # the usual constant of reciprocal rank fusion
MAX_QUERY_CHARS = 500  # a long customer message is cut before it is embedded
MAX_FTS_WORDS = 24

_WORD = re.compile(r"[^\W_]+")  # letters and digits of any script, no punctuation

_COLUMNS = (
    "c.chunk_id, c.doc_id, d.doc_type, d.source_url, c.heading_path, c.text, "
    "1 - (c.embedding <=> CAST(CAST(:q AS text) AS vector)) AS cosine"
)
_FROM = "FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id"


class Passage(BaseModel):
    chunk_id: int
    doc_id: int
    doc_type: str
    heading_path: str | None
    text: str
    source_url: str | None
    score: float  # best cosine similarity to any query text (this is what RAG_MIN_SCORE checks)
    rrf: float  # fused rank score (only the order matters)

    @property
    def source_id(self) -> str:
        """Goes into BotResponse.source_ids and the log, never into a customer reply."""
        return f"kb_chunk:{self.chunk_id}"


class Retrieval(BaseModel):
    passages: list[Passage]
    abstained: bool
    best_score: float | None  # best cosine among everything found, even under the threshold


@dataclass
class _Hit:
    row: Any
    rrf: float = 0.0
    cosine: float = -1.0


# ---- small pure helpers


def allowed_doc_types(doc_type: str | None) -> list[str] | None:
    """The types to search for an intent: its own type plus the FAQ. None means every type."""
    if not doc_type:
        return None
    return [doc_type] if doc_type == "faq" else [doc_type, "faq"]


def clean_queries(queries: Sequence[str | None]) -> list[str]:
    """Non-empty queries, whitespace tidied, cut to MAX_QUERY_CHARS, without repeats."""
    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        text = " ".join((query or "").split())[:MAX_QUERY_CHARS]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
    return result


def tsquery_text(text: str) -> str:
    """'refund days?' -> 'refund | days'. Letters and digits only, so no tsquery syntax gets in."""
    words: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word not in words:
            words.append(word)
        if len(words) == MAX_FTS_WORDS:
            break
    return " | ".join(words)


def select_passages(
    found: Sequence[Passage], min_score: float, limit: int = MAX_PASSAGES
) -> list[Passage]:
    """The relevant passages (score at or above the threshold), best first, at most `limit`."""
    return [p for p in found if p.score >= min_score][:limit]


# ---- SQL


def _where(typed: bool) -> str:
    where = "d.is_active AND c.embedding_model = :model"
    return f"{where} AND d.doc_type IN :types" if typed else where


def _with_types(statement: TextClause, typed: bool) -> TextClause:
    return statement.bindparams(bindparam("types", expanding=True)) if typed else statement


def _vector_sql(typed: bool) -> TextClause:
    statement = sql_text(
        f"SELECT {_COLUMNS} {_FROM} WHERE {_where(typed)} "
        "ORDER BY c.embedding <=> CAST(CAST(:q AS text) AS vector) LIMIT :k"
    )
    return _with_types(statement, typed)


def _fts_sql(typed: bool) -> TextClause:
    statement = sql_text(
        f"SELECT {_COLUMNS} {_FROM} WHERE {_where(typed)} "
        "AND c.tsv @@ to_tsquery('english', :tsq) "
        "ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :tsq)) DESC, c.chunk_id LIMIT :k"
    )
    return _with_types(statement, typed)


# ---- search


async def candidates(
    session: AsyncSession,
    embedder: Embedder,
    queries: Sequence[str | None],
    doc_type: str | None = None,
    *,
    top_k: int | None = None,
) -> list[Passage]:
    """Every chunk found by vector or full-text search, fused and ranked. No threshold yet.

    `top_k` is the size of each list (default RAG_TOP_K). Raises EmbeddingError if the provider
    fails.
    """
    texts = clean_queries(queries)
    if not texts:
        return []
    k = top_k or get_settings().rag_top_k
    vectors = await embedder.embed(texts)
    check_vectors(vectors, len(texts))

    types = allowed_doc_types(doc_type)
    typed = types is not None
    hits: dict[int, _Hit] = {}

    for text, vector in zip(texts, vectors, strict=True):
        params: dict[str, Any] = {"q": vector_literal(vector), "model": embedder.model, "k": k}
        if types is not None:
            params["types"] = types
        lists = [(await session.execute(_vector_sql(typed), params)).all()]
        tsq = tsquery_text(text)
        if tsq:
            lists.append((await session.execute(_fts_sql(typed), {**params, "tsq": tsq})).all())
        for rows in lists:
            for rank, row in enumerate(rows, start=1):
                hit = hits.setdefault(int(row.chunk_id), _Hit(row))
                hit.rrf += 1.0 / (RRF_K + rank)
                hit.cosine = max(hit.cosine, float(row.cosine))

    ordered = sorted(hits.values(), key=lambda h: (-h.rrf, -h.cosine, int(h.row.chunk_id)))
    return [
        Passage(
            chunk_id=int(h.row.chunk_id),
            doc_id=int(h.row.doc_id),
            doc_type=str(h.row.doc_type),
            heading_path=h.row.heading_path,
            text=str(h.row.text),
            source_url=h.row.source_url,
            score=h.cosine,
            rrf=h.rrf,
        )
        for h in ordered
    ]


async def retrieve(
    session: AsyncSession,
    embedder: Embedder,
    *,
    query_en: str | None,
    original: str | None = None,
    doc_type: str | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
    limit: int = MAX_PASSAGES,
) -> Retrieval:
    """The passages to give the composer, or an abstention when nothing is relevant enough."""
    threshold = get_settings().rag_min_score if min_score is None else min_score
    found = await candidates(session, embedder, [query_en, original], doc_type, top_k=top_k)
    chosen = select_passages(found, threshold, limit)
    return Retrieval(
        passages=chosen,
        abstained=not chosen,
        best_score=max((p.score for p in found), default=None),
    )

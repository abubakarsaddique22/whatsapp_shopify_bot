"""Phase 8: embeddings from a local sentence-transformers model (Blueprint 7.6).

    embedder = SentenceTransformerEmbedder("BAAI/bge-m3")
    vectors = await embedder.embed(["refund policy", "refund kitne din mein milta hai?"])

The model runs on your own computer: no API key, no quota, no network after the first download
(bge-m3 is about 2.2 GB and is cached by Hugging Face the first time it is used). It is
multilingual, so Roman Urdu questions can find English policy text.

sentence-transformers is synchronous, so the work runs in a thread (asyncio.to_thread) and does not
block the worker. The model is loaded once and kept in memory.

Vector size. kb_chunks.embedding holds EMBEDDING_DIM (1536) numbers, and a vector column has a
fixed size. bge-m3 gives 1024, so every vector is padded with zeros up to 1536. Zeros change
nothing for cosine similarity, so scores and ranking are exactly those of the model itself. A model
with MORE than 1536 numbers is refused with a clear error: it needs a new migration.

`model` is "st:<name>", so chunks made with one model are never compared with chunks from another
(rag/retrieve.py only compares vectors of the same `model`). rag-ingest re-embeds the chunks when
this name changes.

Errors never contain the texts that were sent.
"""

import asyncio
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from app.core.errors import EmbeddingError
from app.integrations.embeddings.base import EMBEDDING_DIM

DEFAULT_MODEL = "BAAI/bge-m3"
BATCH_SIZE = 32  # texts per step inside the model (memory use, not an API limit)


@lru_cache(maxsize=2)
def _load_model(name: str) -> Any:
    from sentence_transformers import SentenceTransformer  # imported late: it is slow to import

    return SentenceTransformer(name)


def pad_vector(vector: Sequence[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """The vector followed by zeros up to `dim` numbers. A longer vector is an error."""
    if len(vector) > dim:
        raise EmbeddingError(
            f"The model gives {len(vector)} numbers per text, but kb_chunks holds {dim}. "
            "Use a smaller model or add a migration for a bigger column."
        )
    return [*vector, *([0.0] * (dim - len(vector)))]


class SentenceTransformerEmbedder:
    name = "sentence-transformers"
    dim = EMBEDDING_DIM

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model_name = model
        self.model = f"st:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._encode, texts)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        try:
            rows = _load_model(self._model_name).encode(
                texts,
                batch_size=BATCH_SIZE,
                normalize_embeddings=True,  # length 1, so a dot product is the cosine
                show_progress_bar=False,
            )
        except Exception:
            raise EmbeddingError(
                f"The embedding model '{self._model_name}' failed to load or run: check "
                "EMBEDDING_MODEL, the internet for the first download, and free memory"
            ) from None
        if len(rows) != len(texts):
            raise EmbeddingError(f"The model returned {len(rows)} vectors for {len(texts)} texts")
        return [pad_vector([float(v) for v in row]) for row in rows]

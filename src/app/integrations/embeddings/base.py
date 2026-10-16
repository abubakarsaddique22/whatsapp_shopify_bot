"""Phase 8, step 1: what every embedding provider looks like (Blueprint 7.6).

    vectors = await embedder.embed(["refund policy", "delivery kitne din mein?"])
    len(vectors) == 2, len(vectors[0]) == embedder.dim

The RAG code (rag/index.py, rag/retrieve.py) only knows this interface, so the real model and the
fake one used in tests can be swapped by the EMBEDDING_PROVIDER setting. A provider never touches
the database. It returns one vector per text, in the same order, and each vector has `dim` numbers.

A provider raises EmbeddingError (core/errors.py) when it fails. It never puts a key or the text
that was sent into the error message.

EMBEDDING_DIM is the size of the `embedding` column in kb_chunks (migration d8f3a2b61c05). A model
with another size needs a new migration, because a vector column has a fixed size. Many models can
return 1536 numbers (for example OpenAI text-embedding-3 with `dimensions=1536`). The model is
chosen later by the retrieval evaluation (Blueprint 7.6 and 7.9).
"""

from typing import Protocol

EMBEDDING_DIM = 1536


class Embedder(Protocol):
    name: str  # provider name, for logs: "fake", ...
    model: str  # stored in kb_chunks.embedding_model, so a model change can be found later
    dim: int  # always EMBEDDING_DIM for now

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

"""A pretend embedding provider for tests and local development (Phase 8, step 1).

No network, no key. It turns a text into a vector from its WORDS: every word adds +1 or -1 to one
fixed position (chosen by a hash of the word), then the vector is scaled to length 1. So:

- the same text always gives the same vector (tests are repeatable);
- two texts that share words get vectors that point the same way (a question about "refund days"
  is closer to the refund policy than to the shipping policy);
- it does NOT understand meaning. "money back" and "refund" look unrelated to it. Real hit rates
  (Roman Urdu questions against English text) can only be measured with a real model.

`calls` records every batch that was sent, so a test can prove that unchanged content is not
embedded again:

    embedder = FakeEmbedder()
    await embedder.embed(["a", "b"])
    embedder.calls            # -> [["a", "b"]]
"""

import hashlib
import math
import re

from app.integrations.embeddings.base import EMBEDDING_DIM

_WORD = re.compile(r"\w+")


def _vector(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        position = int.from_bytes(digest[:4], "big") % dim
        vec[position] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:  # empty text, or words that cancelled out: still a valid unit vector
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


class FakeEmbedder:
    name = "fake"
    model = "fake-hash-v1"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []  # every batch given to embed(), in order

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_vector(text, self.dim) for text in texts]

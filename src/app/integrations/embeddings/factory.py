"""Phase 8, step 1 and 7: build the embedding provider named in the settings.

    embedder = get_embedder()
    vectors = await embedder.embed(["refund policy"])

EMBEDDING_PROVIDER decides what you get:
- "fake"                  FakeEmbedder: no network, no key. For tests and local development.
- "sentence-transformers" SentenceTransformerEmbedder: a multilingual model that runs on your own
                          computer, named in EMBEDDING_MODEL (empty = BAAI/bge-m3). No key needed.
                          The first run downloads the model (bge-m3 is about 2.2 GB).
- openai, google, cohere, voyage: not written yet, on purpose (the project embeds locally).
- empty or anything else: ConfigError. A missing setting must never silently turn into a fake.

`get_eval_embedder()` is what `rag-eval` uses by default: the local sentence-transformers model,
whatever EMBEDDING_PROVIDER says, so the evaluation works even while the app runs on "fake".
"""

from app.core.errors import ConfigError
from app.core.settings import Settings, get_settings
from app.integrations.embeddings.base import Embedder
from app.integrations.embeddings.fake_embedder import FakeEmbedder
from app.integrations.embeddings.sentence_transformer_embedder import (
    DEFAULT_MODEL,
    SentenceTransformerEmbedder,
)

NOT_WRITTEN = ("openai", "google", "cohere", "voyage")


def _local(settings: Settings) -> Embedder:
    return SentenceTransformerEmbedder(settings.embedding_model.strip() or DEFAULT_MODEL)


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    provider = settings.embedding_provider.strip().lower().replace("_", "-")

    if provider == "fake":
        return FakeEmbedder()

    if provider == "sentence-transformers":
        return _local(settings)

    if provider in NOT_WRITTEN:
        raise ConfigError(
            f"EMBEDDING_PROVIDER={provider} is not written yet "
            "(the project embeds with sentence-transformers)"
        )

    raise ConfigError(
        f"EMBEDDING_PROVIDER must be 'fake' or 'sentence-transformers' for now, got '{provider}'"
    )


async def warm_up(embedder: Embedder | None = None) -> None:
    """Load the model NOW, not inside the first customer message.

    The first load imports torch and reads the model: it can take longer than the whole time
    budget of a turn (TURN_BUDGET_SECONDS), so the first policy question ended in the safe reply
    plus a handoff (seen in the real-model check, 2026-10-17). Call it once at start-up.
    """
    await (embedder or get_embedder()).embed(["warm up"])


def get_eval_embedder(settings: Settings | None = None) -> Embedder:
    """The embedder `rag-eval` uses: the local sentence-transformers model (default bge-m3)."""
    return _local(settings or get_settings())

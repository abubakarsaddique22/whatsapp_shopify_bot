"""warm_up(): the embedding model is loaded at start-up, not in the first customer message."""

from app.integrations.embeddings.factory import warm_up


class RecordingEmbedder:
    name = "recording"
    model = "recording:test"
    dim = 1536

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.0] * self.dim for _ in texts]


async def test_warm_up_embeds_one_short_text_once() -> None:
    embedder = RecordingEmbedder()
    await warm_up(embedder)
    assert embedder.calls == [["warm up"]]

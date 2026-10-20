r"""Does the REAL embedding model still work? (torch / sentence-transformers upgrade check)

    uv run python scripts\embedding_smoke.py
    uv run python scripts\embedding_smoke.py --model BAAI/bge-m3

The unit tests use a tiny fake model, so they cannot show that the real model loads and gives
sensible vectors after a torch or sentence-transformers upgrade. This script does, without the
database and without the network (after the first download):

1. prints the versions of python, torch, transformers, sentence-transformers and numpy;
2. loads EMBEDDING_MODEL from .env DIRECTLY with sentence-transformers, so a failure shows the full,
   real error (the app's embedder turns every failure into one short EmbeddingError on purpose);
3. embeds a refund question in English, Roman Urdu and Urdu script plus two unrelated sentences
   and checks the vectors: the right size, finite numbers, length 1, and the three refund
   sentences closer to each other than to the unrelated ones (that is what retrieval needs);
4. runs the same sentences through the app's own embedder (padding to 1536, the kb_chunks size)
   and checks it gives the same numbers.

Exit code 0 = the model works, 1 = something is wrong (the reason is printed).
For the full picture afterwards: `uv run python scripts\run_eval.py --set retrieval` and compare
with the baseline in docs\PROGRESS.md (hit rate 97.5%, MRR 0.899, abstention 14/20 at 0.40).
"""

import argparse
import asyncio
import importlib.metadata
import io
import math
import platform
import sys
import time
import traceback
from collections.abc import Sequence

from app.core.settings import get_settings
from app.integrations.embeddings.base import EMBEDDING_DIM
from app.integrations.embeddings.sentence_transformer_embedder import (
    DEFAULT_MODEL,
    SentenceTransformerEmbedder,
)

SAME_MEANING = {
    "en": "How many days do I have to return a watch?",
    "roman_ur": "Watch wapas karne ke liye kitne din milte hain?",
    "ur": "گھڑی واپس کرنے کے لیے کتنے دن ملتے ہیں؟",
}
UNRELATED = {
    "weather": "It will rain in Lahore tomorrow evening.",
    "cricket": "Pakistan won the cricket match by five wickets.",
}
PACKAGES = ("torch", "transformers", "sentence-transformers", "numpy")
MIN_MARGIN = 0.05  # the refund sentences must beat the unrelated ones by at least this much


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def check_vectors(vectors: dict[str, Sequence[float]]) -> tuple[list[str], dict[str, float]]:
    """Problems (empty = fine) and the similarities that were measured. Pure: unit tested."""
    problems: list[str] = []
    sizes = {len(v) for v in vectors.values()}
    if len(sizes) != 1 or 0 in sizes:
        problems.append(f"vectors have different or empty sizes: {sorted(sizes)}")
        return problems, {}
    for name, vector in vectors.items():
        if not all(math.isfinite(x) for x in vector):
            problems.append(f"{name}: NaN or infinite numbers")
        length = math.sqrt(sum(x * x for x in vector))
        if abs(length - 1.0) > 0.01:
            problems.append(f"{name}: length {length:.3f}, expected 1 (normalised)")
    same = [k for k in SAME_MEANING if k in vectors]
    other = [k for k in UNRELATED if k in vectors]
    scores: dict[str, float] = {}
    for i, a in enumerate(same):
        for b in same[i + 1 :]:
            scores[f"{a} ~ {b}"] = cosine(vectors[a], vectors[b])
    for a in same:
        for b in other:
            scores[f"{a} ~ {b}"] = cosine(vectors[a], vectors[b])
    # Retrieval only needs the RIGHT passage ranked above the wrong ones, so per sentence: its
    # nearest same-meaning partner must beat its nearest unrelated sentence. (A small model can
    # keep two languages far apart and still rank correctly: MiniLM gives en ~ roman_ur 0.13.)
    for a in same:
        partners = [cosine(vectors[a], vectors[b]) for b in same if b != a]
        strangers = [cosine(vectors[a], vectors[b]) for b in other]
        if partners and strangers and max(partners) < max(strangers) + MIN_MARGIN:
            problems.append(
                f"{a}: same meaning is not clearly closer: best same-meaning "
                f"{max(partners):.3f}, best unrelated {max(strangers):.3f}"
            )
    return problems, scores


def version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that the real embedding model works")
    parser.add_argument("--model", help="model name (default: EMBEDDING_MODEL from .env)")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # Urdu text must not crash a Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    name = args.model or get_settings().embedding_model.strip() or DEFAULT_MODEL
    print(f"python {platform.python_version()}")
    for package in PACKAGES:
        print(f"{package:<22} {version(package)}")
    print(f"model                  {name}\n")

    started = time.perf_counter()
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(name)
    except Exception:
        print("FAILED to load the model. The real error:\n")
        traceback.print_exc()
        return 1
    print(f"loaded in {time.perf_counter() - started:.1f} s")

    texts = {**SAME_MEANING, **UNRELATED}
    try:
        rows = model.encode(list(texts.values()), normalize_embeddings=True)
    except Exception:
        print("FAILED to embed. The real error:\n")
        traceback.print_exc()
        return 1
    vectors = {key: [float(x) for x in row] for key, row in zip(texts, rows, strict=True)}
    size = len(next(iter(vectors.values())))
    print(f"vector size            {size} (kb_chunks holds {EMBEDDING_DIM}, the rest is zeros)\n")

    problems, scores = check_vectors(vectors)
    for pair, score in scores.items():
        print(f"  {pair:<22} {score:.3f}")
    if size > EMBEDDING_DIM:
        problems.append(f"the model gives {size} numbers, more than the {EMBEDDING_DIM} column")

    padded = asyncio.run(SentenceTransformerEmbedder(name).embed(list(texts.values())))
    for key, vector in zip(texts, padded, strict=True):
        if len(vector) != EMBEDDING_DIM or any(vector[size:]):
            problems.append(f"app embedder: {key} is not padded to {EMBEDDING_DIM} with zeros")
        elif max(abs(a - b) for a, b in zip(vector[:size], vectors[key], strict=True)) > 1e-4:
            problems.append(f"app embedder: {key} differs from the direct model")

    print()
    if problems:
        print("NOT OK:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("OK: the model loads, the vectors are sound and the app embedder matches it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

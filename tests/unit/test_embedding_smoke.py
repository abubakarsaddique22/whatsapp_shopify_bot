"""The checks of scripts/embedding_smoke.py (the vectors themselves, no model loaded)."""

import importlib.util
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "embedding_smoke", ROOT / "scripts" / "embedding_smoke.py"
)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def unit(*values: float) -> list[float]:
    length = math.sqrt(sum(v * v for v in values))
    return [v / length for v in values]


def good_vectors() -> dict[str, list[float]]:
    return {
        "en": unit(1.0, 0.1, 0.0),
        "roman_ur": unit(1.0, 0.2, 0.0),
        "ur": unit(0.9, 0.1, 0.1),
        "weather": unit(0.0, 0.1, 1.0),
        "cricket": unit(0.1, 1.0, 0.0),
    }


def test_sound_vectors_pass() -> None:
    problems, scores = smoke.check_vectors(good_vectors())
    assert problems == []
    assert scores["en ~ roman_ur"] > scores["en ~ weather"]


def test_a_weak_pair_is_fine_while_each_sentence_still_ranks_right() -> None:
    # the real MiniLM shape (2026-10-20): en ~ ur 0.905, but roman_ur only 0.18 to ur and 0.13
    # to en, while 0.08 to weather: still ranked right, so not a problem
    vectors = {
        "en": unit(1.0, 0.05, 0.0, 0.0),
        "ur": unit(0.95, 0.1, 0.0, 0.0),
        "roman_ur": unit(0.18, 0.0, 0.08, 0.98),
        "weather": unit(0.0, 0.0, 1.0, 0.0),
        "cricket": unit(0.0, 1.0, 0.0, 0.0),
    }
    problems, scores = smoke.check_vectors(vectors)
    assert scores["en ~ roman_ur"] < 0.2
    assert problems == []


def test_unrelated_sentences_as_close_as_the_same_meaning_fail() -> None:
    vectors = good_vectors()
    vectors["weather"] = vectors["en"]
    problems, _ = smoke.check_vectors(vectors)
    assert any("not clearly closer" in p for p in problems)


def test_nan_and_unnormalised_vectors_fail() -> None:
    vectors = good_vectors()
    vectors["en"] = [float("nan"), 0.0, 0.0]
    vectors["cricket"] = [2.0, 0.0, 0.0]
    problems, _ = smoke.check_vectors(vectors)
    assert any("NaN" in p for p in problems)
    assert any("length 2.000" in p for p in problems)


def test_different_sizes_fail_at_once() -> None:
    vectors = good_vectors()
    vectors["ur"] = [1.0, 0.0]
    problems, scores = smoke.check_vectors(vectors)
    assert problems and scores == {}

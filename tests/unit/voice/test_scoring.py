"""Bake-off arithmetic (Phase 6, step 5b). Only text and numbers: no audio, no network."""

import pytest

from app.voice.scoring import (
    ClipResult,
    build_result,
    cer,
    edit_distance,
    format_report,
    normalize_text,
    percentile,
    script_of,
    summarize,
    wer,
)

# ---- normalisation


def test_case_punctuation_and_spaces_are_removed() -> None:
    assert normalize_text("  Iski  PRICE, kya hai?? ") == "iski price kya hai"


def test_arabic_letter_variants_are_unified() -> None:
    assert normalize_text("كيا") == normalize_text("کیا")  # kaf and yeh variants


def test_diacritics_are_removed() -> None:
    assert normalize_text("قِیمت") == "قیمت"


def test_urdu_punctuation_and_arabic_digits() -> None:
    assert normalize_text("١٠ ہزار؟") == "10 ہزار"
    assert normalize_text("۱۰ ہزار۔") == "10 ہزار"


def test_apostrophes_inside_words_are_dropped() -> None:
    assert normalize_text("don't") == "dont"


# ---- WER and CER


def test_wer_counts_one_wrong_word_in_four() -> None:
    assert wer("iski price kya hai", "iski price kia hai") == pytest.approx(0.25)


def test_wer_is_zero_for_the_same_text_with_other_punctuation() -> None:
    assert wer("Iski price kya hai?", "iski price, kya hai") == 0.0


def test_wer_is_one_for_an_empty_transcript() -> None:
    assert wer("iski price kya hai", "") == 1.0


def test_wer_has_no_value_without_a_reference() -> None:
    assert wer("", "anything") is None
    assert cer("  ", "anything") is None


def test_cer_counts_characters() -> None:
    assert cer("abc", "abd") == pytest.approx(1 / 3)


def test_cer_counts_a_missing_space() -> None:
    assert cer("a b", "ab") == pytest.approx(1 / 3)


def test_edit_distance_with_an_empty_side() -> None:
    assert edit_distance([], ["a"]) == 1
    assert edit_distance(["a", "b"], []) == 2


# ---- script


def test_script_of_each_script() -> None:
    assert script_of("iski price") == "latin"
    assert script_of("اس کی قیمت") == "arabic"
    assert script_of("ਇਸ ਦੀ ਕੀਮਤ") == "gurmukhi"
    assert script_of("इसकी कीमत") == "devanagari"


def test_script_of_numbers_and_even_mix() -> None:
    assert script_of("123 ?!") == "none"
    assert script_of("ab قی") == "mixed"  # 2 Latin and 2 Arabic letters: no script above 50%


# ---- percentile


def test_percentile_nearest_rank() -> None:
    assert percentile([4, 1, 3, 2], 50) == 2
    assert percentile([4, 1, 3, 2], 95) == 4
    assert percentile([], 50) is None


# ---- build_result


def test_build_result_scores_the_transcript() -> None:
    result = build_result(
        provider="p",
        file="ur-001.ogg",
        clip_class="urdu",
        reference="iski price kya hai",
        hypothesis="iski price kia hai",
    )
    assert result.wer == pytest.approx(0.25)
    assert result.script == "latin"
    assert result.cer is not None


def test_a_failed_clip_gets_no_scores() -> None:
    result = build_result(
        provider="p", file="x", clip_class="urdu", reference="a b", error="provider failed"
    )
    assert (result.wer, result.cer, result.script) == (None, None, "none")


# ---- summarize and format_report


def make(
    provider: str = "P",
    clip_class: str = "urdu",
    *,
    wer_value: float | None = 0.0,
    latency_ms: int | None = 100,
    gate_reason: str | None = None,
    error: str | None = None,
    intent_expected: str | None = "price",
    intent_got: str | None = "price",
    hypothesis: str = "iski price kya hai",
) -> ClipResult:
    return ClipResult(
        provider=provider,
        file="f",
        clip_class=clip_class,
        duration_s=30.0,
        reference="iski price kya hai",
        hypothesis=hypothesis,
        wer=wer_value,
        cer=wer_value,
        script="latin",
        latency_ms=latency_ms,
        gate_reason=gate_reason,
        error=error,
        intent_expected=intent_expected,
        intent_got=intent_got,
    )


def test_summarize_one_class() -> None:
    rows = [
        make(wer_value=0.0, latency_ms=100, intent_got="price"),
        make(wer_value=0.5, latency_ms=300, gate_reason="low_confidence", intent_got="specs"),
        make(wer_value=None, latency_ms=None, error="boom", intent_got=None),
    ]
    m = summarize(rows)["P"]["urdu"]

    assert m["clips"] == 3
    assert m["failures"] == 1
    assert m["failure_pct"] == 33.3
    assert m["wer_pct"] == 25.0
    assert m["latency_p50_ms"] == 100
    assert m["latency_p95_ms"] == 300
    assert m["gate_pass_pct"] == 50.0
    assert m["intent_pct"] == 50.0
    assert m["scripts"] == {"latin": 100.0}
    assert m["audio_minutes"] == 1.5


def test_summarize_has_an_all_row_and_one_group_per_provider() -> None:
    rows = [make("A", "urdu"), make("A", "punjabi"), make("B", "urdu")]
    summary = summarize(rows)

    assert list(summary) == ["A", "B"]
    assert list(summary["A"]) == ["punjabi", "urdu", "all"]
    assert summary["A"]["all"]["clips"] == 2
    assert summary["B"]["all"]["clips"] == 1


def test_intent_is_n_a_when_the_router_was_not_run() -> None:
    m = summarize([make(intent_got=None)])["P"]["urdu"]
    assert m["intent_pct"] is None


def test_report_warns_when_a_class_is_small() -> None:
    report = format_report(summarize([make()]))
    assert "== P" in report
    assert "smoke test, not a baseline" in report


def test_report_has_no_warning_for_a_big_enough_class() -> None:
    report = format_report(summarize([make(), make()]), min_clips_per_class=2)
    assert "smoke test" not in report

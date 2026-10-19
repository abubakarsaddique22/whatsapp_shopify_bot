"""Phase 12, Step 4f: scoring of the latency evaluation (no model, no database)."""

from pathlib import Path

from app.evals import latency
from app.evals.latency import LatencyRun, percentile


def text_runs(seconds: list[float]) -> list[LatencyRun]:
    return [LatencyRun(f"q-{i}", "text", s) for i, s in enumerate(seconds)]


def test_percentile_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 21)]  # 1 .. 20

    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0  # the 19th of 20
    assert percentile(values, 100) == 20.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0  # unsorted input is fine


def test_summary_adds_the_poll_wait_to_the_measured_time() -> None:
    s = latency.summarize(text_runs([4.0] * 20), poll_allowance=2.0)

    assert s["text"]["p50"] == 4.0
    assert s["text"]["p50_total"] == 6.0
    assert s["text"]["p95_total"] == 6.0


def test_bad_runs_are_not_in_the_percentiles() -> None:
    runs = [*text_runs([5.0] * 20), LatencyRun("q-bad", "text", 99.0, ok=False, note="429")]
    s = latency.summarize(runs)

    assert s["text"]["runs"] == 21
    assert s["text"]["good"] == 20
    assert s["text"]["max"] == 5.0
    assert [r.id for r in s["text"]["bad"]] == ["q-bad"]


def test_gate_is_met_inside_both_targets() -> None:
    assert latency.gate_met(text_runs([5.0] * 20))  # 5 + 2 = 7 <= 8 and <= 15


def test_gate_fails_when_p50_is_too_slow() -> None:
    # 6.5 s measured + 2 s poll wait = 8.5 s > 8 s
    assert not latency.gate_met(text_runs([6.5] * 20))


def test_gate_fails_when_p95_is_too_slow() -> None:
    # 18 fast runs and 2 slow ones: the 19th of 20 (p95) is slow, p50 is fine
    runs = text_runs([3.0] * 18 + [14.0, 14.0])

    s = latency.summarize(runs)
    assert s["text"]["p50_total"] == 5.0
    assert s["text"]["p95_total"] == 16.0
    assert not latency.gate_met(runs)


def test_gate_needs_enough_good_runs() -> None:
    assert not latency.gate_met(text_runs([1.0] * (latency.MIN_SAMPLES - 1)))
    assert not latency.gate_met([])
    only_bad = [LatencyRun(f"q-{i}", "text", 1.0, ok=False) for i in range(30)]
    assert not latency.gate_met(only_bad)


def test_voice_is_reported_apart_and_has_no_gate() -> None:
    voice = [LatencyRun("v-1", "voice", 30.0), LatencyRun("v-2", "voice", 40.0)]
    runs = [*text_runs([5.0] * 20), *voice]

    s = latency.summarize(runs)
    assert s["voice"]["p50"] == 30.0
    assert s["text"]["good"] == 20  # voice never mixes into the text numbers
    assert latency.gate_met(runs)  # slow voice does not fail the text gate
    assert "Voice (no gate" in latency.format_report(runs)


def test_report_says_when_there_are_no_voice_timings() -> None:
    text = latency.format_report(text_runs([5.0] * 20), voice_clips=0)

    assert "Voice: no timings yet" in text
    assert "MET" in text


def test_report_lists_the_runs_that_were_not_counted() -> None:
    runs = [*text_runs([5.0] * 3), LatencyRun("price-09", "text", 1.0, ok=False, note="429")]
    text = latency.format_report(runs)

    assert "NOT ENOUGH GOOD RUNS" in text
    assert "price-09: 429" in text
    assert "NOT MET" in text


def test_voice_clip_count_skips_the_header(tmp_path: Path) -> None:
    manifest = tmp_path / "voice_manifest.csv"

    assert latency.voice_clip_count(manifest) == 0  # no file
    manifest.write_text("file,lang,text\n", encoding="utf-8")
    assert latency.voice_clip_count(manifest) == 0  # header only
    manifest.write_text("file,lang,text\na.ogg,ur,x\nb.ogg,en,y\n", encoding="utf-8")
    assert latency.voice_clip_count(manifest) == 2

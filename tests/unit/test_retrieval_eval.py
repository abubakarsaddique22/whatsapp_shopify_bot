"""Phase 12, Step 4d: the retrieval scoring and gate. No database, no embedding model."""

from app.evals import retrieval
from app.rag.rag_eval import CaseResult, GoldenCase
from app.rag.retrieve import Passage

EXPECT = "within 14 days"


def passage(text: str, score: float = 0.8, chunk_id: int = 1) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id=1,
        doc_type="policy_refund",
        heading_path="Refunds",
        text=text,
        source_url=None,
        score=score,
        rrf=0.03,
    )


def case(case_id: str, lang: str = "en", expect: str | None = EXPECT) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        lang=lang,
        doc_type="policy_refund" if expect else "none",
        question="q",
        query_en="q",
        expect=expect,
    )


def right(case_id: str, lang: str = "en") -> CaseResult:
    return CaseResult(case(case_id, lang), [passage(f"Returns {EXPECT} of delivery.")])


def wrong(case_id: str, lang: str = "en") -> CaseResult:
    return CaseResult(case(case_id, lang), [passage("Shipping takes 3 days.")])


def second(case_id: str) -> CaseResult:
    found = [passage("Shipping takes 3 days.", chunk_id=1), passage(EXPECT, chunk_id=2)]
    return CaseResult(case(case_id), found)


def answered(case_id: str, score: float = 0.9) -> CaseResult:
    """A question the documents cannot answer, and the search found something anyway."""
    return CaseResult(case(case_id, expect=None), [passage("Some text.", score=score)])


def test_all_right_meets_the_gate() -> None:
    results = [right("a"), right("b"), right("c", "roman_ur")]

    assert retrieval.gate_met(results, 0.4)


def test_a_weak_language_fails_the_hit_gate_even_when_the_overall_rate_is_high() -> None:
    # English 18/18, Roman Urdu 1/2: overall 19/20 = 95%, but Roman Urdu is only 50%
    results = [right(f"en{i}") for i in range(18)] + [right("ur1", "roman_ur")]
    results.append(wrong("ur2", "roman_ur"))

    summary = retrieval.summarize(results, 0.4)

    assert summary.overall.hit_rate >= retrieval.TARGET_HIT_RATE
    assert not retrieval.hit_gate_met(results, 0.4)
    assert not retrieval.gate_met(results, 0.4)


def test_mrr_is_reported_but_has_no_target() -> None:
    results = [second(f"s{i}") for i in range(10)]  # always found, always second: MRR 0.5

    summary = retrieval.summarize(results, 0.4)

    assert summary.overall.hit_rate == 1.0
    assert summary.overall.mrr == 0.5
    assert retrieval.gate_met(results, 0.4)


def test_nothing_answerable_means_nothing_measured() -> None:
    results = [CaseResult(case("n1", expect=None), [])]

    assert not retrieval.hit_gate_met(results, 0.4)
    assert not retrieval.gate_met(results, 0.4)


def test_low_abstention_fails_the_gate_but_not_the_hit_gate() -> None:
    results = [right("a"), right("b"), answered("n1")]  # abstention 0/1

    summary = retrieval.summarize(results, 0.4)

    assert summary.overall.abstain_correct == 0
    assert summary.overall.abstain_cases == 1
    assert retrieval.hit_gate_met(results, 0.4)
    assert not retrieval.abstention_gate_met(results, 0.4)
    assert not retrieval.gate_met(results, 0.4)


def test_abstention_at_the_target_meets_the_gate() -> None:
    # the unanswerable question found only a weak passage (0.2 < 0.4): the bot abstains
    results = [right("a"), right("b"), answered("n1", score=0.2)]

    assert retrieval.abstention_gate_met(results, 0.4)
    assert retrieval.gate_met(results, 0.4)


def test_the_threshold_decides_what_counts_as_found() -> None:
    results = [CaseResult(case("a"), [passage(f"Returns {EXPECT}.", score=0.3)])]

    assert retrieval.gate_met(results, 0.2)
    assert not retrieval.gate_met(results, 0.5)  # under the threshold: false abstain


def test_report_names_both_targets_the_languages_and_the_wrong_questions() -> None:
    results = [right("a"), wrong("b", "roman_ur"), answered("n1")]

    report = retrieval.format_report(results, 0.4)

    assert "Gate (blueprint 11.4, proposed)" in report
    assert "hit rate NOT MET" in report
    assert "abstention NOT MET" in report
    assert "roman_ur" in report
    assert "b: right passage not returned" in report
    assert "n1: answered but should abstain" in report

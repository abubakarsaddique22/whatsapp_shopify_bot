"""Router eval: golden file, scoring arithmetic, T08/T09 (Phase 5, step 5).

Everything here uses FakeLLM. The real-model score comes from `app.cli eval-router`."""

import json

import pytest

from app.agent.router import route_intent
from app.agent.router_eval import (
    SYNTHETIC,
    GoldenCase,
    format_report,
    load_golden,
    run_eval,
    summarize,
)
from app.domain.models import Intent, Lang
from app.integrations.llm.fake_llm import FakeLLM
from app.language.policy import next_language, reply_language


def answer(**fields):
    data = {"language": "roman_ur", "intent": "price", "confidence": 0.9}
    data.update(fields)
    return json.dumps(data)


def perfect_answer(case):
    """What a flawless router would answer for this golden case."""
    return answer(
        language=case.language.value,
        intent=case.intent.value,
        color=case.color,
        max_price=case.max_price,
    )


# ---- the golden file -------------------------------------------------------------------


def test_golden_file_is_valid_and_covers_every_language_class():
    cases = load_golden()
    assert len(cases) >= 60
    assert len({c.text for c in cases}) == len(cases)  # no message twice
    assert all(c.text.strip() for c in cases)
    assert {c.language for c in cases} >= {Lang.EN, Lang.ROMAN_UR, Lang.MIXED, Lang.UR, Lang.PA}
    assert Intent.ABUSE_OR_INJECTION in {c.intent for c in cases}
    assert Intent.PRIVATE_DATA_REQUEST in {c.intent for c in cases}


def test_duplicate_ids_are_rejected(tmp_path):
    line = '{"id": "x-01", "text": "hi", "language": "en", "intent": "greeting"}'
    path = tmp_path / "golden.jsonl"
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate ids"):
        load_golden(path)


@pytest.mark.parametrize(
    "line",
    [
        '{"id": "x", "text": "hi", "language": "en", "intent": "not_an_intent"}',
        '{"id": "x", "text": "hi", "language": "klingon", "intent": "greeting"}',
        '{"id": "x", "language": "en", "intent": "greeting"}',  # no text
        "this is not json",
    ],
)
def test_bad_golden_line_is_rejected(tmp_path, line):
    path = tmp_path / "golden.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden(path)


def test_blank_lines_are_ignored(tmp_path):
    line = '{"id": "x-01", "text": "hi", "language": "en", "intent": "greeting"}'
    path = tmp_path / "golden.jsonl"
    path.write_text("\n" + line + "\n\n", encoding="utf-8")
    assert len(load_golden(path)) == 1


# ---- the labels agree with the router's own checks -------------------------------------


async def test_a_flawless_router_scores_100_percent_on_the_golden_set():
    """If this fails, a label is wrong or the router/price parser disagrees with it
    (for example a model code like GA-2100 read as a price)."""
    cases = load_golden()
    llm = FakeLLM([perfect_answer(c) for c in cases])
    summary = summarize(await run_eval(cases, llm))

    assert summary["failures"] == []
    assert summary["language_pct"] == summary["intent_pct"] == 100.0
    assert summary["color_pct"] == summary["max_price_pct"] == 100.0
    assert summary["fallbacks"] == 0
    assert summary["total"] == len(cases) == len(llm.calls)
    assert sum(summary["sources"].values()) == len(cases)


# ---- the scoring arithmetic ------------------------------------------------------------

MINI = [
    GoldenCase(
        id="t1",
        text="What is the price of the GA-2100?",
        language=Lang.EN,
        intent=Intent.PRICE,
    ),
    GoldenCase(
        id="t2",
        text="refund milta hai?",
        language=Lang.ROMAN_UR,
        intent=Intent.POLICY_REFUND,
    ),
    GoldenCase(
        id="t3",
        text="black wali available hai?",
        language=Lang.MIXED,
        intent=Intent.AVAILABILITY,
        color="black",
    ),
    GoldenCase(
        id="t4",
        text="10k ke andar koi watch hai?",
        language=Lang.ROMAN_UR,
        intent=Intent.PRODUCT_SEARCH,
        max_price=10000,
        source=SYNTHETIC,
    ),
]


async def mini_results():
    llm = FakeLLM(
        [
            answer(language="en", intent="price"),  # t1 right
            answer(language="roman_ur", intent="faq"),  # t2 wrong intent
            answer(language="roman_ur", intent="availability"),  # t3 wrong language, colour missed
            answer(language="roman_ur", intent="product_search", max_price=10000),  # t4 right
        ]
    )
    return await run_eval(MINI, llm)


async def test_scores_are_counted_correctly():
    summary = summarize(await mini_results())
    assert summary["total"] == 4
    assert summary["language_pct"] == 75.0  # t3 wrong
    assert summary["intent_pct"] == 75.0  # t2 wrong
    assert summary["color_pct"] == 0.0  # only t3 involves a colour, and it was missed
    assert summary["max_price_pct"] == 100.0  # only t4 involves a price, and it was right
    assert summary["fallbacks"] == 0
    assert 0.0 <= summary["hint_language_pct"] <= 100.0


async def test_scores_are_split_by_language_class():
    by_language = summarize(await mini_results())["by_language"]
    assert by_language["en"] == {"cases": 1, "language_pct": 100.0, "intent_pct": 100.0}
    assert by_language["roman_ur"] == {"cases": 2, "language_pct": 100.0, "intent_pct": 50.0}
    assert by_language["mixed"] == {"cases": 1, "language_pct": 0.0, "intent_pct": 100.0}


async def test_failures_name_the_wrong_fields():
    failures = summarize(await mini_results())["failures"]
    assert [f["id"] for f in failures] == ["t2", "t3"]
    assert failures[0]["wrong"] == ["intent"]
    assert failures[1]["wrong"] == ["language", "color"]
    assert "faq" in failures[0]["got"]
    assert "policy_refund" in failures[0]["expected"]


async def test_the_word_check_hint_is_recorded_without_the_model():
    case = GoldenCase(
        id="h1", text="ap ka pass black color hai?", language=Lang.ROMAN_UR, intent=Intent.PRICE
    )
    results = await run_eval([case], FakeLLM([answer(language="en")]))
    assert results[0].hint == Lang.ROMAN_UR  # the model said 'en', the word check did not


async def test_a_router_that_gives_up_is_counted_as_a_fallback():
    case = MINI[0]
    results = await run_eval([case], FakeLLM(["bad", "still bad"]))
    summary = summarize(results)
    assert summary["fallbacks"] == 1
    assert summary["intent_pct"] == 0.0
    assert len(summary["failures"]) == 1


async def test_limit_runs_only_the_first_cases():
    llm = FakeLLM([answer()] * 10)
    results = await run_eval(MINI, llm, limit=2)
    assert [r.case.id for r in results] == ["t1", "t2"]
    assert len(llm.calls) == 2


def test_empty_results_do_not_crash():
    summary = summarize([])
    assert summary["total"] == 0
    assert summary["intent_pct"] is None
    assert "n/a" in format_report(summary)


async def test_report_shows_numbers_wrong_cases_and_the_synthetic_warning():
    report = format_report(summarize(await mini_results()))
    assert "Intent match:" in report
    assert "75.0%" in report
    assert "refund milta hai?" in report  # a wrong case is listed with its text
    assert "Wrong (2)" in report
    assert "not a real baseline" in report  # t4 is marked synthetic-starter


async def test_no_synthetic_warning_for_reviewed_cases():
    case = GoldenCase(
        id="c1", text="hello", language=Lang.EN, intent=Intent.GREETING, source="client"
    )
    llm = FakeLLM([answer(intent="greeting", language="en")])
    report = format_report(summarize(await run_eval([case], llm)))
    assert "not a real baseline" not in report


# ---- T08 and T09: language is detected and the reply language follows ------------------


async def test_t08_english_message_gets_an_english_reply_language():
    text = "What is the price of the GA-2100?"
    out = await route_intent(text, FakeLLM([answer(language="en", intent="price")]))
    language = next_language(Lang.UNKNOWN, None, out.language, text)
    assert out.language == Lang.EN
    assert reply_language(language) == "en"


async def test_t09_roman_urdu_message_gets_a_roman_urdu_reply_language():
    text = "ap ka pass black color hai?"
    llm = FakeLLM([answer(language="roman_ur", intent="colors_sizes", color="black")])
    out = await route_intent(text, llm)
    language = next_language(Lang.UNKNOWN, None, out.language, text)
    assert out.language == Lang.ROMAN_UR
    assert reply_language(language) == "roman_ur"


async def test_t08_t09_over_the_whole_golden_set():
    """First message of a conversation: the reply language follows table 6.1 for every case."""
    expected = {
        Lang.EN: "en",
        Lang.ROMAN_UR: "roman_ur",
        Lang.MIXED: "mixed",
        Lang.UR: "roman_ur",  # Urdu script gets Roman Urdu (A4, needs client confirmation)
        Lang.PA: "roman_ur",
    }
    for case in load_golden():
        out = await route_intent(case.text, FakeLLM([perfect_answer(case)]))
        language = next_language(Lang.UNKNOWN, None, out.language, case.text)
        assert reply_language(language) == expected[case.language], case.id

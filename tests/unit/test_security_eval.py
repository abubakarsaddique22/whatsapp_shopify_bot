"""Phase 12, step 4: the security evaluator (app/evals/security.py). FakeLLM, no network."""

import json

import pytest

from app.core.errors import ProviderError
from app.evals import security
from app.evals.security import (
    KNOWN_GAPS,
    customer_rows,
    document_rows,
    format_report,
    gate_met,
    load_rows,
    run_router,
    scan_documents,
    summarize,
)
from app.integrations.llm.fake_llm import FakeLLM


def says(intent: str) -> str:
    return json.dumps({"language": "en", "intent": intent, "confidence": 0.9})


def perfect_answers(rows) -> list[str]:
    """What a router that always picks the first accepted intent would answer."""
    return [says(row.ok_intents[0]) for row in rows]


ROWS = load_rows()
CUSTOMERS = customer_rows(ROWS)
DOCUMENTS = document_rows(ROWS)


# ---- the file


def test_every_row_is_a_customer_message_or_a_store_text() -> None:
    assert len(CUSTOMERS) + len(DOCUMENTS) == len(ROWS)
    assert len(ROWS) >= 50  # blueprint 11.3: at least 50


def test_the_set_covers_every_kind_of_attack_and_three_languages() -> None:
    assert {r.category for r in CUSTOMERS} >= {
        "direct_injection",
        "secret_request",
        "private_data",
        "false_authority",
        "abusive",
    }
    assert {r.lang for r in CUSTOMERS} >= {"en", "roman_ur", "ur"}


def test_a_row_with_both_or_neither_field_is_refused(tmp_path) -> None:
    both = {
        "id": "x",
        "category": "c",
        "lang": "en",
        "text": "t",
        "ok_intents": ["greeting"],
        "must_flag": True,
    }
    neither = {"id": "x", "category": "c", "lang": "en", "text": "t"}
    for row in (both, neither):
        path = tmp_path / "a.jsonl"
        path.write_text(json.dumps(row), encoding="utf-8")
        with pytest.raises(ValueError, match="OR must_flag"):
            load_rows(path)


def test_an_unknown_intent_in_the_file_is_refused(tmp_path) -> None:
    row = {"id": "x", "category": "c", "lang": "en", "text": "t", "ok_intents": ["hacking"]}
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown intent"):
        load_rows(path)


def test_a_duplicate_id_is_refused(tmp_path) -> None:
    row = {"id": "x", "category": "c", "lang": "en", "text": "t", "ok_intents": ["greeting"]}
    path = tmp_path / "a.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_rows(path)


# ---- the router half


async def test_a_router_that_recognises_every_attack_meets_the_gate() -> None:
    results = await run_router(CUSTOMERS, FakeLLM(perfect_answers(CUSTOMERS)))

    assert all(r.ok for r in results)
    assert gate_met(results, scan_documents(DOCUMENTS))


async def test_an_attack_the_router_calls_a_greeting_fails_the_gate_and_is_named() -> None:
    answers = perfect_answers(CUSTOMERS)
    answers[0] = says("greeting")  # adv-001, 'ignore all previous instructions'

    results = await run_router(CUSTOMERS, FakeLLM(answers))

    assert [r.row.id for r in results if r.wrong] == [CUSTOMERS[0].id]
    assert not gate_met(results, [])
    report = format_report(results, [])
    assert "ROUTER GOT IT WRONG (1)" in report and CUSTOMERS[0].id in report
    assert "NOT MET" in report


async def test_any_accepted_intent_counts_as_a_pass() -> None:
    row = next(r for r in CUSTOMERS if len(r.ok_intents) > 1)  # e.g. false authority
    for intent in row.ok_intents:
        (result,) = await run_router([row], FakeLLM([says(intent)]))
        assert result.ok


async def test_an_intent_that_is_not_accepted_is_wrong_even_if_it_sounds_safe() -> None:
    row = next(r for r in CUSTOMERS if r.ok_intents == ["private_data_request"])

    (result,) = await run_router([row], FakeLLM([says("out_of_scope")]))

    assert result.wrong  # T19 wants private_data_request, not just any refusal


async def test_a_provider_failure_is_a_run_error_not_a_model_mistake() -> None:
    row = CUSTOMERS[0]

    (result,) = await run_router([row], FakeLLM([ProviderError("down"), ProviderError("down")]))

    assert result.fell_back and not result.ok and not result.wrong
    assert not gate_met([result], [])  # still not a pass: run it again
    assert "Run errors (1)" in format_report([result], [])


async def test_limit_and_by_category_numbers() -> None:
    results = await run_router(CUSTOMERS, FakeLLM(perfect_answers(CUSTOMERS[:5])), limit=5)

    assert len(results) == 5
    summary = summarize(results, [])
    assert summary.customer_total == 5 and summary.customer_ok == 5
    assert sum(total for _, total in summary.by_category.values()) == 5


# ---- the scanner half


def test_the_scanner_passes_every_row_except_the_known_gap() -> None:
    # When the gap is closed this fails on purpose (like the strict xfail in test_t21_ingest.py):
    # remove the id from KNOWN_GAPS and from this expectation.
    docs = scan_documents(DOCUMENTS)

    failed = [d.row.id for d in docs if not d.ok]
    assert failed == sorted(KNOWN_GAPS)  # exactly the known gap, nothing else
    assert gate_met([], docs)


def test_the_known_gap_is_reported_but_does_not_fail_the_gate() -> None:
    report = format_report([], scan_documents(DOCUMENTS))

    assert "Known gaps, not counted" in report and "adv-048" in report
    assert "SCANNER GOT IT WRONG" not in report
    assert report.rstrip().endswith("MET")


def test_a_real_scanner_miss_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "scan_text", lambda text: [])  # a scanner that sees nothing

    docs = scan_documents(DOCUMENTS)

    assert not gate_met([], docs)
    report = format_report([], docs)
    assert "SCANNER GOT IT WRONG" in report and "should flag" in report


def test_a_scanner_that_flags_normal_text_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "scan_text", lambda text: ["everything"])

    docs = scan_documents(DOCUMENTS)

    assert not gate_met([], docs)
    assert "should NOT flag" in format_report([], docs)

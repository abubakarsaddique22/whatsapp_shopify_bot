"""Phase 12, Step 4b: the scoring of the language evaluation (no model, no database)."""

from app.agent.router_eval import GoldenCase
from app.domain.models import Intent, Lang
from app.evals import language
from app.evals.language import LanguageResult

ENGLISH_REPLY = "The price of this watch is Rs 3,820 and it is in stock right now."
ROMAN_UR_REPLY = (
    "Is watch ki price Rs 3,820 hai aur yeh abhi stock mein hai, aap order kar sakte hain."
)


def case(lang: Lang, text: str = "hello") -> GoldenCase:
    return GoldenCase(id="t-01", text=text, language=lang, intent=Intent.PRICE)


def test_router_language_is_compared_with_the_case() -> None:
    assert LanguageResult(case(Lang.EN), Lang.EN, ENGLISH_REPLY).router_ok
    assert not LanguageResult(case(Lang.ROMAN_UR), Lang.MIXED, ROMAN_UR_REPLY).router_ok
    assert not LanguageResult(case(Lang.EN), None, ENGLISH_REPLY).router_ok


def test_reply_language_follows_the_case_language() -> None:
    assert LanguageResult(case(Lang.EN), Lang.EN, ENGLISH_REPLY).reply_ok
    assert LanguageResult(case(Lang.ROMAN_UR), Lang.ROMAN_UR, ROMAN_UR_REPLY).reply_ok
    # English question answered in Roman Urdu, and the other way round
    assert not LanguageResult(case(Lang.EN), Lang.EN, ROMAN_UR_REPLY).reply_ok
    assert not LanguageResult(case(Lang.ROMAN_UR), Lang.ROMAN_UR, ENGLISH_REPLY).reply_ok


def test_every_other_language_is_answered_in_roman_urdu() -> None:
    for lang in (Lang.UR, Lang.PA, Lang.MIXED, Lang.UNKNOWN):
        assert LanguageResult(case(lang), lang, ROMAN_UR_REPLY).reply_ok
        assert not LanguageResult(case(lang), lang, ENGLISH_REPLY).reply_ok


def test_empty_and_error_replies_are_not_scored() -> None:
    assert not LanguageResult(case(Lang.EN), Lang.EN, "").scored_reply
    assert not LanguageResult(case(Lang.EN), Lang.EN, "[ERROR ValueError] boom").scored_reply
    assert not LanguageResult(case(Lang.EN), Lang.EN, "[PROVIDER FAILED 3x] x").scored_reply
    assert LanguageResult(case(Lang.EN), Lang.EN, ENGLISH_REPLY).scored_reply


def test_summary_counts_and_gate() -> None:
    good = [LanguageResult(case(Lang.EN), Lang.EN, ENGLISH_REPLY) for _ in range(19)]
    bad = LanguageResult(case(Lang.EN), Lang.ROMAN_UR, ENGLISH_REPLY)  # router wrong, reply fine

    summary = language.summarize([*good, bad])
    assert summary["total"] == 20
    assert summary["router_pct"] == 95.0
    assert summary["reply_pct"] == 100.0
    assert language.gate_met([*good, bad])  # exactly 95 % is enough

    assert not language.gate_met([*good[:18], bad, bad])  # 90 % router


def test_gate_is_not_met_without_scored_replies() -> None:
    assert not language.gate_met([])
    assert not language.gate_met([LanguageResult(case(Lang.EN), Lang.EN, "")])


def test_report_names_the_failures() -> None:
    wrong_router = LanguageResult(case(Lang.ROMAN_UR, "kya price hai?"), Lang.MIXED, ROMAN_UR_REPLY)
    wrong_reply = LanguageResult(case(Lang.EN, "price?"), Lang.EN, ROMAN_UR_REPLY)

    text = language.format_report([wrong_router, wrong_reply])

    assert "Router named another language (1)" in text
    assert "expected roman_ur, got mixed" in text
    assert "Reply in the wrong language (1)" in text
    assert "NOT MET" in text

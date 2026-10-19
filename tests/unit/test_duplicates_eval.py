"""Phase 12, Step 4f: scoring of the duplicate-reply evaluation (no model, no database)."""

from app.evals import duplicates
from app.evals.duplicates import ScenarioResult, Tally


def scenario(
    name: str, *tallies: Tally, known_gap: bool = False, error: str = ""
) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        what="what happens",
        deliveries=2,
        tallies={f"c{i}": t for i, t in enumerate(tallies)},
        known_gap=known_gap,
        error=error,
    )


CLEAN = Tally(wamids=1, inbox=1, queued=1, sent=1)


def test_one_message_one_reply_is_clean() -> None:
    assert CLEAN.duplicate_inbox == 0
    assert CLEAN.duplicate_replies == 0
    assert CLEAN.lost_replies == 0
    assert scenario("t16", CLEAN, CLEAN).ok


def test_a_stored_twice_message_is_a_duplicate_inbox_row() -> None:
    tally = Tally(wamids=1, inbox=2, queued=1, sent=1)

    assert tally.duplicate_inbox == 1
    assert not scenario("t16", tally).ok


def test_a_reply_sent_twice_is_a_duplicate_reply() -> None:
    tally = Tally(wamids=1, inbox=1, queued=1, sent=2)

    assert tally.duplicate_replies == 1
    assert tally.lost_replies == 0
    assert not scenario("chaos", tally).ok


def test_a_message_without_a_reply_is_lost() -> None:
    nothing_sent = Tally(wamids=1, inbox=1, queued=1, sent=0)
    never_queued = Tally(wamids=2, inbox=2, queued=1, sent=1)  # 2 messages, 1 reply

    assert nothing_sent.lost_replies == 1
    assert never_queued.lost_replies == 1
    assert not scenario("chaos", nothing_sent).ok


def test_two_messages_two_replies_is_clean() -> None:
    assert scenario("burst", Tally(wamids=2, inbox=2, queued=2, sent=2)).ok


def test_a_scenario_that_crashed_or_saw_nothing_is_not_ok() -> None:
    assert not scenario("boom", error="RuntimeError: x").ok
    assert not scenario("empty").ok  # no customer at all proves nothing


def test_totals_add_up_over_customers() -> None:
    result = scenario("chaos", Tally(1, 1, 1, 2), Tally(1, 2, 1, 1), CLEAN)

    assert result.customers == 3
    assert result.duplicate_replies == 1
    assert result.duplicate_inbox == 1
    assert result.sent == 4


def test_gate_needs_every_counted_scenario_clean() -> None:
    assert duplicates.gate_met([scenario("a", CLEAN), scenario("b", CLEAN)])
    assert not duplicates.gate_met([scenario("a", CLEAN), scenario("b", Tally(1, 1, 1, 2))])
    assert not duplicates.gate_met([])


def test_a_known_gap_is_shown_but_not_counted() -> None:
    gap = scenario("meta_accepts_then_crash", Tally(1, 1, 1, 2), known_gap=True)
    results = [scenario("t16", CLEAN), gap]

    assert duplicates.gate_met(results)
    assert duplicates.totals(results) == (0, 0)
    text = duplicates.format_report(results)
    assert "Known gap" in text
    assert "meta_accepts_then_crash" in text
    assert "MET" in text


def test_only_a_known_gap_is_not_a_pass() -> None:
    assert not duplicates.gate_met([scenario("gap", CLEAN, known_gap=True)])


def test_report_names_the_customer_that_went_wrong() -> None:
    text = duplicates.format_report([scenario("t16_webhook_twice", Tally(1, 1, 1, 3))])

    assert "duplicate replies 2" in text
    assert "c0: messages 1, inbox 1, queued 1, sent 3" in text
    assert "NOT MET" in text

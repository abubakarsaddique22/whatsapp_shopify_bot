"""Phase 12, Step 5: the failure drills (Blueprint 11.2, 11.4 'Error recovery', Phase 12 card).

The blueprint asks for five drills: Shopify down, LLM down, STT down, Meta send failing and a
database restart. "Each failure drill has a written expected behaviour and a test", and the gate is
"every drill ends in a safe, honest behaviour".

This file is the WRITTEN part: what is broken, what must happen, and which test proves it. The
tests live in tests/drills/test_failure_drills.py (real worker, real Postgres, every test rolled
back). scripts/failure_drills.py runs those tests and prints the report from `judge()` below.

Known gaps are written down, not hidden. A gap has its own test marked
`xfail(strict=True)`: it fails today on purpose. When the code is fixed, the strict xfail turns
into a FAILED test, which is the signal to delete the gap note here and the xfail marker there.

Scoring is pure (no database, no pytest): unit tested in tests/unit/test_drills.py.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

BLUEPRINT_FAILURES = ("shopify", "llm", "stt", "meta", "database")  # one drill name prefix each

DrillStatus = Literal["PASS", "GAP", "FAIL"]


@dataclass(frozen=True)
class Drill:
    name: str  # starts with one of BLUEPRINT_FAILURES
    breaks: str  # what is broken during the drill
    expected: str  # the written expected behaviour
    tests: tuple[str, ...]  # test functions that must PASS
    gap: str | None = None  # a known gap: what is not safe or honest yet
    gap_tests: tuple[str, ...] = ()  # strict-xfail tests that describe the wanted behaviour


DRILLS: tuple[Drill, ...] = (
    Drill(
        name="shopify_down",
        breaks="Shopify Admin API raises ShopifyUnavailable on the live price lookup",
        expected=(
            "get_price returns ok=False with error_code shopify_unavailable; no price is guessed "
            "and the mirror price is never used. The customer hears that the price cannot be "
            "checked right now (composer) or gets the safe reply plus a handoff. If the composer "
            "invents a price anyway, the validator rejects it twice and the safe reply plus a "
            "handoff go out: the invented number never reaches the outbox."
        ),
        tests=(
            "test_shopify_down_price_is_not_guessed",
            "test_shopify_down_an_invented_price_never_reaches_the_customer",
        ),
    ),
    Drill(
        name="llm_down",
        breaks="the LLM provider is down (ProviderUnavailable): composer only, or the router",
        expected=(
            "Composer down: the safe_fallback template goes out, a handoff record is written "
            "(reason composer_failed) and the bot pauses for this customer. Router down: no "
            "crash, no tool, no composer call; the customer is told honestly that there is a "
            "technical problem (bot_trouble template, NOT 'please give more detail'), one "
            "handoff record (reason llm_unavailable) and the pause: the next message gets one "
            "'team has your request', then silence, and staff can read every message. A "
            "provider that answers with nothing usable (plain ProviderError) still counts as an "
            "unclear message: one clarifying question."
        ),
        tests=(
            "test_llm_composer_down_gives_the_safe_reply_and_a_handoff",
            "test_llm_router_down_says_so_and_hands_off",
            "test_llm_router_down_next_messages_wait_for_the_team",
            "test_llm_a_garbled_answer_is_still_treated_as_unclear",
        ),
    ),
    Drill(
        name="stt_down",
        breaks="the speech-to-text provider raises TranscriptionError",
        expected=(
            "The worker tries the voice note again (MAX_ATTEMPTS = 3); a failed try leaves "
            "nothing half-done (no transcript, no reply row) and the temporary audio file is "
            "closed after every try. On the LAST try the customer gets one honest reply in the "
            "conversation language (voice_unavailable: 'cannot listen to voice messages right "
            "now, please type your question'), the transcript line is '[voice: not transcribed]' "
            "and the row ends 'done'. A provider that fails once and then works gives the normal "
            "turn."
        ),
        tests=(
            "test_stt_down_is_retried_then_the_customer_is_told",
            "test_stt_recovers_on_the_retry",
        ),
    ),
    Drill(
        name="meta_send_failing",
        breaks="Meta's send API answers 5xx (temporary) or 401 (permanent, expired token)",
        expected=(
            "Temporary: the reply row is retried up to sender.MAX_ATTEMPTS (5) and then becomes "
            "'failed' (dead letter); nothing is sent twice and the worker never crashes. "
            "Permanent: 'failed' at once, never retried. Either way last_error holds only the "
            "HTTP status and Meta code (no token, no phone number). When Meta comes back the "
            "pending reply is sent exactly once."
        ),
        tests=(
            "test_meta_temporary_error_is_retried_then_dead_lettered",
            "test_meta_permanent_error_fails_at_once",
            "test_meta_recovers_and_sends_exactly_once",
        ),
    ),
    Drill(
        name="database_restart",
        breaks="Postgres is unreachable (restart), before or in the middle of a turn",
        expected=(
            "Webhook while the database is down: a non-200 answer, so Meta delivers again later; "
            "the redelivery after the restart is stored once. Worker while the database is "
            "down: the pass raises (main() logs worker_loop_error and waits) and the message "
            "stays pending; the sender pass returns quietly. Connection lost in the middle of a "
            "turn: everything of that turn is rolled back (no reply row, no transcript line), the "
            "retry produces exactly one reply."
        ),
        tests=(
            "test_database_down_webhook_is_refused_so_meta_retries",
            "test_database_down_worker_waits_and_the_message_stays_pending",
            "test_database_lost_mid_turn_leaves_nothing_half_done",
        ),
    ),
)


@dataclass
class DrillResult:
    drill: Drill
    status: DrillStatus
    problems: list[str] = field(default_factory=list)


def judge(drill: Drill, outcomes: Mapping[str, str]) -> DrillResult:
    """PASS: every test passed. GAP: that, and every gap test is still an expected failure.
    FAIL: anything else (a test failed, was skipped, is missing, or a gap test passed)."""
    problems: list[str] = []
    for test in drill.tests:
        outcome = outcomes.get(test, "missing")
        if outcome != "passed":
            problems.append(f"{test}: {outcome}")
    for test in drill.gap_tests:
        outcome = outcomes.get(test, "missing")
        if outcome in ("passed", "xpassed"):
            problems.append(
                f"{test}: {outcome}: the gap looks fixed, remove the xfail and the gap note"
            )
        elif outcome == "failed":
            problems.append(
                f"{test}: failed (an XPASS(strict) means the gap is fixed: remove the xfail and "
                "the gap note; otherwise the test itself broke)"
            )
        elif outcome != "xfailed":
            problems.append(f"{test}: {outcome}")
    if problems:
        return DrillResult(drill, "FAIL", problems)
    return DrillResult(drill, "GAP" if drill.gap else "PASS")


def judge_all(outcomes: Mapping[str, str], drills: Sequence[Drill] = DRILLS) -> list[DrillResult]:
    return [judge(drill, outcomes) for drill in drills]


def gate_met(results: Sequence[DrillResult]) -> bool:
    """Blueprint 11.4: every drill ends in a safe, honest behaviour. A known gap is NOT met."""
    return bool(results) and all(r.status == "PASS" for r in results)


def format_report(results: Sequence[DrillResult]) -> str:
    title = "FAILURE DRILLS (Blueprint 11.4: every drill ends in a safe, honest behaviour)"
    lines = ["", title, ""]
    for result in results:
        drill = result.drill
        lines.append(f"[{result.status}] {drill.name}")
        lines.append(f"    breaks:   {drill.breaks}")
        lines.append(f"    expected: {drill.expected}")
        if result.status == "GAP" and drill.gap:
            lines.append(f"    KNOWN GAP: {drill.gap}")
        for problem in result.problems:
            lines.append(f"    PROBLEM: {problem}")
        lines.append("")
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "GAP", "FAIL")}
    verdict = "MET" if gate_met(results) else "NOT MET"
    lines.append(
        f"Gate: {verdict} ({counts['PASS']} pass, {counts['GAP']} known gap, "
        f"{counts['FAIL']} fail, of {len(results)} drills)"
    )
    return "\n".join(lines)
